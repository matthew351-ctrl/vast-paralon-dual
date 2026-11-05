"""The notification hook (v0.8.0): config `notify_command`.

Run (through the shell, as the user that runs this tool) for every
WARNING-level decision and every confirmed side change -- a read that shows
Vast listed / unlisted or Paralon available / paused where the last read
showed the opposite, whoever made the change. The message is on stdin; the
environment adds VPDUAL_MACHINE (the Vast id), VPDUAL_LEVEL (WARNING | INFO)
and VPDUAL_EVENT (a short kind, e.g. vast_unlisted, relist_held).

One notification per machine per event kind per RATE_S; a held relist
(no price to relist with) repeats every HELD_RELIST_S until it clears. The
command runs on its own thread with a TIMEOUT_S limit, so a slow or failing
command never holds a pass; a failure is logged at most once per RATE_S.
"""

import logging
import os
import re
import subprocess
import threading
import time

log = logging.getLogger("vpdual")

TIMEOUT_S = 20
RATE_S = 600
HELD_RELIST_S = 3600
RELIST_HELD = "relist_held"
SIDE_EVENTS = {("vast", True): "vast_listed", ("vast", False): "vast_unlisted",
               ("paralon", True): "paralon_available",
               ("paralon", False): "paralon_paused"}


def event_kind(msg):
    """A warning's kind for the rate limit: its first words, without the
    numbers and ids that change from pass to pass."""
    if msg.startswith("hold: cannot relist Vast"):
        return RELIST_HELD
    words = re.sub(r"\(.*|[^a-z ]", " ", msg.lower()).split()
    return "_".join(words[:4]) or "warning"


def _background(fn):
    threading.Thread(target=fn, name="vpdual-notify", daemon=True).start()


class Notifier:
    def __init__(self, command, *, clock=time.time, runner=subprocess.run,
                 spawn=_background):
        self.command = command
        self.clock = clock
        self.runner = runner
        self.spawn = spawn
        self._sent = {}              # (machine id, kind) -> when sent
        self._last_seen = {}         # (machine id, side) -> up?
        self._last_failure_log = None
        self._lock = threading.Lock()

    def warning(self, machine_id, msg):
        self.send(machine_id, "WARNING", event_kind(msg), msg)

    def seen(self, machine_id, side, up, label=""):
        """A read showed `side` up or down: notify on a change."""
        key = (machine_id, side)
        before = self._last_seen.get(key)
        self._last_seen[key] = bool(up)
        if side == "vast" and up:
            with self._lock:          # a held relist is over: a new one tells at once
                self._sent.pop((machine_id, RELIST_HELD), None)
        if before is None or before == bool(up):
            return
        kind = SIDE_EVENTS[(side, bool(up))]
        self.send(machine_id, "INFO", kind,
                  f"{label or machine_id}: {kind.replace('_', ' ')}")

    def send(self, machine_id, level, kind, msg):
        now = self.clock()
        every = HELD_RELIST_S if kind == RELIST_HELD else RATE_S
        with self._lock:
            last = self._sent.get((machine_id, kind))
            if last is not None and now - last < every:
                return False
            self._sent[(machine_id, kind)] = now
        env = dict(os.environ, VPDUAL_MACHINE=str(machine_id),
                   VPDUAL_LEVEL=level, VPDUAL_EVENT=kind)
        self.spawn(lambda: self._run(msg, env))
        return True

    def _run(self, msg, env):
        try:
            res = self.runner(self.command, shell=True, input=msg + "\n",
                              env=env, capture_output=True, text=True,
                              timeout=TIMEOUT_S)
            if res.returncode == 0:
                return
            why = f"exited {res.returncode}"
        except Exception as exc:     # never into the pass
            why = str(exc)
        now = self.clock()
        if self._last_failure_log is None or now - self._last_failure_log >= RATE_S:
            self._last_failure_log = now
            log.warning("notify_command failed (%s); notifications continue, "
                        "this is logged at most every %d min", why, RATE_S // 60)
