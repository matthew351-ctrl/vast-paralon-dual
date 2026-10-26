"""React to containers starting on a GPU machine, in seconds instead of at the
next pass.

One watcher per machine the tool can reach (`paralon_agent`: "local", or
user@host over ssh). It streams `docker events` for container starts and, for
each one, calls `on_start(name, image)` -- the controller pauses Paralon at
once for a non-Paralon container -- then wakes the main loop for a full pass.
It only READS `docker events`; it never touches a container itself.
"""

import logging
import subprocess
import threading
import time

log = logging.getLogger("vpdual.docker")

RESTART_DELAY_S = 30
EVENTS_ARGS = ["events", "--filter", "type=container", "--filter", "event=start",
               "--format", "{{.Actor.Attributes.name}}\t{{.Actor.Attributes.image}}"]


def events_cmd(target="local"):
    if target == "local":
        return ["docker", *EVENTS_ARGS]
    remote = "docker " + " ".join(f"'{a}'" for a in EVENTS_ARGS)
    return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
            "-o", "ServerAliveInterval=30", target, remote]


def start_watcher(wake, popen=subprocess.Popen, sleep=time.sleep, retries=None,
                  target="local", on_start=None):
    """Watch `target` for container starts: call on_start(name, image) for
    each, then set `wake`. The stream ends when the Docker daemon restarts or
    the ssh link drops, so it is reopened after a pause (forever by default;
    `retries` bounds it for tests)."""
    cmd = events_cmd(target)

    def run():
        attempt = 0
        while retries is None or attempt <= retries:
            attempt += 1
            try:
                proc = popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             text=True)
            except OSError as exc:
                log.warning("docker events on %s unavailable (%s); polling only",
                            target, exc)
                return
            for line in proc.stdout:
                if not line.strip():
                    continue
                name, _, image = line.rstrip("\n").partition("\t")
                log.info("%s: container %s started; checking now", target, name)
                if on_start is not None:
                    try:
                        on_start(name, image)
                    except Exception:            # never kill the watcher
                        log.exception("%s: handling the start of %s failed", target, name)
                wake.set()
            log.warning("docker events on %s ended; reopening in %ss (polling "
                        "continues meanwhile)", target, RESTART_DELAY_S)
            sleep(RESTART_DELAY_S)

    thread = threading.Thread(target=run, name=f"docker-events-{target}", daemon=True)
    thread.start()
    return thread
