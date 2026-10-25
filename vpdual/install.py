"""`init` (write the config) and `service` (render the systemd unit).

`init` checks the Paralon key and the vastai CLI, reads your Vast machines and
Paralon nodes (one read each), lets you pair them, and writes:
  - the config (default ~/.config/vast-paralon-dual/config.json)
  - the key file `env` next to it (mode 600), for the service
  - mode `dual` for each paired machine in the state file (nothing is changed
    on Vast or Paralon until the watcher runs)
"""

import getpass
import json
import os
import shlex
import shutil
import sys

from .config import (DEFAULT_CONFIG, POLICIES, POLICY_HELP, ConfigError,
                     config_path, parse_config)
from .paralon import ParalonClient, ParalonError
from .state import MODES, StateStore
from .vast import VastCLI, VastError

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(HERE, "vast_paralon_dual.py")
TEMPLATE = os.path.join(HERE, "systemd", "vast-paralon-dual.service")
UNIT_PATH = "/etc/systemd/system/vast-paralon-dual.service"


class InitError(Exception):
    """A plain message for the host; init stops."""


def env_file_for(cfg_path):
    return os.path.join(os.path.dirname(cfg_path), "env")


def _cmd(cfg_path, *args):
    parts = [shlex.quote(sys.executable or "python3"), shlex.quote(SCRIPT)]
    if cfg_path != config_path(DEFAULT_CONFIG):
        parts += ["-c", shlex.quote(cfg_path)]
    return " ".join(parts + list(args))


# -- service ---------------------------------------------------------------
def render_service(cfg_path, user=None, python=None):
    with open(TEMPLATE, encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    while lines and lines[0].startswith("#"):
        lines.pop(0)              # the template's how-to header
    text = "\n".join(lines) + "\n"
    for key, val in {"@USER@": user or getpass.getuser(),
                     "@PYTHON@": python or sys.executable or "/usr/bin/python3",
                     "@SCRIPT@": SCRIPT,
                     "@CONFIG@": cfg_path,
                     "@ENV_FILE@": env_file_for(cfg_path)}.items():
        text = text.replace(key, val)
    return "# Rendered by `vast_paralon_dual.py service`\n" + text


def service_command(cfg_path):
    return (f"{_cmd(cfg_path, 'service')} | sudo tee {UNIT_PATH} >/dev/null"
            " && sudo systemctl daemon-reload"
            " && sudo systemctl enable --now vast-paralon-dual")


# -- checks ----------------------------------------------------------------
def check_key(env):
    key = (env.get("PARALON_API_KEY") or "").strip()
    if not key:
        raise InitError(
            "PARALON_API_KEY is not set.\n"
            "  Create a key with the \"My nodes\" permission in the Paralon "
            "dashboard, then run:\n"
            "    export PARALON_API_KEY=prlc_...\n"
            "  and run init again.")
    return key


def check_vastai(binary, which):
    path = which(binary)
    if not path:
        raise InitError(
            f"`{binary}` is not installed (or not on PATH).\n"
            "  Install Vast's CLI and log it in:\n"
            "    pip install --user vastai\n"
            "    vastai set api-key YOUR_VAST_API_KEY")
    return path


def read_vast(vast):
    try:
        machines = vast.read_machines()
    except VastError as exc:
        raise InitError(
            f"vastai cannot read your machines: {exc}\n"
            "  Is it logged in? Run: vastai set api-key YOUR_VAST_API_KEY")
    good = {k: v for k, v in machines.items() if not isinstance(v, VastError)}
    if not good:
        raise InitError("your Vast account has no machines to pair")
    return dict(sorted(good.items()))


def read_paralon(paralon):
    try:
        nodes = paralon.list_nodes()
    except ParalonError as exc:
        raise InitError(f"cannot read your Paralon nodes: {exc}")
    if not nodes:
        raise InitError("your Paralon account has no nodes to pair")
    return nodes


# -- prompts ---------------------------------------------------------------
def _ask_policy(ask, out):
    out("\nWhile a Paralon renter is on a machine, what should Vast do?")
    for i, p in enumerate(POLICIES, 1):
        out(f"  {i}) {p:<16} {POLICY_HELP[p]}")
    while True:
        ans = ask("Choose 1 or 2: ").strip()
        if ans in ("1", "2"):
            return POLICIES[int(ans) - 1]
        if ans in POLICIES:
            return ans
        out("  please type 1 or 2")


def _vast_label(vm):
    raw = vm.raw
    gpus = f"{raw.get('num_gpus', '?')}x {raw.get('gpu_name', 'GPU')}"
    host = raw.get("hostname") or ""
    price = (f"${raw['listed_gpu_cost']}/GPU/h" if vm.listed and
             raw.get("listed_gpu_cost") is not None else "")
    state = "listed" if vm.listed else "UNLISTED"
    return " ".join(x for x in (gpus, host, state, price) if x)


def _ask_pairs(vms, nodes, ask, out):
    out("\nYour Paralon nodes:")
    for i, n in enumerate(nodes, 1):
        kind = "spot" if n.spot else "ON-DEMAND (switch to spot for dual)"
        out(f"  {i}) {n.id}  {n.label}  {kind}")
    pairs, used = [], set()
    for vid, vm in vms.items():
        out(f"\nVast machine {vid}: {_vast_label(vm)}")
        while True:
            ans = ask("  Pair with Paralon node number (Enter = skip): ").strip()
            if not ans:
                break
            if ans.isdigit() and 1 <= int(ans) <= len(nodes) \
                    and int(ans) not in used:
                used.add(int(ans))
                pairs.append((vid, nodes[int(ans) - 1].id))
                break
            out("  not a free node number")
    return pairs


def _ask_price(ask, out, prompt):
    while True:
        ans = ask(prompt).strip()
        if not ans:
            return None
        try:
            return float(ans)
        except ValueError:
            out("  type a number like 0.40, or press Enter")


# -- writing ---------------------------------------------------------------
def _write_private(path, text, mode=0o600):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def run_init(args, *, env=None, which=shutil.which, ask=input, out=print,
             vast=None, paralon=None):
    """Returns an exit code. `vast` / `paralon` are injectable for tests."""
    env = os.environ if env is None else env
    cfg_path = config_path(args.config)
    interactive = not (args.policy and args.pair)
    try:
        key = check_key(env)
        if vast is None:
            vast = VastCLI(check_vastai(args.vastai_bin, which))
            vastai_bin = vast.binary
        else:
            vastai_bin = which(args.vastai_bin) or args.vastai_bin
        if paralon is None:
            paralon = ParalonClient(args.paralon_base_url, key)
        if os.path.exists(cfg_path) and not args.force:
            if not interactive:
                raise InitError(f"{cfg_path} already exists; pass --force to "
                                "overwrite it")
            if ask(f"{cfg_path} exists. Overwrite? [y/N] ").strip().lower() \
                    not in ("y", "yes"):
                raise InitError("nothing written")
        vms = read_vast(vast)
        nodes = read_paralon(paralon)

        policy = args.policy or _ask_policy(ask, out)
        if args.pair:
            pairs = []
            node_ids = {n.id for n in nodes}
            for item in args.pair:
                vid, sep, nid = item.partition("=")
                if not sep or not vid.strip().isdigit():
                    raise InitError(f"--pair {item!r}: use VAST_ID=NODE_ID")
                vid, nid = int(vid), nid.strip()
                if vid not in vms:
                    raise InitError(f"Vast machine {vid} is not in your "
                                    f"account (have: {', '.join(map(str, vms))})")
                if nid not in node_ids:
                    raise InitError(f"Paralon node {nid} is not in your "
                                    "account")
                pairs.append((vid, nid))
        else:
            pairs = _ask_pairs(vms, nodes, ask, out)
        if not pairs:
            raise InitError("no machine paired; nothing written")

        raw = {"on_paralon_rental": policy, "vastai_bin": vastai_bin,
               "machines": []}
        top = {k: v for k, v in (("price_gpu", args.price_gpu),
                                 ("price_min_bid", args.price_min_bid),
                                 ("duration", args.duration)) if v is not None}
        if top:
            raw["vast_listing"] = top
        if args.state_file:
            raw["state_file"] = os.path.abspath(
                os.path.expanduser(args.state_file))
        by_id = {n.id: n for n in nodes}
        for vid, nid in pairs:
            entry = {"vast_machine_id": vid, "paralon_node_id": nid}
            if not by_id[nid].spot:
                out(f"WARNING: Paralon node {nid} is ON-DEMAND. Dual mode "
                    "needs spot to reclaim it; switch it in the dashboard.")
            if not vms[vid].listed and not {"price_gpu", "price_min_bid"} <= set(top):
                out(f"NOTE: Vast machine {vid} is unlisted now, so there is "
                    "no current price to save. The tool needs fallback prices "
                    "to list it (otherwise it holds and logs why).")
                if interactive:
                    gpu = _ask_price(ask, out, "  price_gpu $/GPU/h (Enter = "
                                     "skip): ")
                    bid = _ask_price(ask, out, "  price_min_bid $/GPU/h (Enter "
                                     "= skip): ") if gpu is not None else None
                    if gpu is not None and bid is not None:
                        entry["vast_listing"] = {"price_gpu": gpu,
                                                 "price_min_bid": bid}
            raw["machines"].append(entry)

        cfg = parse_config(raw, env)         # validate before writing
    except (InitError, ConfigError) as exc:
        out(f"error: {exc}")
        return 2

    try:
        _write_private(cfg_path, json.dumps(raw, indent=2) + "\n", 0o644)
        env_path = env_file_for(cfg_path)
        _write_private(env_path, f"PARALON_API_KEY={key}\n", 0o600)
        store = StateStore(cfg.state_file)
        state = store.load([m.vast_machine_id for m in cfg.machines])
        for m in cfg.machines:
            ms = state["machines"][str(m.vast_machine_id)]
            if ms["mode"] not in MODES:
                ms["mode"] = args.mode
        store.save(state)
    except (OSError, ValueError) as exc:
        out(f"error: cannot write {cfg_path} / state: {exc}")
        return 2

    out(f"\nWrote {cfg_path} ({len(pairs)} machine(s), {policy}) and the key "
        f"file {env_path} (mode 600).")
    out(f"Mode `{args.mode}` is set; nothing has been changed on Vast or "
        "Paralon yet.\n")
    out("Next:")
    out("  1. See both sides and what it would do (changes nothing):")
    out(f"       {_cmd(cfg_path, 'status')}")
    out("  2. Start the service (runs as you, on every boot):")
    out(f"       {service_command(cfg_path)}")
    out("     Follow it with: journalctl -u vast-paralon-dual -f")
    return 0


def add_init_args(p):
    p.add_argument("--policy", choices=POLICIES,
                   help="what Vast does while Paralon is rented")
    p.add_argument("--pair", action="append", metavar="VAST_ID=NODE_ID",
                   help="pair a Vast machine with a Paralon node (repeat for "
                        "more). With --policy, init asks nothing.")
    p.add_argument("--price-gpu", type=float, dest="price_gpu",
                   help="fallback $/GPU/h, used only if no saved price")
    p.add_argument("--price-min-bid", type=float, dest="price_min_bid",
                   help="fallback min bid $/GPU/h, used only if no saved price")
    p.add_argument("--duration", help="fallback offer length, e.g. '14 days'")
    p.add_argument("--mode", choices=MODES, default="dual",
                   help="starting mode for the paired machines (default dual)")
    p.add_argument("--state-file", dest="state_file",
                   help="state file (default "
                        "~/.local/state/vast-paralon-dual/state.json)")
    p.add_argument("--vastai-bin", default="vastai", dest="vastai_bin")
    p.add_argument("--paralon-base-url", dest="paralon_base_url",
                   default="https://paraloncloud.com/api/v1")
    p.add_argument("--force", action="store_true",
                   help="overwrite an existing config")

