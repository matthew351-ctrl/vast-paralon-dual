"""A small JSON state file, written atomically.

Per Vast machine it records the mode, which side THIS tool took off, and the
listing terms the Vast machine had just before the tool unlisted it, so a
restart never forgets to relist something (and never relists something it did
not unlist), and a relist restores the machine's own prices.

    {"machines": {"123": {"mode": "dual",
                          "took_off": {"vast": false, "paralon": false},
                          "saved_listing": null,
                          "reclaim_sent_at": null,
                          "decisions": [...]}}}
"""

import json
import os
import tempfile

MODES = ("vast", "paralon", "dual")
MAX_DECISIONS = 30


def default_machine_state():
    return {
        "mode": None,
        # True = this tool took that side off and owes it a relist.
        "took_off": {"vast": False, "paralon": False},
        # The Vast listing terms saved just before the tool unlisted it.
        "saved_listing": None,
        "reclaim_sent_at": None,
        "decisions": [],
    }


def _clean(saved):
    st = default_machine_state()
    if not isinstance(saved, dict):
        return st
    if saved.get("mode") in MODES:
        st["mode"] = saved["mode"]
    took = saved.get("took_off") or {}
    st["took_off"] = {"vast": bool(took.get("vast")),
                      "paralon": bool(took.get("paralon"))}
    if isinstance(saved.get("saved_listing"), dict) and saved["saved_listing"]:
        st["saved_listing"] = dict(saved["saved_listing"])
    if isinstance(saved.get("reclaim_sent_at"), (int, float)):
        st["reclaim_sent_at"] = saved["reclaim_sent_at"]
    if isinstance(saved.get("decisions"), list):
        st["decisions"] = saved["decisions"][-MAX_DECISIONS:]
    return st


def owes_something(mstate):
    return any(mstate["took_off"].values())


class StateStore:
    def __init__(self, path):
        self.path = path

    def load(self, machine_ids=()):
        ids = [str(i) for i in machine_ids]
        try:
            with open(self.path, encoding="utf-8") as fh:
                saved = json.load(fh)
        except FileNotFoundError:
            saved = {}
        if not isinstance(saved, dict):
            raise ValueError(f"state file {self.path} is not a JSON object")
        if "machines" not in saved and ("took_off" in saved or "mode" in saved):
            saved = self._migrate(saved, ids)
        machines = saved.get("machines") or {}
        if not isinstance(machines, dict):
            raise ValueError(f"state file {self.path}: bad \"machines\"")
        # Keep machines no longer in the config: an owed relist is not lost.
        state = {"machines": {k: _clean(v) for k, v in machines.items()}}
        for i in ids:
            state["machines"].setdefault(i, default_machine_state())
        return state

    def _migrate(self, old, ids):
        """Read a v0.1 single-machine state file."""
        st = _clean(old)
        if len(ids) == 1:
            return {"machines": {ids[0]: st}}
        if owes_something(st):
            raise ValueError(
                f"state file {self.path} is from the single-machine version "
                f"and still owes a relist ({st['took_off']}), but the config "
                f"now has {len(ids)} machines. Relist by hand or run once "
                "with only the original machine in the config.")
        return {"machines": {}}

    def save(self, state):
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".state-", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(state, fh, indent=2, sort_keys=True)
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
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
