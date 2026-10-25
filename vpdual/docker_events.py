"""Optional: wake the watcher immediately when a container starts.

Only useful when the watcher runs ON the GPU machine with access to the
Docker socket. It only READS `docker events`; it never touches a container.
Polling works without it.
"""

import logging
import subprocess
import threading

log = logging.getLogger("vpdual.docker")


def start_watcher(wake, popen=subprocess.Popen):
    """Set the `wake` threading.Event on every container start."""
    cmd = ["docker", "events", "--filter", "type=container",
           "--filter", "event=start", "--format", "{{.ID}}"]

    def run():
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
        log.warning("docker events stream ended; polling only")

    thread = threading.Thread(target=run, name="docker-events", daemon=True)
    thread.start()
    return thread
