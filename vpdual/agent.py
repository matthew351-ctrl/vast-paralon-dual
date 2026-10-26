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
PS_FORMAT = "{{.ID}}\t{{.Image}}\t{{.Names}}\t{{.State}}\t{{.Label \"%s\"}}" % TYPE_LABEL


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
        if self.target == "local":
            return ["docker", *args]
        return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                self.target, "docker " + " ".join(shlex.quote(a) for a in args)]

    def _docker(self, *args):
        try:
            res = self._run(self._cmd(list(args)), capture_output=True,
                            text=True, timeout=self.timeout)
        except (OSError, subprocess.SubprocessError) as exc:
            raise AgentError(f"{self.target}: {exc}") from exc
        if res.returncode != 0:
            err = (res.stderr or res.stdout or "").strip().splitlines()
            raise AgentError(f"{self.target}: docker {args[0]} exited "
                             f"{res.returncode}: {err[-1] if err else 'no output'}")
        return res.stdout

    def containers(self):
        return parse_ps(self._docker("ps", "-a", "--no-trunc", "--format", PS_FORMAT))

    def stop(self, ids):
        if ids:
            self._docker("stop", "-t", str(STOP_GRACE_S), *ids)

    def start(self, cid):
        self._docker("start", cid)
