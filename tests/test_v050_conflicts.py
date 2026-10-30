"""v0.5.0: a Vast renter and a Paralon renter never share the GPU
(2026-10-30).

1. Any STORED Vast instance keeps Paralon paused, spot or on-demand.
3. A Paralon rental container starting unlists Vast at once (dual +
   unlist_vast), before any pass sees node.rented.
4. The host's idle job never runs under a Paralon rental: a Vast default job
   is stopped/started through Vast, a plain container through Docker.
5. After a Paralon rental, Vast is relisted only once the rental's container
   is gone and the GPU is free; the idle job restarts after the relist."""

import json
import subprocess
import unittest

from vpdual.engine import Controller
from vpdual.vast import VastCLI, VastError

from helpers import FakeDocker, FakeParalon, FakeVast, TempDirMixin, make_config

OFF = ("available", "node-1", False)
ON = ("available", "node-1", True)
RECLAIM = ("reclaim", "node-1")
UNLIST = ("unlist", 111)
TARGET = "user@gpu1"
RENTAL = ("r1", "ghcr.io/paraloncloud/jupyter:latest", "jupyter-abc", True, "rental")
JOB = ("j1", "me/miner:1", "C.777")          # the host's Vast default job
PLAIN = ("m1", "me/miner:2", "my-miner")     # the host's plain Docker idle job


def lists(v):
    return [w for w in v.writes if w[0] == "list"]


class Base(TempDirMixin, unittest.TestCase):
    def ctl(self, vast, paralon, docker=None, policy="unlist_vast", mode="dual",
            agent=TARGET, idle=("me/miner",)):
        cfg = make_config(self.state_path, policy, agent=agent)
        cfg.idle_job_images = list(idle)
        c = Controller(cfg, vast, paralon, self.store, clock=lambda: 1000.0,
                       docker=docker if docker is not None else FakeDocker())
        c.set_mode(mode)
        return c

    def said(self, c):
        return " | ".join(msg for _, msg in c.pass_decisions)


# -- fix 1 ----------------------------------------------------------------
class Fix1StoredInstanceKeepsParalonPaused(Base):
    def test_spot_node_paused_while_an_instance_is_stored(self):
        v, p = FakeVast(running=0, resident=1), FakeParalon(available=True, spot=True)
        c = self.ctl(v, p)
        c.poll()
        self.assertEqual(p.writes, [OFF])
        self.assertTrue(self.mstate()["took_off"]["paralon"])
        self.assertIn("stored instance", self.said(c))

    def test_on_demand_node_paused_too(self):
        v, p = FakeVast(running=0, resident=2), FakeParalon(available=True, spot=False)
        with self.assertLogs("vpdual", "WARNING"):
            self.ctl(v, p).poll()
        self.assertEqual(p.writes, [OFF])

    def test_relisted_once_nothing_is_stored(self):
        v, p = FakeVast(running=0, resident=1), FakeParalon(available=True)
        c = self.ctl(v, p)
        c.poll()
        self.assertEqual(p.writes, [OFF])
        c.poll()                                   # still stored: stays paused
        self.assertEqual(p.writes, [OFF])
        v.resident = 0
        c.poll()
        self.assertEqual(p.writes, [OFF, ON])
        c.poll()
        self.assertFalse(self.mstate()["took_off"]["paralon"])

    def test_every_mode_where_paralon_could_be_available(self):
        v, p = FakeVast(running=0, resident=1), FakeParalon(available=True)
        self.ctl(v, p, mode="paralon").poll()
        self.assertIn(OFF, p.writes)
        self.assertNotIn(ON, p.writes)

    def test_api_only_machine_too(self):
        v, p = FakeVast(running=0, resident=1), FakeParalon(available=True)
        self.ctl(v, p, agent="").poll()
        self.assertEqual(p.writes, [OFF])

    def test_running_renter_is_not_a_stored_instance(self):
        # resident counts running too: 1 running, 1 resident = nothing stored
        v, p = FakeVast(running=1, resident=1), FakeParalon(available=False)
        c = self.ctl(v, p)
        c.poll()
        self.assertNotIn("stored instance", self.said(c))

    def test_rented_node_with_a_stored_instance_warns(self):
        v = FakeVast(running=0, resident=1)
        p = FakeParalon(available=True, rented=True)
        c = self.ctl(v, p)
        with self.assertLogs("vpdual", "WARNING") as logs:
            c.poll()
        self.assertIn("cannot be paused", " ".join(logs.output))
        self.assertNotIn(RECLAIM, p.writes)        # nothing resumed yet
        self.assertIn(OFF, p.writes)               # no new rental after this one


# -- fix 3 ----------------------------------------------------------------
class Fix3RentalContainerUnlistsVastAtOnce(Base):
    def setUp(self):
        super().setUp()
        self.v, self.p, self.d = FakeVast(), FakeParalon(available=True), FakeDocker()

    def start(self, c, name="jupyter-abc", image="ghcr.io/paraloncloud/jupyter:latest",
              ptype="rental"):
        return c.container_started(TARGET, name, image, ptype)

    def test_unlists_at_once_with_terms_saved_first(self):
        c = self.ctl(self.v, self.p, self.d)
        c.poll()
        seen = {}
        real = self.v.unlist_machine

        def unlist(mid):
            seen["flag"] = self.mstate()["took_off"]["vast"]
            seen["terms"] = self.mstate()["saved_listing"]
            real(mid)
        self.v.unlist_machine = unlist
        self.assertTrue(self.start(c))
        self.assertTrue(seen["flag"])
        self.assertEqual(seen["terms"]["price_gpu"], 0.45)
        self.assertEqual(seen["terms"]["price_min_bid"], 0.35)
        self.assertFalse(self.v.listed)
        self.assertTrue(c.bursting)

    def test_not_for_the_agent_a_benchmark_or_an_inference_worker(self):
        c = self.ctl(self.v, self.p, self.d)
        c.poll()
        for name, image, ptype in (
                ("paralon-agent", "ghcr.io/paraloncloud/agent:latest", ""),
                ("paralon-bench-1", "ghcr.io/paraloncloud/benchmark:1", "benchmark"),
                ("paralon-inference-x", "vllm/vllm-openai:latest", "inference"),
                ("worker-z", "vllm/vllm-openai:latest", "inference"),
                ("jupyter-abc", "ghcr.io/paraloncloud/jupyter:latest", "")):
            self.assertFalse(self.start(c, name, image, ptype), name)
        self.assertNotIn(UNLIST, self.v.writes)
        self.assertEqual(self.p.writes, [])        # Paralon's own: no pause either

    def test_not_with_keep_vast_listed(self):
        c = self.ctl(self.v, self.p, self.d, policy="keep_vast_listed")
        c.poll()
        self.start(c)
        self.assertNotIn(UNLIST, self.v.writes)

    def test_not_while_a_vast_renter_runs(self):
        c = self.ctl(FakeVast(running=1), self.p, self.d)
        c.poll()
        self.start(c)
        self.assertNotIn(UNLIST, c.vast.writes)
        self.d.add("v1", "pytorch/pytorch", "C.123")     # renter seen on the machine only
        c2 = self.ctl(self.v, self.p, self.d)
        self.start(c2)
        self.assertNotIn(UNLIST, self.v.writes)

    def test_not_in_vast_mode(self):
        c = self.ctl(self.v, FakeParalon(available=False), self.d, mode="vast")
        c.poll()
        self.start(c)
        self.assertNotIn(UNLIST, self.v.writes)

    def test_vast_unreadable_holds_the_unlist_for_the_pass(self):
        c = self.ctl(self.v, self.p, self.d)
        c.poll()
        self.v.fail_read = True
        with self.assertLogs("vpdual", "WARNING"):
            self.start(c)
        self.assertNotIn(UNLIST, self.v.writes)

    def test_the_rental_ends_and_the_saved_terms_are_relisted(self):
        c = self.ctl(self.v, self.p, self.d)
        c.poll()
        self.start(c)
        self.d.add(*RENTAL)
        self.p.rented = True
        c.poll()                                    # the API agrees: stays off
        self.assertEqual(lists(self.v), [])
        self.p.rented = False
        self.d.containers_.pop()                    # rental container gone
        c.poll()
        self.assertEqual(len(lists(self.v)), 1)
        listing = lists(self.v)[0][2]
        self.assertEqual((listing["price_gpu"], listing["price_min_bid"]), (0.45, 0.35))
        c.poll()
        self.assertFalse(self.mstate()["took_off"]["vast"])

    def test_a_false_alarm_relists(self):
        c = self.ctl(self.v, self.p, self.d)
        c.poll()
        self.start(c)                               # unlisted at once
        self.assertFalse(self.v.listed)
        c.poll()                                    # API never shows a rental, no container
        self.assertTrue(self.v.listed)
        self.assertEqual(len(lists(self.v)), 1)

    def test_a_pass_sees_the_rental_container_before_the_api(self):
        self.d.add(*RENTAL)
        c = self.ctl(self.v, self.p, self.d)        # node.rented still False
        c.poll()
        self.assertIn(UNLIST, self.v.writes)


# -- fix 4 ----------------------------------------------------------------
class Fix4IdleJobNeverUnderAParalonRental(Base):
    def test_vast_default_job_stopped_through_vast_flag_first(self):
        v = FakeVast().add_job(777)
        d = FakeDocker().add(*JOB)
        p = FakeParalon(available=True, rented=True)
        c = self.ctl(v, p, d)
        seen = {}
        real = v.stop_instance

        def stop(iid):
            seen["owed"] = self.mstate()["idle_job"]
            real(iid)
        v.stop_instance = stop
        c.poll()
        self.assertIn(("stop_instance", 777), v.writes)
        self.assertEqual(seen["owed"], [{"kind": "vast", "id": 777, "name": "C.777"}])
        self.assertEqual(d.writes, [])              # never `docker stop` a Vast job

    def test_only_a_running_default_job_is_stopped(self):
        v = FakeVast().add_job(777, status="stopped").add_job(778, machine_id=222) \
            .add_job(779, image="someone/else:1")
        d = FakeDocker().add(*JOB)
        c = self.ctl(v, FakeParalon(rented=True), d)
        c.poll()
        self.assertEqual([w for w in v.writes if w[0] == "stop_instance"], [])
        self.assertEqual(self.mstate()["idle_job"], [])
        self.assertIn("waiting for Vast to stop", self.said(c))

    def test_no_matching_instance_warns(self):
        v = FakeVast()
        d = FakeDocker().add(*JOB)
        c = self.ctl(v, FakeParalon(rented=True), d)
        with self.assertLogs("vpdual", "WARNING") as logs:
            c.poll()
        self.assertIn("cannot stop it", " ".join(logs.output))

    def test_instances_unreadable_retries(self):
        v = FakeVast().add_job(777)
        v.fail_instances = True
        d = FakeDocker().add(*JOB)
        c = self.ctl(v, FakeParalon(rented=True), d)
        with self.assertLogs("vpdual", "WARNING"):
            self.assertFalse(c.poll())
        self.assertEqual(self.mstate()["idle_job"], [])

    def test_restarted_after_the_rental_after_the_vast_relist(self):
        v = FakeVast().add_job(777)
        d = FakeDocker().add(*JOB).add(*RENTAL)
        p = FakeParalon(available=True, rented=True)
        c = self.ctl(v, p, d)
        c.poll()                                    # unlist + stop the job
        self.assertEqual(v.writes[:2], [UNLIST, ("stop_instance", 777)])
        d.get("j1").running = False
        p.rented = False
        d.containers_ = [x for x in d.containers_ if x.id != "r1"]
        v.writes.clear()
        c.poll()
        self.assertEqual([w[0] for w in v.writes], ["list", "start_instance"])
        self.assertEqual(self.mstate()["idle_job"], [{"kind": "vast", "id": 777, "name": "C.777"}])
        d.get("j1").running = True
        c.poll()                                    # a later read shows it back
        self.assertEqual(self.mstate()["idle_job"], [])
        self.assertNotIn(("start_instance", 777), v.writes[2:])

    def test_a_job_this_tool_did_not_stop_is_not_started(self):
        v = FakeVast().add_job(777, status="stopped")
        d = FakeDocker().add("j1", "me/miner:1", "C.777", running=False)
        c = self.ctl(v, FakeParalon(available=True), d)
        c.poll()
        self.assertNotIn(("start_instance", 777), v.writes)

    def test_not_stopped_while_a_vast_renter_has_the_gpu(self):
        v = FakeVast(running=1).add_job(777)
        d = FakeDocker().add(*JOB)
        c = self.ctl(v, FakeParalon(rented=True), d)
        c.poll()
        self.assertNotIn(("stop_instance", 777), v.writes)

    def test_not_restarted_while_paralon_is_still_rented(self):
        v = FakeVast().add_job(777)
        d = FakeDocker().add(*JOB)
        p = FakeParalon(rented=True)
        c = self.ctl(v, p, d)
        c.poll()
        d.get("j1").running = False
        c.poll()
        c.poll()
        self.assertNotIn(("start_instance", 777), v.writes)

    def test_the_rental_container_start_stops_it_at_once(self):
        v = FakeVast().add_job(777)
        d = FakeDocker().add(*JOB)
        c = self.ctl(v, FakeParalon(available=True), d)
        c.poll()
        self.assertTrue(c.container_started(TARGET, "jupyter-abc",
                                            "ghcr.io/paraloncloud/jupyter:latest", "rental"))
        self.assertIn(("stop_instance", 777), v.writes)

    def test_plain_docker_idle_job_stopped_and_started(self):
        v = FakeVast()
        d = FakeDocker().add(*PLAIN).add(*RENTAL)
        p = FakeParalon(rented=True)
        c = self.ctl(v, p, d)
        c.poll()
        self.assertIn(("stop", ["m1"]), d.writes)
        self.assertEqual(self.mstate()["idle_job"],
                         [{"kind": "docker", "id": "m1", "name": "my-miner"}])
        self.assertEqual(v.instance_reads, 0)       # no Vast call for a plain container
        p.rented = False
        d.containers_ = [x for x in d.containers_ if x.id != "r1"]
        c.poll()
        self.assertIn(("start", "m1"), d.writes)
        c.poll()
        self.assertEqual(self.mstate()["idle_job"], [])

    def test_owed_start_survives_a_restart(self):
        v = FakeVast()
        d = FakeDocker().add(*PLAIN)
        p = FakeParalon(rented=True)
        self.ctl(v, p, d).poll()
        p.rented = False
        c2 = self.ctl(v, p, d)                      # a fresh process, same state file
        c2.poll()
        self.assertIn(("start", "m1"), d.writes)

    def test_without_idle_job_images_nothing_is_touched(self):
        v = FakeVast().add_job(777)
        d = FakeDocker().add(*JOB)
        c = self.ctl(v, FakeParalon(rented=True), d, idle=())
        c.poll()
        self.assertEqual(v.instance_reads, 0)


# -- fix 5 ----------------------------------------------------------------
class Fix5VastRelistWaitsForTheGpu(Base):
    def _after_rental(self, agent=TARGET):
        v, p, d = FakeVast(), FakeParalon(available=True, rented=True), FakeDocker()
        d.add(*RENTAL)
        c = self.ctl(v, p, d, agent=agent)
        c.poll()                                    # unlisted for the rental
        self.assertFalse(v.listed)
        p.rented = False                            # the API says it ended
        return c, v, p, d

    def test_holds_while_the_rental_container_tears_down(self):
        c, v, p, d = self._after_rental()
        c.poll()
        self.assertEqual(lists(v), [])
        self.assertIn("counted as rented until it is gone", self.said(c))
        d.containers_ = [x for x in d.containers_ if x.id != "r1"]
        d.gpu_procs, d.gpu_mem_mib = 1, 20000     # its memory not released yet
        c.poll()
        self.assertEqual(lists(v), [])
        self.assertIn("not relisting Vast yet", self.said(c))
        self.assertIn("GPU is not free", self.said(c))
        self.assertIn(111, c.recovering)           # fast passes meanwhile
        d.gpu_procs, d.gpu_mem_mib = 0, 3
        c.poll()
        self.assertEqual(len(lists(v)), 1)

    def test_unreadable_machine_holds(self):
        c, v, p, d = self._after_rental()
        d.containers_ = [x for x in d.containers_ if x.id != "r1"]
        d.fail_read = True
        with self.assertLogs("vpdual", "WARNING"):
            c.poll()
        self.assertEqual(lists(v), [])

    def test_a_paralon_side_job_does_not_hold_vast_forever(self):
        c, v, p, d = self._after_rental()
        d.containers_ = [x for x in d.containers_ if x.id != "r1"]
        d.add("b1", "ghcr.io/paraloncloud/benchmark:1", "paralon-bench", True, "benchmark")
        d.gpu_procs, d.gpu_mem_mib = 1, 9000
        c.poll()
        self.assertEqual(len(lists(v)), 1)          # rule 1 kills it if a renter comes

    def test_api_only_machine_relists_at_once(self):
        v, p = FakeVast(), FakeParalon(available=True, rented=True)
        c = self.ctl(v, p, agent="")
        c.poll()
        p.rented = False
        c.poll()
        self.assertEqual(len(lists(v)), 1)

    def test_the_idle_job_can_never_hold_the_relist(self):
        # the idle job comes back first (someone started it): gpu_free counts it free
        c, v, p, d = self._after_rental()
        d.containers_ = [x for x in d.containers_ if x.id != "r1"]
        d.add(*PLAIN)
        d.gpu_procs, d.gpu_mem_mib = 1, 15000
        c.poll()
        self.assertEqual(len(lists(v)), 1)


# -- the CLI calls ---------------------------------------------------------
class Runner:
    def __init__(self, out):
        self.out, self.calls = out, []

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, self.out, "DEPRECATED: ...")


class VastInstanceCalls(unittest.TestCase):
    def test_show_instances_raw(self):
        rows = [{"id": 777, "machine_id": 111, "image_uuid": "me/miner:1",
                 "intended_status": "running", "actual_status": "running"}]
        r = Runner(json.dumps(rows))
        self.assertEqual(VastCLI(runner=r).read_instances(), rows)
        self.assertEqual(r.calls, [["vastai", "show", "instances", "--raw"]])

    def test_show_instances_non_json_is_an_error(self):
        with self.assertRaises(VastError):
            VastCLI(runner=Runner("DEPRECATED")).read_instances()

    def test_stop_and_start_check_the_reply_text(self):
        r = Runner("stopping instance 777.\n")
        VastCLI(runner=r).stop_instance(777)
        self.assertEqual(r.calls, [["vastai", "stop", "instance", "777"]])
        VastCLI(runner=Runner("starting instance 777.")).start_instance(777)
        with self.assertRaises(VastError):
            VastCLI(runner=Runner("Instance 777 not found")).stop_instance(777)


if __name__ == "__main__":
    unittest.main()
