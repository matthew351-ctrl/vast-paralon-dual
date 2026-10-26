"""Stop the Paralon agent while a Vast renter has the GPU; start it after.

Paralon's agent keeps working on a PAUSED node: by itself it starts inference
workers, model downloads and GPU verification benchmarks. Beside a Vast renter
those take the renter's GPU memory, and when one is killed Paralon answers
with a fresh benchmark, which can mark the node "failed". With the agent
stopped, nothing on the machine can start them.

Docker on the GPU machine is reached directly ("local", when this tool runs
on that machine) or over ssh ("user@host": key login, a user in the docker
group). Only these commands are run:
  docker ps -a --no-trunc --format ...
  docker stop -t 10 <id> [<id> ...]
  docker start <id>

Only Paralon's containers are ever stopped or started: the agent (found by its
image, because it renames its own container when it updates) and the
containers it started. Never a Vast container, and never a Paralon rental.

It also reads the machine itself (`machine_state`): which containers run,
and whether the GPU is free (nvidia-smi: compute processes and memory in use).
Decisions come from that, with Vast's rental count only as a hint.

It also keeps Paralon's Jupyter image on the machine (`pin_jupyter`). Paralon
rents a node only while that image is present, and Vast's daemon deletes
images no container uses within minutes once images pass 10% of the disk. A
container that is created but never started (`docker create`) references the
image, so the delete is refused. Never pulls: if the image is absent, nothing.
"""

import logging
import re
import shlex
import subprocess
from dataclasses import dataclass

log = logging.getLogger("vpdual.agent")

AGENT_IMAGE = "paraloncloud/agent"
PARALON_IMAGE_PREFIX = "ghcr.io/paraloncloud/"
PARALON_NAME_PREFIX = "paralon-"
TYPE_LABEL = "paralon.type"
RENTAL = "rental"
STOP_GRACE_S = 10
# [user@]host -- never starting with "-", so it cannot be read as an ssh option.
SSH_TARGET_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]*(@[A-Za-z0-9_][A-Za-z0-9._:-]*)?$")
JUPYTER_IMAGE = PARALON_IMAGE_PREFIX + "jupyter:latest"
PIN_LABEL = "vpdual.pin=jupyter"
PIN_PREFIX = "vpdual-pin-jupyter-"
# One shell pass: pin the CURRENT image id, drop pins on older ids. `docker
# create` never starts the container; the inspect first means it never pulls.
PIN_SCRIPT = (
    f"id=$(docker image inspect -f '{{{{.Id}}}}' {JUPYTER_IMAGE} 2>/dev/null) "
    "|| { echo absent; exit 0; }; "
    "s=${id#sha256:}; s=$(printf %.12s \"$s\"); "
    f"n={PIN_PREFIX}$s; "
    "if docker ps -a --format '{{.Names}}' | grep -qx \"$n\"; then echo \"kept $s\"; "
    f"else docker create --name \"$n\" --label {PIN_LABEL} {JUPYTER_IMAGE} /bin/true "
    ">/dev/null && echo \"pinned $s\" || { echo failed; exit 1; }; fi; "
    f"for o in $(docker ps -a --filter label={PIN_LABEL} --format '{{{{.Names}}}}'); do "
    "[ \"$o\" = \"$n\" ] || { docker rm \"$o\" >/dev/null && echo \"removed $o\"; }; done"
)
PS_FORMAT = "{{.ID}}\t{{.Image}}\t{{.Names}}\t{{.State}}\t{{.Label \"%s\"}}" % TYPE_LABEL
# Vast names every rental's container C.<contract id> (Docker and VM rentals).
VAST_NAME_RE = re.compile(r"^C\.\d+$")
GPU_MARK = "---gpu---"
# One shell: containers, then the GPU. nvidia-smi missing or failing reads
# "gpu unreadable", never "free".
MACHINE_SCRIPT = (
    f"docker ps -a --no-trunc --format '{PS_FORMAT}' && echo {GPU_MARK} && "
    "if p=$(nvidia-smi --query-compute-apps=pid --format=csv,noheader 2>/dev/null) && "
    "m=$(nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits 2>/dev/null); "
    "then echo \"procs $(printf '%s\\n' \"$p\" | grep -c .)\"; "
    "echo \"mem $(printf '%s\\n' \"$m\" | awk '{s+=$1} END{print s+0}')\"; "
    "else echo 'gpu unreadable'; fi"
)


class AgentError(Exception):
    """Docker on the GPU machine could not be read or written."""


def valid_target(target):
    return target == "local" or bool(SSH_TARGET_RE.match(target or ""))


@dataclass
class Container:
    id: str
    image: str
    name: str
    running: bool
    paralon_type: str = ""

    @property
    def is_agent(self):
        return AGENT_IMAGE in self.image.lower()

    @property
    def is_paralon(self):
        return (self.image.lower().startswith(PARALON_IMAGE_PREFIX)
                or self.name.lower().startswith(PARALON_NAME_PREFIX))


@dataclass
class MachineState:
    """What runs on the GPU machine right now, read from the machine."""
    containers: list
    gpu_procs: object = None       # int, or None when nvidia-smi was unreadable
    gpu_mem_mib: object = None

    def _idle(self, c, idle_images):
        return any(c.image.lower().startswith(i.lower()) for i in idle_images)

    def vast_running(self, idle_images=()):
        """Names of running Vast rental containers (C.<id>), not counting the
        host's own idle job (`idle_job_images`)."""
        return [c.name for c in self.containers if c.running
                and VAST_NAME_RE.match(c.name) and not self._idle(c, idle_images)]

    def idle_job_running(self, idle_images=()):
        return any(c.running and self._idle(c, idle_images) for c in self.containers)

    def gpu_free(self, limit_mib, idle_images=()):
        """True: no compute process and memory near 0 (or only the host's idle
        job on it). False: busy. None: nvidia-smi unreadable."""
        if self.idle_job_running(idle_images):
            return True
        if self.gpu_procs is None or self.gpu_mem_mib is None:
            return None
        return self.gpu_procs == 0 and self.gpu_mem_mib <= limit_mib

    def gpu_text(self):
        if self.gpu_procs is None:
            return "GPU unreadable"
        return f"{self.gpu_procs} compute process(es), {self.gpu_mem_mib} MiB in use"


def parse_machine(text):
    ps, _sep, gpu = text.partition(GPU_MARK)
    procs = mem = None
    for line in gpu.splitlines():
        k, _, v = line.strip().partition(" ")
        if k in ("procs", "mem") and v.strip().isdigit():
            if k == "procs":
                procs = int(v)
            else:
                mem = int(v)
    if procs is None or mem is None:
        procs = mem = None
    return MachineState(parse_ps(ps), procs, mem)


def parse_ps(text):
    out = []
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) < 4 or not parts[0].strip():
            continue
        out.append(Container(id=parts[0].strip(), image=parts[1].strip(),
                             name=parts[2].strip(),
                             running=parts[3].strip().lower() == "running",
                             paralon_type=(parts[4].strip() if len(parts) > 4
                                           else "")))
    return out


def agent_of(containers):
    """The agent container: a running one if any, else the newest (docker ps
    lists newest first). None if the machine has no agent."""
    agents = [c for c in containers if c.is_agent]
    return next((c for c in agents if c.running), agents[0] if agents else None)


def leftovers(containers):
    """Running Paralon containers other than the agent and a rental: its
    inference workers, model downloads, benchmarks."""
    return [c for c in containers if c.running and c.is_paralon
            and not c.is_agent and c.paralon_type != RENTAL]


class DockerHost:
    def __init__(self, target, run=subprocess.run, timeout=60):
        if not valid_target(target):
            raise ValueError(f"not 'local' or user@host: {target!r}")
        self.target = target
        self._run = run
        self.timeout = timeout

    def _cmd(self, args):
        return self._remote("docker " + " ".join(shlex.quote(a) for a in args),
                            ["docker", *args])

    def _remote(self, shell, local_argv=None):
        if self.target == "local":
            return local_argv or ["sh", "-c", shell]
        return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                self.target, shell]

    def _exec(self, argv, what):
        try:
            res = self._run(argv, capture_output=True, text=True,
                            timeout=self.timeout)
        except (OSError, subprocess.SubprocessError) as exc:
            raise AgentError(f"{self.target}: {exc}") from exc
        if res.returncode != 0:
            err = (res.stderr or res.stdout or "").strip().splitlines()
            raise AgentError(f"{self.target}: {what} exited "
                             f"{res.returncode}: {err[-1] if err else 'no output'}")
        return res.stdout

    def _docker(self, *args):
        return self._exec(self._cmd(list(args)), f"docker {args[0]}")

    def pin_jupyter(self):
        """Pin Paralon's Jupyter image. Returns the script's lines: 'absent',
        'kept <id>' or 'pinned <id>', then any 'removed <old pin>'."""
        return [ln.strip() for ln in self._exec(self._remote(PIN_SCRIPT),
                                               "jupyter pin").splitlines() if ln.strip()]

    def containers(self):
        return parse_ps(self._docker("ps", "-a", "--no-trunc", "--format", PS_FORMAT))

    def machine_state(self):
        """Containers and GPU in one call (one ssh for a remote machine)."""
        return parse_machine(self._exec(self._remote(MACHINE_SCRIPT), "machine read"))

    def stop(self, ids):
        if ids:
            self._docker("stop", "-t", str(STOP_GRACE_S), *ids)

    def start(self, cid):
        self._docker("start", cid)
