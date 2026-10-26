"""Load and validate the JSON config file.

New format (one or more machines):

    {"on_paralon_rental": "keep_vast_listed",
     "machines": [{"vast_machine_id": 123, "paralon_node_id": "abc"}]}

The old single-machine keys (`vast_machine_id` + `paralon_node_id` at the top
level) are still accepted and read as a one-machine list.
"""

import json
import os
from dataclasses import dataclass, field

from .agent import valid_target

POLICIES = ("keep_vast_listed", "unlist_vast")
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


class ConfigError(Exception):
    """The config file is missing, unreadable or incomplete."""


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


@dataclass
class Config:
    machines: list
    on_paralon_rental: str
    vast_listing: dict = field(default_factory=dict)
    state_file: str = DEFAULT_STATE
    poll_interval_s: int = 60
    paralon_base_url: str = "https://paraloncloud.com/api/v1"
    paralon_min_get_interval_s: int = 60
    paralon_max_calls_per_min: int = 4
    reclaim_retry_s: int = 300
    vastai_bin: str = "vastai"
    docker_events: bool = False
    paralon_api_key: str = field(default="", repr=False)  # never printed

    def machine(self, vast_machine_id):
        for m in self.machines:
            if m.vast_machine_id == int(vast_machine_id):
                return m
        return None


def config_path(path=None):
    return os.path.abspath(os.path.expanduser(path or DEFAULT_CONFIG))


def _check_listing(listing, where):
    if not isinstance(listing, dict):
        raise ConfigError(f"\"{where}\" must be an object")
    unknown = sorted(k for k in listing
                     if k not in LISTING_FLAGS and not k.startswith("_"))
    if unknown:
        raise ConfigError(f"unknown {where} keys: {', '.join(unknown)}")
    for key in NUMERIC_KEYS:
        if key in listing and (isinstance(listing[key], bool)
                               or not isinstance(listing[key], (int, float))):
            raise ConfigError(f"\"{where}.{key}\" must be a number")
    if "end_date" in listing and "duration" in listing:
        raise ConfigError(f"\"{where}\": set end_date OR duration, not both "
                          "(vastai refuses the pair)")
    return {k: v for k, v in listing.items() if not k.startswith("_")}


def _machine_list(raw):
    legacy = "vast_machine_id" in raw or "paralon_node_id" in raw
    if "machines" in raw and legacy:
        raise ConfigError("use either \"machines\" or the old "
                          "vast_machine_id/paralon_node_id keys, not both")
    if legacy:
        entries = [{"vast_machine_id": raw.get("vast_machine_id"),
                    "paralon_node_id": raw.get("paralon_node_id")}]
    else:
        entries = raw.get("machines")
    if not isinstance(entries, list) or not entries:
        raise ConfigError(
            "config needs \"machines\": [{\"vast_machine_id\": ..., "
            "\"paralon_node_id\": \"...\"}] (run `init` to write one)")
    return entries


def parse_config(raw, env=None):
    env = os.environ if env is None else env
    if not isinstance(raw, dict):
        raise ConfigError("config must be a JSON object")

    policy = raw.get("on_paralon_rental")
    if policy not in POLICIES:
        raise ConfigError(
            "config must set \"on_paralon_rental\" to one of: "
            + " | ".join(POLICIES)
            + ". There is no default: decide what Vast should do while a "
            "Paralon renter is on the machine.\n"
            + "\n".join(f"  {k:<16} - {v}" for k, v in POLICY_HELP.items()))

    top_listing = _check_listing(raw.get("vast_listing", {}), "vast_listing")
    machines, seen_ids, seen_nodes = [], set(), set()
    for i, entry in enumerate(_machine_list(raw)):
        where = f"machines[{i}]"
        if not isinstance(entry, dict):
            raise ConfigError(f"{where} must be an object")
        unknown = sorted(k for k in entry if not k.startswith("_") and k not in
                         ("vast_machine_id", "paralon_node_id", "vast_listing",
                          "paralon_agent"))
        if unknown:
            raise ConfigError(f"unknown {where} keys: {', '.join(unknown)}")
        try:
            vid = int(entry.get("vast_machine_id"))
        except (TypeError, ValueError):
            raise ConfigError(f"{where}.vast_machine_id must be an integer")
        node = entry.get("paralon_node_id")
        if not node:
            raise ConfigError(f"{where}.paralon_node_id is missing")
        node = str(node)
        if vid in seen_ids or node in seen_nodes:
            raise ConfigError(f"{where}: machine {vid} or node {node} is "
                              "listed twice")
        seen_ids.add(vid)
        seen_nodes.add(node)
        listing = dict(top_listing)
        listing.update(_check_listing(entry.get("vast_listing", {}),
                                      f"{where}.vast_listing"))
        agent = entry.get("paralon_agent", "")
        if agent and (not isinstance(agent, str) or not valid_target(agent)):
            raise ConfigError(f"{where}.paralon_agent must be \"local\" or "
                              f"user@host for ssh (got {agent!r})")
        machines.append(Machine(vid, node, listing, agent or ""))

    known = set(Config.__dataclass_fields__) - {"paralon_api_key", "machines"}
    unknown = sorted(k for k in raw if k not in known and not k.startswith("_")
                     and k not in ("machines", "vast_machine_id",
                                   "paralon_node_id"))
    if unknown:
        raise ConfigError(f"unknown config keys: {', '.join(unknown)}")

    kwargs = {k: raw[k] for k in known if k in raw}
    for key in ("poll_interval_s", "paralon_min_get_interval_s",
                "paralon_max_calls_per_min", "reclaim_retry_s"):
        if key in kwargs and (isinstance(kwargs[key], bool)
                              or not isinstance(kwargs[key], int)
                              or kwargs[key] < 1):
            raise ConfigError(f"\"{key}\" must be a whole number >= 1 "
                              f"(got {kwargs[key]!r})")
    if "docker_events" in kwargs and not isinstance(kwargs["docker_events"], bool):
        raise ConfigError("\"docker_events\" must be true or false (no quotes)")
    for key in ("state_file", "vastai_bin", "paralon_base_url"):
        if key in kwargs and (not isinstance(kwargs[key], str) or not kwargs[key]):
            raise ConfigError(f"\"{key}\" must be a non-empty string")
    kwargs["vast_listing"] = top_listing
    cfg = Config(machines=machines, **kwargs)
    cfg.state_file = os.path.expanduser(cfg.state_file)
    cfg.paralon_api_key = env.get("PARALON_API_KEY", "").strip()
    if not cfg.paralon_api_key:
        raise ConfigError(
            "PARALON_API_KEY is not set (a prlc_... key with the "
            "\"My nodes\" permission)")
    if cfg.paralon_max_calls_per_min < 1:
        raise ConfigError("paralon_max_calls_per_min must be >= 1")
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
