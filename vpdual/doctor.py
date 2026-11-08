"""`doctor`: check an install before it is trusted with your listings (v0.8.0).

Every check is a row: PASS, WARN or FAIL, and a one-line fix for anything
but a pass. Read-only: it runs `vastai --version`, `vastai show user`,
`vastai show machines`, one GET /nodes, `docker ps` per paralon_agent (over
ssh for user@host), and `systemctl show` for the unit. The only file it
writes is the `.lock` beside the state file, which every pass takes anyway.

`run` makes the same checks at start-up and refuses to start (exit 2) on a
FAIL in the first five groups (BLOCKING): a watcher that cannot read Vast,
Paralon, its config, its machines or their Docker would only hold every
pass. `--skip-doctor` bypasses that. `init` prints the table at its end.
"""

import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass

from .agent import AgentError, DockerHost, SystemdControl
from .config import (REQUIRED_PRICES, ConfigError, DEFAULT_STATE, key_from_env_file,
                     load_config)
from .paralon import ParalonClient, ParalonError
from .state import StateStore
from .vast import VastCLI, VastError

PASS, WARN, FAIL, SKIP = "PASS", "WARN", "FAIL", "SKIP"

MIN_PYTHON = (3, 8)
VASTAI_MIN = (1, 3, 0)
# The newest vastai CLI this release was tested against; a newer one is a
# WARN (its --raw output has changed shape before), not a failure.
VASTAI_TESTED = "1.3.0"
UNIT = "vast-paralon-dual"
LOCK_TRIES = 6            # x 0.5 s: a pass holds the lock for seconds at most

GROUPS = ("python + vastai", "Paralon key", "config", "machines and nodes",
          "Paralon agents", "relist prices", "state file", "service")
BLOCKING = GROUPS[:5]     # `run` refuses to start on a FAIL in these


@dataclass
class Check:
    group: str
    name: str
    status: str
    detail: str
    hint: str = ""


def _version(text):
    m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", text or "")
    return tuple(int(x or 0) for x in m.groups()) if m else None


def _vtext(v):
    return ".".join(map(str, v))


def vastai_version(binary, runner=subprocess.run):
    """`vastai --version` as a tuple, or None."""
    try:
        res = runner([binary, "--version"], capture_output=True, text=True,
                     timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return _version(res.stdout or res.stderr) if res.returncode == 0 else None


def summary(rows):
    """For `status --json` (v0.8.0): counts, and each non-PASS row's group,
    check and status -- never its detail or hint, which can name paths,
    hosts or users. An agent's check is named by its user@host: dropped."""
    return {"pass": sum(r.status == PASS for r in rows),
            "warn": sum(r.status == WARN for r in rows),
            "fail": sum(r.status == FAIL for r in rows),
            "problems": [{"group": r.group, "status": r.status,
                          "check": "agent" if r.group == GROUPS[4] else r.name}
                         for r in rows if r.status in (WARN, FAIL)]}


def _raw_config(path):
    """The config as JSON, for the few settings the early checks need even
    when it does not validate. {} if unreadable (the config check says why)."""
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
        return raw if isinstance(raw, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_probe(directory, existing=None):
    """None if a file can really be created in `directory` (a temp file,
    removed at once) and `existing`, when given, opened for append (nothing
    written); else the OS error as text."""
    try:
        with tempfile.NamedTemporaryFile(dir=directory, prefix=".vpdual-probe-",
                                         delete=True):
            pass
        if existing is not None:
            with open(existing, "a"):
                pass
    except OSError as exc:
        return str(exc)
    return None


class Doctor:
    """Collaborators are injectable for tests: `runner` (subprocess.run
    shape) for vastai/systemctl, `vast` (read_machines), `paralon`
    (list_nodes), `docker` (target -> DockerHost)."""

    def __init__(self, cfg_path, *, env=None, runner=subprocess.run,
                 which=shutil.which, vast=None, paralon=None, docker=None,
                 python=None, sleep=time.sleep):
        self.cfg_path = cfg_path
        self.env = os.environ if env is None else env
        self.runner = runner
        self.which = which
        self.vast = vast
        self.paralon = paralon
        self.docker = docker or (lambda t: DockerHost(t, run=runner))
        self.python = tuple(python or sys.version_info[:3])
        self.sleep = sleep
        self.rows = []
        self.cfg = None
        self.nodes = None

    def add(self, group, name, status, detail, hint=""):
        self.rows.append(Check(group, name, status, detail, hint))

    def _run(self, argv, timeout=30):
        return self.runner(argv, capture_output=True, text=True, timeout=timeout)

    # -- the checks, in GROUPS order ------------------------------------
    def run(self):
        raw = _raw_config(self.cfg_path)
        self.check_python_vastai(raw)
        self.check_paralon(raw)
        self.check_config()
        if self.cfg is None:
            for g in GROUPS[3:7]:
                self.add(g, "-", SKIP, "not checked: the config did not load")
        else:
            self.check_ids()
            self.check_agents()
            self.check_prices()
            self.check_state()
        self.check_service()
        return self.rows

    def check_python_vastai(self, raw):
        g = GROUPS[0]
        v = self.python
        if v >= MIN_PYTHON:
            self.add(g, "python", PASS, f"Python {_vtext(v)}")
        else:
            self.add(g, "python", FAIL, f"Python {_vtext(v)} is older than "
                     f"{_vtext(MIN_PYTHON)}", "install Python 3.8 or newer")
        name = raw.get("vastai_bin") if isinstance(raw.get("vastai_bin"), str) \
            and raw.get("vastai_bin") else "vastai"
        binary = self.which(name)
        if not binary:
            self.add(g, "vastai found", FAIL, f"`{name}` is not on PATH",
                     "pip install --user vastai; or set \"vastai_bin\" to its "
                     "full path, e.g. ~/.local/bin/vastai")
            return
        self.binary = binary
        self.add(g, "vastai found", PASS, binary)
        ver = vastai_version(binary, self.runner)
        tested = _version(VASTAI_TESTED)
        if ver is None:
            self.add(g, "vastai version", FAIL, "`vastai --version` gave no "
                     "version", "pip install --user --upgrade vastai")
        elif ver < VASTAI_MIN:
            self.add(g, "vastai version", FAIL, f"vastai {_vtext(ver)} is older "
                     f"than {_vtext(VASTAI_MIN)}",
                     "pip install --user --upgrade vastai")
        elif ver > tested:
            self.add(g, "vastai version", WARN, f"vastai {_vtext(ver)} is newer "
                     f"than the tested {VASTAI_TESTED}",
                     "probably fine; report anything odd in `status`")
        else:
            self.add(g, "vastai version", PASS, f"vastai {_vtext(ver)}")
        try:
            # stdout carries the account's details: never shown
            ok = self._run([binary, "show", "user", "--raw"]).returncode == 0
        except (OSError, subprocess.SubprocessError):
            ok = False
        if ok:
            self.add(g, "vastai logged in", PASS, "`vastai show user` works")
        else:
            self.add(g, "vastai logged in", FAIL, "`vastai show user` failed",
                     "vastai set api-key YOUR_VAST_KEY (as the user that runs "
                     "this tool)")

    def check_paralon(self, raw):
        g = GROUPS[1]
        key = self.env.get("PARALON_API_KEY", "").strip()
        env_path = os.path.join(os.path.dirname(self.cfg_path), "env")
        if key:
            self.add(g, "key present", PASS, "PARALON_API_KEY from the environment")
        else:
            key = key_from_env_file(self.cfg_path)
            if not key:
                self.add(g, "key present", FAIL, f"no PARALON_API_KEY in the "
                         f"environment or {env_path}",
                         "run `init`, or write PARALON_API_KEY=prlc_... to that "
                         "file (chmod 600)")
                self.add(g, "GET /nodes", SKIP, "no key")
                return
            mode = os.stat(env_path).st_mode & 0o777
            if mode & 0o077:
                self.add(g, "key present", WARN, f"{env_path} is mode "
                         f"{mode:o}: other users can read the key",
                         f"chmod 600 {env_path}")
            else:
                self.add(g, "key present", PASS, f"{env_path} (mode 600)")
        client = self.paralon
        if client is None:
            base = raw.get("paralon_base_url") or "https://paraloncloud.com/api/v1"
            client = ParalonClient(base, key, min_get_interval_s=0)
        try:
            self.nodes = {n.id: n for n in client.list_nodes(refresh=True)}
        except ParalonError as exc:
            self.add(g, "GET /nodes", FAIL, str(exc),
                     "the key needs the \"My nodes\" permission (Paralon "
                     "dashboard -> API keys)")
            return
        self.add(g, "GET /nodes", PASS, f"{len(self.nodes)} node(s) readable")

    def check_config(self):
        g = GROUPS[2]
        try:
            self.cfg = load_config(self.cfg_path, self.env)
        except ConfigError as exc:
            more = len(exc.errors) - 1
            self.add(g, "loads and validates", FAIL, str(exc).splitlines()[0]
                     + (f" (and {more} more)" if more > 0 else ""),
                     f"fix {self.cfg_path} (or run `init`)")
            return
        self.add(g, "loads and validates", PASS,
                 f"{self.cfg_path}: {len(self.cfg.machines)} machine(s)")

    def check_ids(self):
        g = GROUPS[3]
        vast = self.vast or VastCLI(getattr(self, "binary", None)
                                    or self.cfg.vastai_bin, runner=self.runner)
        try:
            vms = vast.read_machines()
        except VastError as exc:
            self.add(g, "show machines", FAIL, str(exc),
                     "check `vastai show machines` works for this user")
            vms = None
        for m in self.cfg.machines:
            vid, nid = m.vast_machine_id, m.paralon_node_id
            if vms is not None:
                if vid in vms:
                    self.add(g, f"vast {vid}", PASS, "in `show machines`")
                else:
                    self.add(g, f"vast {vid}", FAIL, "not in `show machines`",
                             "fix vast_machine_id (ids: `vastai show machines`)")
            if self.nodes is None:
                self.add(g, f"node {nid}", SKIP, "GET /nodes failed above")
            elif nid in self.nodes:
                self.add(g, f"node {nid}", PASS, "in GET /nodes")
            else:
                self.add(g, f"node {nid}", FAIL, "not in GET /nodes",
                         "fix paralon_node_id (ids: Paralon dashboard -> My nodes)")

    def check_agents(self):
        g = GROUPS[4]
        seen = set()
        for m in self.cfg.machines:
            t = m.paralon_agent
            if m.agent_control == "none":
                self.add(g, f"machine {m.vast_machine_id}", PASS,
                         "agent_control none: the agent is never touched")
                continue
            if m.agent_control == "systemd":
                self.check_unit(g, m)
            if not t:
                self.add(g, f"machine {m.vast_machine_id}", WARN,
                         "no paralon_agent: the agent keeps starting work "
                         "beside Vast renters",
                         "set \"paralon_agent\": \"local\" or user@host")
                continue
            if t in seen:
                continue
            seen.add(t)
            try:
                n = len(self.docker(t).containers())
            except (AgentError, ValueError) as exc:
                hint = ("add this user to the docker group (sudo usermod -aG "
                        "docker $USER, then log in again)" if t == "local" else
                        f"key login (ssh-copy-id {t}) and that user in the "
                        "docker group there; test: ssh -o BatchMode=yes "
                        f"{t} docker ps")
                self.add(g, t, FAIL, str(exc), hint)
                continue
            how = "docker ps" if t == "local" else "ssh (BatchMode) + docker ps"
            self.add(g, t, PASS, f"{how} works ({n} container(s))")

    def check_unit(self, g, m):
        """agent_control systemd: `systemctl --user show` works there and
        the unit is loaded (v0.8.0)."""
        name = f"{m.paralon_agent_unit} on {m.paralon_agent}"
        try:
            unit = SystemdControl(self.docker(m.paralon_agent),
                                  m.paralon_agent_unit).agent()
        except (AgentError, ValueError) as exc:
            self.add(g, name, FAIL, str(exc), "systemctl --user needs the "
                     "user's systemd instance: loginctl enable-linger USER")
            return
        if unit is None:
            self.add(g, name, FAIL, "unit not loaded",
                     "check the name: systemctl --user list-units 'paralon*'")
        else:
            self.add(g, name, PASS, "systemctl --user works ("
                     + ("active" if unit.running else "not active") + ")")

    def check_prices(self):
        g = GROUPS[5]
        try:
            state = StateStore(self.cfg.state_file).load(
                [m.vast_machine_id for m in self.cfg.machines])
        except (OSError, ValueError):
            state = None
        for m in self.cfg.machines:
            st = (state or {}).get("machines", {}).get(str(m.vast_machine_id), {})
            saved = st.get("saved_listing") or {}
            owed = bool(st.get("took_off", {}).get("vast"))
            fallback = all(isinstance(m.vast_listing.get(k), (int, float))
                           for k in REQUIRED_PRICES)
            have = all(isinstance(saved.get(k), (int, float)) for k in REQUIRED_PRICES)
            name = f"machine {m.vast_machine_id}"
            fix = ("add price_gpu + price_min_bid to its vast_listing (or "
                   "the top-level one)")
            if owed and have:
                self.add(g, name, PASS, "relist owed; uses the saved listing")
            elif owed and fallback:
                self.add(g, name, WARN, "relist owed with no saved listing: "
                         "the config fallback prices will be used",
                         "check the config vast_listing prices are what you want")
            elif owed:
                self.add(g, name, FAIL, "relist owed and no price to relist "
                         "with: the relist holds", fix)
            elif fallback:
                self.add(g, name, PASS, "terms are saved at unlist; config "
                         "fallback present")
            else:
                self.add(g, name, WARN, "no config fallback: a machine you "
                         "unlisted by hand cannot be relisted", fix)

    def check_state(self):
        g = GROUPS[6]
        path = self.cfg.state_file or os.path.expanduser(DEFAULT_STATE)
        d = os.path.dirname(os.path.abspath(path))
        # v0.9.1: a real probe, never os.access() -- that says yes to root on
        # a read-only mount or a full disk. A temp file is created (and
        # removed) in the state directory, and an existing state file is
        # opened for append (nothing written).
        if not os.path.isdir(d):
            up = d
            while not os.path.exists(up) and os.path.dirname(up) != up:
                up = os.path.dirname(up)
            if not os.path.isdir(up):
                self.add(g, "writable", FAIL, f"cannot create {d}",
                         f"{up} is not a directory; set state_file to a path "
                         "under a directory")
                return
            err = _write_probe(up)
            if err is None:
                self.add(g, "writable", PASS, f"{d} will be created")
            else:
                self.add(g, "writable", FAIL, f"cannot create {d}",
                         f"{err}; mkdir -p {d} as a user that can, then chown it")
            return
        err = _write_probe(d, path if os.path.exists(path) else None)
        if err is not None:
            self.add(g, "writable", FAIL, f"{path} or its directory is not "
                     "writable", f"{err}; check the mount, the free space, "
                     f"then chown $USER {d} {path}")
            return
        self.add(g, "writable", PASS, path)
        try:
            with open(path + ".lock", "a") as fh:
                for i in range(LOCK_TRIES):
                    try:
                        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        if i + 1 < LOCK_TRIES:
                            self.sleep(0.5)
                        continue
                    fcntl.flock(fh, fcntl.LOCK_UN)
                    self.add(g, ".lock", PASS, "can be taken")
                    return
            self.add(g, ".lock", WARN, "held for 3 s by another process",
                     "a pass of a running `run` holds it briefly; if it stays "
                     "held, find the process: fuser " + path + ".lock")
        except OSError as exc:
            self.add(g, ".lock", FAIL, str(exc), f"chown $USER {path}.lock")

    def check_service(self):
        g = GROUPS[7]
        try:
            res = self._run(["systemctl", "show", "-p", "LoadState", "-p",
                             "ActiveState", UNIT], timeout=10)
            props = dict(line.partition("=")[::2]
                         for line in (res.stdout or "").splitlines())
        except (OSError, subprocess.SubprocessError):
            self.add(g, "unit", WARN, "systemctl not available",
                     "run `run` under your own supervisor")
            return
        if props.get("LoadState") != "loaded":
            self.add(g, "installed", WARN, f"{UNIT}.service is not installed",
                     "see `service` (after a day unarmed and arming)")
            return
        self.add(g, "installed", PASS, f"{UNIT}.service")
        if props.get("ActiveState") == "active":
            self.add(g, "active", PASS, "running")
        else:
            self.add(g, "active", WARN, f"not running ({props.get('ActiveState')})",
                     f"sudo systemctl enable --now {UNIT}")


def run_checks(cfg_path, **kw):
    return Doctor(cfg_path, **kw).run()


def blocking(rows):
    """The FAIL rows that stop `run` from starting."""
    return [r for r in rows if r.status == FAIL and r.group in BLOCKING]


def lines(rows):
    out, group = [], None
    for r in rows:
        if r.group != group:
            group = r.group
            out.append(f"{group}:")
        out.append(f"  {r.status}  {r.name:<20} {r.detail}")
        if r.hint and r.status != PASS:
            out.append(f"        fix: {r.hint}")
    fails = sum(r.status == FAIL for r in rows)
    warns = sum(r.status == WARN for r in rows)
    out.append(f"{fails} fail, {warns} warn, "
               f"{sum(r.status == PASS for r in rows)} pass")
    return out


def exit_code(rows):
    return 1 if any(r.status == FAIL for r in rows) else 0
