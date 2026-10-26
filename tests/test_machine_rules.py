"""v0.4.0: decide from the machine (Paralon's founder, 2026-10-26).

1. Decide from the machine, not from Vast's API count.
2. docker_events by default, acting at once on a non-Paralon container start.
3. When the Vast renter leaves: poll every few seconds until the GPU is free,
   start the agent, wait until the node is online, only then make it available.
4. unlist_vast by default.
Plus: an ON-DEMAND node stays paused while Vast has a stopped instance."""

import threading
import unittest

from vpdual.docker_events import events_cmd, start_watcher
from vpdual.engine import Controller

from helpers import FakeDocker, FakeParalon, FakeVast, TempDirMixin, make_config

OFF = ("available", "node-1", False)
ON = ("available", "node-1", True)
RENTER = ("v1", "pytorch/pytorch:latest", "C.123")


class Base(TempDirMixin, unittest.TestCase):
    def ctl(self, vast, paralon, docker, agent="user@gpu1", idle=None):
        cfg = make_config(self.state_path, agent=agent)
        cfg.idle_job_images = list(idle or [])
        c = Controller(cfg, vast, paralon, self.store, clock=lambda: 1000.0,
                       docker=docker)
        c.set_mode("dual")
        return c


class Rule1TheMachineDecides(Base):
    def test_a_vast_container_on_the_machine_is_a_renter_whatever_vast_counts(self):
        d = FakeDocker().add(*RENTER)
        p = FakeParalon(available=True)
        self.ctl(FakeVast(running=0), p, d).poll()          # Vast's count lags
        self.assertIn(OFF, p.writes)
        self.assertIn(("stop", ["agent1"]), d.writes)

    def test_vast_count_alone_still_counts_while_the_image_pulls(self):
        d, p = FakeDocker(), FakeParalon(available=True)
        self.ctl(FakeVast(running=1), p, d).poll()          # no container yet
        self.assertIn(OFF, p.writes)

    def test_the_hosts_idle_job_is_not_a_renter(self):
        d = FakeDocker().add("j1", "me/miner:1", "C.777")
        p = FakeParalon(available=True)
        self.ctl(FakeVast(running=0), p, d, idle=["me/miner"]).poll()
        self.assertEqual(p.writes, [])

    def test_unreadable_machine_never_makes_paralon_available(self):
        d, p = FakeDocker(), FakeParalon(available=False)
        c = self.ctl(FakeVast(running=0), p, d)
        c.mstate(c.cfg.machines[0])["took_off"]["paralon"] = True
        d.fail_read = True
        with self.assertLogs("vpdual", "WARNING"):
            c.poll()
        self.assertNotIn(ON, p.writes)


class Rule3RecoveryAfterTheVastRenter(Base):
    def _after_rental(self, gpu_mem=0, agent_running=False):
        d = FakeDocker(agent_running=agent_running)
        d.gpu_procs, d.gpu_mem_mib = (1 if gpu_mem else 0), gpu_mem
        p = FakeParalon(available=False)
        c = self.ctl(FakeVast(running=0), p, d)
        st = c.mstate(c.cfg.machines[0])
        st["took_off"]["paralon"] = st["took_off"]["agent"] = True
        c._save()
        return c, d, p

    def test_waits_for_the_gpu_before_starting_the_agent(self):
        c, d, p = self._after_rental(gpu_mem=18000)
        c.poll()
        self.assertEqual(d.writes, [])
        self.assertIn("waiting for the GPU", " ".join(m for _, m in c.pass_decisions))
        self.assertIn(111, c.recovering)                    # fast passes meanwhile
        d.gpu_procs, d.gpu_mem_mib = 0, 3
        c.poll()
        self.assertEqual(d.writes, [("start", "agent1")])

    def test_paralon_waits_for_the_agent_then_the_node(self):
        c, d, p = self._after_rental()
        c.poll()                                            # starts the agent
        self.assertNotIn(ON, p.writes)                      # agent was not running
        p.nodes["node-1"]["online"] = False
        c.poll()                                            # agent up, node offline
        self.assertNotIn(ON, p.writes)
        p.nodes["node-1"]["online"] = True
        c.poll()
        self.assertIn(ON, p.writes)

    def test_recovery_reads_fresh_nodes_within_the_call_budget(self):
        c, d, p = self._after_rental(gpu_mem=18000)
        now = [1000.0]
        c.clock = lambda: now[0]
        c.poll()                                            # becomes recovering
        c.poll()                                            # first fresh read
        r0 = p.refreshes
        self.assertEqual(r0, 1)
        c.poll()                                            # within 60 s: cached
        self.assertEqual(p.refreshes, r0)
        now[0] += 61
        c.poll()
        self.assertEqual(p.refreshes, r0 + 1)

    def test_recovery_ends_when_nothing_is_owed(self):
        c, d, p = self._after_rental()
        for _ in range(3):
            p.nodes["node-1"]["online"] = True
            c.poll()
        self.assertNotIn(111, c.recovering)


class OnDemandWithAStoppedVastInstance(Base):
    def test_on_demand_node_paused_while_an_instance_can_resume(self):
        d = FakeDocker()
        p = FakeParalon(available=True, spot=False)
        with self.assertLogs("vpdual", "WARNING"):          # the on-demand warning
            self.ctl(FakeVast(running=0, resident=1), p, d).poll()
        self.assertIn(OFF, p.writes)

    def test_spot_node_stays_listed(self):
        p = FakeParalon(available=True, spot=True)
        self.ctl(FakeVast(running=0, resident=1), p, FakeDocker()).poll()
        self.assertEqual(p.writes, [])


class Rule2DockerEventsActAtOnce(Base):
    def test_a_vast_container_start_pauses_paralon_immediately(self):
        d, p = FakeDocker(), FakeParalon(available=True)
        c = self.ctl(FakeVast(), p, d)
        c.poll()
        self.assertTrue(c.container_started("user@gpu1", "C.555", "pytorch/pytorch"))
        self.assertIn(OFF, p.writes)
        self.assertTrue(c.mstate(c.cfg.machines[0])["took_off"]["paralon"])

    def test_paralon_pins_and_idle_jobs_are_ignored(self):
        p = FakeParalon(available=True)
        c = self.ctl(FakeVast(), p, FakeDocker(), idle=["me/miner"])
        c.poll()
        for name, image in (("jupyter-1", "ghcr.io/paraloncloud/jupyter:latest"),
                            ("paralon-inference-x", "vllm/vllm-openai"),
                            ("vpdual-pin-jupyter-abc", "ghcr.io/paraloncloud/jupyter:latest"),
                            ("C.9", "me/miner:3")):
            self.assertFalse(c.container_started("user@gpu1", name, image), name)
        self.assertEqual(p.writes, [])

    def test_other_machines_are_untouched(self):
        p = FakeParalon(available=True)
        c = self.ctl(FakeVast(), p, FakeDocker())
        c.poll()
        self.assertFalse(c.container_started("other@host", "C.1", "x"))
        self.assertEqual(p.writes, [])

    def test_watcher_passes_name_and_image_and_wakes(self):
        seen, wake = [], threading.Event()

        class Proc:
            def __init__(self):
                import io
                self.stdout = io.StringIO("C.123\tpytorch/pytorch:2\n")

        t = start_watcher(wake, popen=lambda cmd, **kw: Proc(), sleep=lambda s: None,
                          retries=0, target="me@gpu1",
                          on_start=lambda n, i: seen.append((n, i)))
        t.join(5)
        self.assertEqual(seen, [("C.123", "pytorch/pytorch:2")])
        self.assertTrue(wake.is_set())

    def test_remote_events_go_over_ssh_batch_mode(self):
        cmd = events_cmd("me@gpu1")
        self.assertEqual(cmd[:2], ["ssh", "-o"])
        self.assertIn("BatchMode=yes", cmd)
        self.assertIn("me@gpu1", cmd)
        self.assertTrue(cmd[-1].startswith("docker 'events'"))
        self.assertEqual(events_cmd()[:2], ["docker", "events"])


if __name__ == "__main__":
    unittest.main()
