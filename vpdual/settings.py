"""The web UI's Settings tab: read, check and save the config file.

Checking runs the candidate through `check_config`, the validator
`load_config` itself uses, then three rules the file alone cannot know:
  - a machine this tool still owes something (a relist, an un-pause, the
    agent's or the idle job's start; see the state file) cannot be removed;
  - its Paralon node id cannot change while it owes Paralon an un-pause;
  - `state_file` cannot change while anything is owed (the owed flags live
    in the old file and would be forgotten).
Nothing is saved when any check fails.

Saving keeps what the user had: `_`-prefixed keys (comments, custom data)
the page did not send back are restored (machines are matched by Vast id),
and the file's key order and indent are kept. The old file is copied to
`config.json.bak-YYYYmmdd-HHMMSS` (the last KEEP_BACKUPS are kept), then the
new one is written atomically (temp file, fsync, os.replace) with mode 600.
A save is refused when the file changed on disk since the page loaded it.

The Paralon API key is never read into, or returned by, any of this: only
whether it is set and where it is read from. It is changed with `init` or
by editing the `env` file next to the config.

The `run` service loads the config once, when it starts: a saved config
takes effect when it is restarted.
"""

import hashlib
import json
import os
import re
import shutil
import tempfile
import time
from dataclasses import MISSING

from .config import (DEFAULT_STATE, INT_SETTINGS, LISTING_FLAGS, NUMERIC_KEYS,
                     POLICIES, Config, check_config, key_from_env_file)
from .install import env_file_for
from .state import StateStore, owes_something

KEEP_BACKUPS = 10
MAX_CONFIG_BODY = 65536
RESTART_COMMAND = "sudo systemctl restart vast-paralon-dual"
_BACKUP_RE = re.compile(r"\.bak-\d{8}-\d{6}(-\d+)?$")

# Settings shown, by group (every user-settable Config key; the key and the
# machines list are handled on their own).
GROUPS = [
    ("General", ["on_paralon_rental", "poll_interval_s", "fast_poll_s",
                 "docker_events", "gpu_free_mib", "idle_job_images"]),
    ("Vast", ["vastai_bin"]),
    ("Paralon", ["paralon_base_url", "paralon_min_get_interval_s",
                 "paralon_max_calls_per_min", "reclaim_retry_s",
                 "jupyter_refresh_s"]),
    ("Files", ["state_file"]),
]
UNITS = {"poll_interval_s": "s", "fast_poll_s": "s", "reclaim_retry_s": "s",
         "jupyter_refresh_s": "s", "paralon_min_get_interval_s": "s",
         "paralon_max_calls_per_min": "calls/min", "gpu_free_mib": "MiB"}
LISTING_UNITS = {"price_gpu": "$/h", "price_min_bid": "$/h",
                 "price_disk": "$/GB/month", "price_inetu": "$/GB",
                 "price_inetd": "$/GB", "discount_rate": "0-1",
                 "min_chunk": "GPUs", "end_date": "unix time",
                 "duration": "e.g. 14 days", "vol_size": "GB",
                 "vol_price": "$/GB/month"}
LISTING_INTS = ("min_chunk", "vol_size", "end_date")


def schema():
    """What the page needs to draw a typed input per setting."""
    fields = Config.__dataclass_fields__

    def default(key):
        f = fields[key]
        return f.default if f.default is not MISSING else f.default_factory()

    def kind(key):
        if key == "on_paralon_rental":
            return "select"
        t = fields[key].type
        return {bool: "bool", list: "list"}.get(t) or \
            ("int" if key in INT_SETTINGS else "text")

    groups = [{"title": title, "fields": [
        {"key": k, "type": kind(k), "default": default(k),
         "unit": UNITS.get(k, ""),
         "options": list(POLICIES) if k == "on_paralon_rental" else None}
        for k in keys]} for title, keys in GROUPS]
    listing = [{"key": k,
                "type": "number" if k in NUMERIC_KEYS else
                        "int" if k in LISTING_INTS else "text",
                "unit": LISTING_UNITS.get(k, "")} for k in LISTING_FLAGS]
    return {"groups": groups, "listing": listing}


def shown_keys():
    return {f["key"] for g in schema()["groups"] for f in g["fields"]}


# -- reading -----------------------------------------------------------
def read_raw(path):
    """(raw dict, sha256 of the bytes on disk)."""
    with open(path, "rb") as fh:
        data = fh.read()
    raw = json.loads(data.decode("utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("the config file is not a JSON object")
    return raw, hashlib.sha256(data).hexdigest()


def normalized(raw):
    """The old single-machine keys as a machines list, for the editor."""
    if "machines" in raw or not ("vast_machine_id" in raw
                                 or "paralon_node_id" in raw):
        return raw
    out = {k: v for k, v in raw.items()
           if k not in ("vast_machine_id", "paralon_node_id")}
    out["machines"] = [{"vast_machine_id": raw.get("vast_machine_id"),
                        "paralon_node_id": raw.get("paralon_node_id")}]
    return out


def key_status(path, environ=None):
    """Whether PARALON_API_KEY is set and where it is read from. Never the
    key itself."""
    environ = os.environ if environ is None else environ
    env_file = env_file_for(path)
    if (environ.get("PARALON_API_KEY") or "").strip():
        return {"set": True, "source": "the PARALON_API_KEY environment "
                "variable", "env_file": env_file}
    if key_from_env_file(path):
        return {"set": True, "source": env_file, "env_file": env_file}
    return {"set": False, "source": "", "env_file": env_file}


def _state_path(raw, fallback):
    sf = raw.get("state_file") if isinstance(raw, dict) else None
    return os.path.expanduser(sf) if isinstance(sf, str) and sf else \
        (fallback or os.path.expanduser(DEFAULT_STATE))


def owed_machines(state_file):
    """{vast id: what this tool owes it} from the state file."""
    try:
        machines = StateStore(state_file).load()["machines"]
    except (OSError, ValueError):
        return {}
    out = {}
    for mid, st in machines.items():
        if owes_something(st):
            what = [k for k, v in st["took_off"].items() if v]
            what = [{"vast": "a Vast relist", "paralon": "a Paralon un-pause",
                     "agent": "the Paralon agent's start"}[k] for k in what]
            if st.get("idle_job"):
                what.append("the idle job's start")
            out[str(mid)] = {"took_off": dict(st["took_off"]),
                             "what": ", ".join(what)}
    return out


# -- checking ----------------------------------------------------------
def _merge(old, new, redact, top=False):
    """`new` as the file will hold it:
      - `_` keys (comments, custom data) are ALWAYS the file's own, in
        their place: the page cannot edit them, and what it got back may
        have been redacted;
      - a string the page returns as exactly the redacted form of the
        file's value is the file's value (the page never saw the original);
      - the file's key order is kept; machines are matched by Vast id."""
    if isinstance(old, dict) and isinstance(new, dict):
        out = {}
        for k, v in old.items():
            if k.startswith("_"):
                out[k] = v
            elif k in new:
                out[k] = (_machines(v, new[k], redact)
                          if top and k == "machines" else
                          _merge(v, new[k], redact))
        for k, v in new.items():
            if k not in out and not k.startswith("_"):
                out[k] = v
        return out
    if isinstance(old, list) and isinstance(new, list):
        return [_merge(old[i], v, redact) if i < len(old) else v
                for i, v in enumerate(new)]
    if (redact is not None and isinstance(old, str) and isinstance(new, str)
            and new != old and new == redact(old)):
        return old
    return new


def _machines(old, new, redact):
    if not (isinstance(old, list) and isinstance(new, list)):
        return new
    by_id = {str(e.get("vast_machine_id")): e for e in old
             if isinstance(e, dict)}
    return [_merge(by_id.get(str(e.get("vast_machine_id"))), e, redact)
            if isinstance(e, dict) else e for e in new]


def merge_custom(old, new, redact=None):
    return _merge(old, new, redact, top=True)


def _flat(raw):
    out = {}
    if not isinstance(raw, dict):
        return out

    def listing(prefix, value):
        if isinstance(value, dict):
            for k, v in value.items():
                if not k.startswith("_"):
                    out[f"{prefix}.{k}"] = v
        else:
            out[prefix] = value

    for k, v in raw.items():
        if k.startswith("_"):
            continue
        if k == "machines" and isinstance(v, list):
            for i, e in enumerate(v):
                if not isinstance(e, dict):
                    out[f"machine #{i + 1}"] = e
                    continue
                label = f"machine {e.get('vast_machine_id')}"
                for mk, mv in e.items():
                    if mk.startswith("_"):
                        continue
                    if mk == "vast_listing":
                        listing(f"{label}.vast_listing", mv)
                    else:
                        out[f"{label}.{mk}"] = mv
        elif k == "vast_listing":
            listing("vast_listing", v)
        else:
            out[k] = v
    return out


def diff(old, new):
    """[{path, kind: added|removed|changed, old, new}] over the settings
    (not the `_` keys, which are kept as they were)."""
    a, b = _flat(old), _flat(new)
    rows = []
    for path in list(a) + [p for p in b if p not in a]:
        if path not in b:
            rows.append({"path": path, "kind": "removed", "old": a[path],
                         "new": None})
        elif path not in a:
            rows.append({"path": path, "kind": "added", "old": None,
                         "new": b[path]})
        elif a[path] != b[path] or type(a[path]) is not type(b[path]):
            rows.append({"path": path, "kind": "changed", "old": a[path],
                         "new": b[path]})
    return rows


def _ids(raw):
    out = {}
    for i, e in enumerate(raw.get("machines") or [] if isinstance(raw, dict)
                          else []):
        if isinstance(e, dict):
            out[str(e.get("vast_machine_id"))] = (i, e)
    return out


def check(old_raw, candidate, *, key, fallback_state=None, redact=None):
    """(merged candidate, errors [(field, msg)], diff rows, Config|None).
    `redact`: what the page's copy went through, so it can be undone."""
    if not isinstance(candidate, dict):
        return candidate, [("", "send the config as a JSON object")], [], None
    merged = merge_custom(old_raw, candidate, redact)
    cfg, errors = check_config(merged, env={"PARALON_API_KEY": key or ""})
    state_file = _state_path(old_raw, fallback_state)
    owed = owed_machines(state_file)
    old_ids, new_ids = _ids(old_raw), _ids(merged)
    for mid, (_, entry) in old_ids.items():
        if mid in owed and mid not in new_ids:
            label = (f"{entry['name']} (Vast {mid})" if entry.get("name")
                     else f"Machine {mid}")
            errors.append((
                "machines",
                f"{label} cannot be removed: this tool still "
                f"owes it {owed[mid]['what']}. Let the service put it back "
                "first (for a relist, switch it to vast or dual), then "
                "remove it."))
    for mid, (i, entry) in new_ids.items():
        old = old_ids.get(mid)
        if (old and mid in owed and owed[mid]["took_off"].get("paralon")
                and old[1].get("paralon_node_id") != entry.get("paralon_node_id")):
            errors.append((
                f"machines[{i}].paralon_node_id",
                f"machine {mid} still owes Paralon node "
                f"{old[1].get('paralon_node_id')} an un-pause: its node id "
                "cannot change until that is done"))
    if owed and _state_path(merged, fallback_state) != state_file:
        errors.append((
            "state_file",
            "state_file cannot change while this tool owes machines "
            f"something ({', '.join(sorted(owed))}): those debts live in "
            f"{state_file} and the new file would forget them"))
    return merged, errors, diff(old_raw, merged), (None if errors else cfg)


# -- saving ------------------------------------------------------------
def _indent(text):
    for line in text.splitlines()[1:]:
        stripped = line.lstrip(" ")
        if stripped and len(stripped) < len(line):
            return len(line) - len(stripped)
    return 2


def backup(path, now=None):
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
    dest, n = f"{path}.bak-{stamp}", 1
    while os.path.exists(dest):
        n += 1
        dest = f"{path}.bak-{stamp}-{n}"
    shutil.copy2(path, dest)
    os.chmod(dest, 0o600)
    directory = os.path.dirname(os.path.abspath(path))
    base = os.path.basename(path)
    ours = sorted(f for f in os.listdir(directory)
                  if f.startswith(base + ".bak-")
                  and _BACKUP_RE.search(f[len(base):]))
    for old in ours[:-KEEP_BACKUPS]:
        try:
            os.unlink(os.path.join(directory, old))
        except OSError:
            pass
    return dest


def write_atomic(path, raw, indent=2):
    directory = os.path.dirname(os.path.abspath(path))
    text = json.dumps(raw, indent=indent, ensure_ascii=False) + "\n"
    fd, tmp = tempfile.mkstemp(prefix=".config-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fchmod(fh.fileno(), 0o600)
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    try:
        dfd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass


def save(path, merged, now=None):
    """Back up the file, then write `merged` in its place (same indent)."""
    with open(path, encoding="utf-8") as fh:
        indent = _indent(fh.read())
    dest = backup(path, now)
    write_atomic(path, merged, indent)
    return dest
