"""Load and validate the JSON config file.

New format (one or more machines):

    {"on_paralon_rental": "unlist_vast",
     "machines": [{"vast_machine_id": 123, "paralon_node_id": "abc"}]}

The old single-machine keys (`vast_machine_id` + `paralon_node_id` at the top
level) are still accepted and read as a one-machine list.
"""

import json
import os
import unicodedata
from dataclasses import dataclass, field

from .agent import valid_target

POLICIES = ("unlist_vast", "keep_vast_listed")   # the default first: init offers it as 1 (v0.4.8)
DEFAULT_POLICY = "unlist_vast"   # Vast cannot start a renter on a Paralon-rented GPU
POLICY_HELP = {
    "keep_vast_listed": "Vast stays listed while Paralon is rented; a Vast "
                        "booking reclaims the Paralon spot rental.",
    "unlist_vast": "Vast is unlisted while Paralon is rented and relisted "
                   "when the rental ends.",
}

# `vastai list machine` flags, keyed by the config / state name.
# Values are passed through unchanged; see `vastai list machine --help`.
LISTING_FLAGS = {
    "price_gpu": "--price_gpu",
    "price_min_bid": "--price_min_bid",
    "price_disk": "--price_disk",
    "price_inetu": "--price_inetu",
    "price_inetd": "--price_inetd",
    "discount_rate": "--discount_rate",
    "min_chunk": "--min_chunk",
    "end_date": "--end_date",
    "duration": "--duration",
    "vol_size": "--vol_size",
    "vol_price": "--vol_price",
}
# A relist cannot happen without these (saved from the machine, or config).
REQUIRED_PRICES = ("price_gpu", "price_min_bid")
NUMERIC_KEYS = ("price_gpu", "price_min_bid", "price_disk", "price_inetu",
                "price_inetd", "discount_rate", "vol_price")

DEFAULT_CONFIG = "~/.config/vast-paralon-dual/config.json"
DEFAULT_STATE = "~/.local/state/vast-paralon-dual/state.json"


MAX_NAME = 64


def clean_name(value):
    """A machine label as shown to people: a string, control characters
    dropped, trimmed, at most MAX_NAME characters; "" if nothing is left.
    Used to check the config's `name` and to tidy Vast's `hostname`."""
    if not isinstance(value, str):
        return ""
    text = "".join(" " if unicodedata.category(ch) in ("Cc", "Zl", "Zp")
                   else "" if unicodedata.category(ch) == "Cf" else ch
                   for ch in value)
    return " ".join(text.split())[:MAX_NAME].strip()


class ConfigError(Exception):
    """The config file is missing, unreadable or incomplete. `errors` holds
    every problem found as (field, message) pairs -- the web UI's settings
    show each beside its field; the exception's message is the first."""

    def __init__(self, message, errors=None):
        super().__init__(message)
        self.errors = list(errors) if errors else [("", message)]


@dataclass
class Machine:
    vast_machine_id: int
    paralon_node_id: str
    # Fallback listing terms: top-level `vast_listing` overlaid with this
    # machine's own `vast_listing`. Used only where no saved value exists.
    vast_listing: dict = field(default_factory=dict)
    # How to reach Docker on this GPU machine, to stop the Paralon agent
    # while a Vast renter has the GPU: "local" or "user@host" (ssh). Empty =
    # not managed (the agent keeps running beside Vast renters).
    paralon_agent: str = ""
    # Optional label for people ("GPU box 1"): `status` and the web UI.
    # Never a key -- commands still take the Vast machine id.
    name: str = ""


@dataclass
class Config:
    machines: list
    on_paralon_rental: str = DEFAULT_POLICY
    vast_listing: dict = field(default_factory=dict)
    state_file: str = DEFAULT_STATE
    poll_interval_s: int = 60
    paralon_base_url: str = "https://paraloncloud.com/api/v1"
    paralon_min_get_interval_s: int = 120
    paralon_max_calls_per_min: int = 6   # 4 until v0.4.7: a 30 s burst read left a write no room in its minute
    reclaim_retry_s: int = 300
    vastai_bin: str = "vastai"
    docker_events: bool = True     # act on container starts at once
    gpu_free_mib: int = 500         # "GPU free" = no compute process and <= this
    fast_poll_s: int = 5            # pass interval while a machine is recovering
    jupyter_refresh_s: int = 3600   # pull Paralon's Jupyter image this often while the agent is stopped
    idle_job_images: list = field(default_factory=list)   # the host's own idle job
    # v0.8.0: false = decide and log ("NOT ARMED: would ..."), write nothing.
    # `init` writes false; a config without the key (any install from before
    # v0.8.0) is armed, so it keeps working unchanged.
    armed: bool = True
    paralon_api_key: str = field(default="", repr=False)  # never printed

    def machine(self, vast_machine_id):
        for m in self.machines:
            if m.vast_machine_id == int(vast_machine_id):
                return m
        return None


def config_path(path=None):
    return os.path.abspath(os.path.expanduser(path or DEFAULT_CONFIG))


def _check_listing(listing, where, errors):
    if not isinstance(listing, dict):
        errors.append((where, f"\"{where}\" must be an object"))
        return {}
    unknown = sorted(k for k in listing
                     if k not in LISTING_FLAGS and not k.startswith("_"))
    if unknown:
        errors.append((where, f"unknown {where} keys: {', '.join(unknown)}"))
    for key in NUMERIC_KEYS:
        if key in listing and (isinstance(listing[key], bool)
                               or not isinstance(listing[key], (int, float))):
            errors.append((f"{where}.{key}",
                           f"\"{where}.{key}\" must be a number"))
    if "end_date" in listing and "duration" in listing:
        errors.append((f"{where}.duration",
                       f"\"{where}\": set end_date OR duration, not both "
                       "(vastai refuses the pair)"))
    return {k: v for k, v in listing.items() if not k.startswith("_")}


def _machine_list(raw, errors):
    legacy = "vast_machine_id" in raw or "paralon_node_id" in raw
    if "machines" in raw and legacy:
        errors.append(("machines", "use either \"machines\" or the old "
                       "vast_machine_id/paralon_node_id keys, not both"))
        return []
    if legacy:
        entries = [{"vast_machine_id": raw.get("vast_machine_id"),
                    "paralon_node_id": raw.get("paralon_node_id")}]
    else:
        entries = raw.get("machines")
    if not isinstance(entries, list) or not entries:
        errors.append((
            "machines",
            "config needs \"machines\": [{\"vast_machine_id\": ..., "
            "\"paralon_node_id\": \"...\"}] (run `init` to write one)"))
        return []
    return entries


INT_SETTINGS = ("poll_interval_s", "paralon_min_get_interval_s",
                "paralon_max_calls_per_min", "reclaim_retry_s", "gpu_free_mib",
                "fast_poll_s", "jupyter_refresh_s")
STR_SETTINGS = ("state_file", "vastai_bin", "paralon_base_url")


def check_config(raw, env=None):
    """Validate a config dict; nothing is read from or written to disk.
    Returns (Config, []) or (None, [(field, message), ...]) with EVERY
    problem found, in the order `parse_config` reports them. `load_config`
    and the web UI's settings both use it."""
    env = os.environ if env is None else env
    if not isinstance(raw, dict):
        return None, [("", "config must be a JSON object")]
    errors = []

    def err(where, msg):
        errors.append((where, msg))

    policy = raw.get("on_paralon_rental", DEFAULT_POLICY)
    if policy not in POLICIES:
        err("on_paralon_rental",
            "config must set \"on_paralon_rental\" to one of: "
            + " | ".join(POLICIES)
            + f" (default {DEFAULT_POLICY}).\n"
            + "\n".join(f"  {k:<16} - {v}" for k, v in POLICY_HELP.items()))
        policy = DEFAULT_POLICY

    top_listing = _check_listing(raw.get("vast_listing", {}), "vast_listing",
                                 errors)
    machines, seen_ids, seen_nodes = [], set(), set()
    for i, entry in enumerate(_machine_list(raw, errors)):
        where = f"machines[{i}]"
        if not isinstance(entry, dict):
            err(where, f"{where} must be an object")
            continue
        unknown = sorted(k for k in entry if not k.startswith("_") and k not in
                         ("vast_machine_id", "paralon_node_id", "vast_listing",
                          "paralon_agent", "name"))
        if unknown:
            err(where, f"unknown {where} keys: {', '.join(unknown)}")
        try:
            vid = int(entry.get("vast_machine_id"))
        except (TypeError, ValueError):
            err(f"{where}.vast_machine_id",
                f"{where}.vast_machine_id must be an integer")
            vid = None
        node = entry.get("paralon_node_id")
        if not node:
            err(f"{where}.paralon_node_id", f"{where}.paralon_node_id is missing")
            node = None
        else:
            node = str(node)
        if (vid is not None and vid in seen_ids) or \
                (node is not None and node in seen_nodes):
            err(where, f"{where}: machine {vid} or node {node} is listed twice")
        seen_ids.add(vid)
        seen_nodes.add(node)
        listing = dict(top_listing)
        listing.update(_check_listing(entry.get("vast_listing", {}),
                                      f"{where}.vast_listing", errors))
        agent = entry.get("paralon_agent", "")
        if agent and (not isinstance(agent, str) or not valid_target(agent)):
            err(f"{where}.paralon_agent",
                f"{where}.paralon_agent must be \"local\" or user@host for "
                f"ssh (got {agent!r})")
            agent = ""
        name = entry.get("name", "")
        if name != "" and clean_name(name) != name:
            err(f"{where}.name",
                f"{where}.name must be text of 1-{MAX_NAME} characters: no "
                "line breaks, tabs or control characters, no leading, "
                f"trailing or double spaces (got {name!r})")
            name = ""
        if vid is not None and node is not None:
            machines.append(Machine(vid, node, listing, agent or "", name))

    known = set(Config.__dataclass_fields__) - {"paralon_api_key", "machines"}
    unknown = sorted(k for k in raw if k not in known and not k.startswith("_")
                     and k not in ("machines", "vast_machine_id",
                                   "paralon_node_id"))
    if unknown:
        err("", f"unknown config keys: {', '.join(unknown)}")

    kwargs = {k: raw[k] for k in known if k in raw}
    for key in INT_SETTINGS:
        if key in kwargs and (isinstance(kwargs[key], bool)
                              or not isinstance(kwargs[key], int)
                              or kwargs[key] < 1):
            err(key, f"\"{key}\" must be a whole number >= 1 "
                     f"(got {kwargs[key]!r})")
    for key in ("docker_events", "armed"):
        if key in kwargs and not isinstance(kwargs[key], bool):
            err(key, f"\"{key}\" must be true or false (no quotes)")
    imgs = kwargs.get("idle_job_images", [])
    if not isinstance(imgs, list) or not all(isinstance(i, str) and i for i in imgs):
        err("idle_job_images",
            "\"idle_job_images\" must be a list of image name prefixes")
    for key in STR_SETTINGS:
        if key in kwargs and (not isinstance(kwargs[key], str) or not kwargs[key]):
            err(key, f"\"{key}\" must be a non-empty string")
    key = env.get("PARALON_API_KEY", "").strip()
    if not key:
        err("PARALON_API_KEY",
            "PARALON_API_KEY is not set (a prlc_... key with the "
            "\"My nodes\" permission)")
    if errors:
        return None, errors
    kwargs["vast_listing"] = top_listing
    kwargs["on_paralon_rental"] = policy
    cfg = Config(machines=machines, **kwargs)
    cfg.state_file = os.path.expanduser(cfg.state_file)
    cfg.paralon_api_key = key
    return cfg, []


def parse_config(raw, env=None):
    cfg, errors = check_config(raw, env)
    if errors:
        raise ConfigError(errors[0][1], errors)
    return cfg


def key_from_env_file(cfg_path):
    """PARALON_API_KEY from the `env` file `init` writes next to the config,
    so hand-run commands work without exporting the key."""
    try:
        with open(os.path.join(os.path.dirname(cfg_path), "env"),
                  encoding="utf-8") as fh:
            for line in fh:
                name, sep, value = line.strip().partition("=")
                if sep and name.strip() == "PARALON_API_KEY":
                    return value.strip().strip("'\"")
    except OSError:
        pass
    return ""


def load_config(path, env=None):
    path = os.path.expanduser(path)
    env = dict(os.environ if env is None else env)
    if not env.get("PARALON_API_KEY", "").strip():
        env["PARALON_API_KEY"] = key_from_env_file(os.path.abspath(path))
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {path} (run `init` to "
                          "create it)")
    except (OSError, ValueError) as exc:
        raise ConfigError(f"cannot read config {path}: {exc}")
    return parse_config(raw, env)
