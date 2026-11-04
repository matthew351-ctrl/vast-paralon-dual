"""A fixed, many-branch set of machines for `status`: the text-output pin and the web
UI tests both build it. Four machines and one removed from the config:

  111  dual, agent via user@gpu1, Vast unlisted by us, owed idle job, an
       ON-DEMAND node rented, a Paralon rental container on the machine
  222  vast mode, no agent, a Vast renter running, node available
  333  paralon mode, not in the Vast account (unreadable)
  444  no mode, no prices anywhere, node missing from GET /nodes
  999  removed from the config but still owed a relist

With names=True: 111 has a config `name`, 222 and 444 a Vast `hostname`
(444's needs tidying), 333 neither (and Vast cannot read it): the display
name chain config name -> Vast hostname -> Vast id.
"""

import os

from vpdual.config import Config, Machine
from vpdual.engine import Controller
from vpdual.state import StateStore

from helpers import FakeDocker, FakeParalon, FakeVast

KEY = "prlc_scenario_secret_key_never_shown"


def _decisions(n, start=0):
    return [{"t": f"2026-11-0{1 + (start + i) // 10}T0{(start + i) % 10}:00:00Z",
             "msg": f"decision {start + i}"} for i in range(n)]


NAME_111 = "GPU host A"


def build_scenario(tmp, *, vast_fail=False, paralon_fail=False,
                   docker_fail=False, dry_run=False, names=False):
    state_path = os.path.join(tmp, "state.json")
    listing = {"price_gpu": 0.5, "price_min_bid": 0.3, "duration": "14 days"}
    cfg = Config(
        machines=[Machine(111, "node-1", dict(listing), "user@gpu1",
                          NAME_111 if names else ""),
                  Machine(222, "node-2", dict(listing), ""),
                  Machine(333, "node-3", dict(listing), ""),
                  Machine(444, "node-4", {}, "")],
        on_paralon_rental="unlist_vast", vast_listing=dict(listing),
        state_file=state_path, paralon_api_key=KEY,
        idle_job_images=["me/miner"])
    decisions_111 = _decisions(6) + [
        {"t": "2026-11-03T12:00:00Z", "msg": "hold: waiting", "count": 3}]
    StateStore(state_path).save({"machines": {
        "111": {"mode": "dual",
                "took_off": {"vast": True, "paralon": False, "agent": False},
                "saved_listing": {"price_gpu": 0.45, "price_min_bid": 0.35,
                                  "price_disk": 0.2, "end_date": 5000.0},
                "idle_job": [{"kind": "vast", "id": 777, "name": "C.777"}],
                "decisions": decisions_111},
        "222": {"mode": "vast", "decisions": _decisions(2, 20)},
        "333": {"mode": "paralon"},
        "444": {},
        "999": {"mode": "dual",
                "took_off": {"vast": True, "paralon": False, "agent": False}},
    }})
    vast = FakeVast(listed=False).add(222, running=1).add(444)
    vast.add_job(777, status="stopped")
    if names:
        vast.machines[111]["hostname"] = "ignored-config-name-wins"
        vast.machines[222]["hostname"] = "gpu-host-b"
        vast.machines[444]["hostname"] = "  gpu\tbox\n4 \u200b"
    vast.fail_read = vast_fail
    paralon = FakeParalon(available=False, rented=True, spot=False)
    paralon.add("node-2").add("node-3")
    paralon.fail_read = paralon_fail
    docker = FakeDocker().add("r1", "ghcr.io/paraloncloud/x:1",
                              "paralon-rental-x", paralon_type="rental")
    docker.add("j1", "me/miner:1", "C.777", running=False)
    docker.gpu_procs, docker.gpu_mem_mib = 1, 2048
    docker.fail_read = docker_fail
    ctl = Controller(cfg, vast, paralon, StateStore(state_path),
                     dry_run=dry_run, clock=lambda: 1000.0, docker=docker)
    return cfg, ctl, vast, paralon, docker
