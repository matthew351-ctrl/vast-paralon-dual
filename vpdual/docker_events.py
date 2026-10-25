"""Optional: wake the watcher immediately when a container starts.

Only useful when the watcher runs ON the GPU machine with access to the
Docker socket. It only READS `docker events`; it never touches a container.
Polling works without it.
"""

import logging
import subprocess
import threading
import time

log = logging.getLogger("vpdual.docker")

RESTART_DELAY_S = 30


def start_watcher(wake, popen=subprocess.Popen, sleep=time.sleep, retries=None):
    """Set the `wake` threading.Event on every container start. The stream
    ends when the Docker daemon restarts, so it is reopened after a pause
    (forever by default; `retries` bounds it for tests)."""
    cmd = ["docker", "events", "--filter", "type=container",
           "--filter", "event=start", "--format", "{{.ID}}"]

    def run():
        attempt = 0
        while retries is None or attempt <= retries:
            attempt += 1
            try:
                proc = popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             text=True)
            except OSError as exc:
                log.warning("docker events unavailable (%s); polling only", exc)
                return
            for line in proc.stdout:
                if line.strip():
                    log.info("container started; checking now")
                    wake.set()
            log.warning("docker events stream ended; reopening in %ss "
                        "(polling continues meanwhile)", RESTART_DELAY_S)
            sleep(RESTART_DELAY_S)

    thread = threading.Thread(target=run, name="docker-events", daemon=True)
    thread.start()
    return thread
