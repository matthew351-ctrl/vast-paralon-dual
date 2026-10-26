"""Fakes for the two sides. They record every read and write."""

import json
import os
import shutil
import tempfile

from vpdual.agent import AgentError, Container, MachineState
from vpdual.config import Config, Machine
from vpdual.paralon import ParalonError, ParalonNode
from vpdual.state import StateStore
from vpdual.vast import VastError, VastMachine

HERE = os.path.dirname(os.path.abspath(__file__))


def fixture(name):
    with open(os.path.join(HERE, name), encoding="utf-8") as fh:
        return json.load(fh)


# The listing terms a listed fake machine reports, as `show machines --raw`.
DEFAULT_TERMS = {"listed_gpu_cost": 0.45, "min_bid_price": 0.35,
                 "listed_storage_cost": 0.2,
                 "listed_inet_up_cost": 4.0 / 1024,
                 "listed_inet_down_cost": 3.0 / 1024,
                 "end_date": 5000.0}


class FakeVast:
    """Machine 111 by default; `add()` more. `v.listed` etc. proxy 111."""

    def __init__(self, listed=True, running=0, resident=0, terms=None):
        self.machines = {}
        self.add(111, listed, running, resident, terms)
        self.fail_read = False
        self.fail_write = False
        self.reads = 0
        self.writes = []

    def add(self, mid, listed=True, running=0, resident=0, terms=None):
        self.machines[mid] = {
            "id": mid, "listed": listed, "current_rentals_running": running,
            "current_rentals_resident": resident,
            **(DEFAULT_TERMS if terms is None else terms)}
        return self

    def _p(name):  # noqa: N805 - property factory for machine 111
        key = {"listed": "listed", "running": "current_rentals_running",
               "resident": "current_rentals_resident"}[name]
        return property(lambda s: s.machines[111][key],
                        lambda s, v: s.machines[111].__setitem__(key, v))
    listed, running, resident = _p("listed"), _p("running"), _p("resident")

    def read_machines(self):
        self.reads += 1
        if self.fail_read:
            raise VastError("cli error")
        return {mid: VastMachine.from_raw(dict(raw))
                for mid, raw in self.machines.items()}

    def list_machine(self, machine_id, listing):
        self.writes.append(("list", machine_id, dict(listing)))
        if self.fail_write:
            raise VastError("list failed")
        self.machines[machine_id]["listed"] = True

    def unlist_machine(self, machine_id):
        self.writes.append(("unlist", machine_id))
        if self.fail_write:
            raise VastError("unlist failed")
        self.machines[machine_id]["listed"] = False


class FakeParalon:
    """Node "node-1" by default; `add()` more. `p.available` etc. proxy it."""

    def __init__(self, available=True, rented=False, spot=True):
        self.nodes = {}
        self.add("node-1", available, rented, spot)
        self.fail_read = False
        self.reads = 0
        self.writes = []

    def add(self, nid, available=True, rented=False, spot=True):
        self.nodes[nid] = {"available": available, "rented": rented,
                           "spot": spot}
        return self

    def _p(name):  # noqa: N805
        return property(lambda s: s.nodes["node-1"][name],
                        lambda s, v: s.nodes["node-1"].__setitem__(name, v))
    available, rented, spot = _p("available"), _p("rented"), _p("spot")

    def list_nodes(self, refresh=False):
        self.reads += 1
        self.refreshes = getattr(self, "refreshes", 0) + bool(refresh)
        if self.fail_read:
            raise ParalonError("HTTP 503")
        return self._build()

    def _build(self):
        out = []
        for nid, n in self.nodes.items():
            out.append(ParalonNode(
                id=nid, state="rented" if n["rented"] else
                ("available" if n["available"] else "paused"),
                available=n["available"], spot=n["spot"], price=1.0,
                rental={"status": "running"} if n["rented"] else None,
                raw={"name": nid, "gpus": [{"model": "RTX 4090"}],
                     "online": n.get("online", True)}))
        return out

    def cached_node(self, node_id):
        # like the real client: the last read, no API call (not counted)
        for n in self._build():
            if n.id == node_id:
                return n
        return None

    def set_available(self, node_id, available):
        self.writes.append(("available", node_id, available))
        self.nodes[node_id]["available"] = available

    def reclaim(self, node_id):
        self.writes.append(("reclaim", node_id))


class FakeDocker:
    """Docker on one GPU machine: an agent plus whatever is `add`ed. Call the
    instance with a target to get it back, as the Controller's `docker`."""

    def __init__(self, agent=True, agent_running=True):
        self.containers_ = []
        self.fail_read = False
        self.fail_write = False
        self.targets = []
        self.writes = []
        self.reads = 0
        self.pins = 0
        self.pin_lines = ["kept 526048b557bf"]
        self.fail_pin = False
        self.gpu_procs, self.gpu_mem_mib = 0, 0     # free unless a test says not
        self.machine_reads = 0
        if agent:
            self.add("agent1", "ghcr.io/paraloncloud/agent:latest",
                     "paralon-agent", agent_running)

    def add(self, cid, image, name, running=True, paralon_type=""):
        self.containers_.append(Container(cid, image, name, running,
                                          paralon_type))
        return self

    def __call__(self, target):
        self.targets.append(target)
        return self

    def get(self, cid):
        return next(c for c in self.containers_ if c.id == cid)

    def pin_jupyter(self):
        self.pins += 1
        if self.fail_pin:
            raise AgentError("ssh: connect timed out")
        return list(self.pin_lines)

    def machine_state(self):
        self.machine_reads += 1
        if self.fail_read:
            raise AgentError("ssh: connect timed out")
        return MachineState([Container(**vars(c)) for c in self.containers_],
                            self.gpu_procs, self.gpu_mem_mib)

    def containers(self):
        self.reads += 1
        if self.fail_read:
            raise AgentError("ssh: connect timed out")
        return [Container(**vars(c)) for c in self.containers_]

    def stop(self, ids):
        self.writes.append(("stop", list(ids)))
        if self.fail_write:
            raise AgentError("docker stop exited 1")
        for cid in ids:
            self.get(cid).running = False

    def kill(self, ids):
        self.writes.append(("kill", list(ids)))
        if self.fail_write:
            raise AgentError("docker kill exited 1")
        for cid in ids:
            self.get(cid).running = False

    def start(self, cid):
        self.writes.append(("start", cid))
        if self.fail_write:
            raise AgentError("docker start exited 1")
        self.get(cid).running = True


def make_config(state_file, policy="keep_vast_listed", machines=None,
                listing=None, agent=""):
    listing = {"price_gpu": 0.5, "price_min_bid": 0.3, "duration": "14 days"} \
        if listing is None else listing
    machines = machines or [(111, "node-1")]
    return Config(
        machines=[Machine(vid, nid, dict(listing), agent)
                  for vid, nid in machines],
        on_paralon_rental=policy, vast_listing=dict(listing),
        state_file=state_file, paralon_api_key="prlc_test_secret")


class TempDirMixin:
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.state_path = os.path.join(self.tmp, "state.json")
        self.store = StateStore(self.state_path)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def mstate(self, mid=111):
        return StateStore(self.state_path).load([mid])["machines"][str(mid)]
