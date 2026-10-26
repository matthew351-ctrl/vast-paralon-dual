"""The Paralon agent is stopped while a Vast renter has the GPU."""

import subprocess
import unittest

from vpdual.agent import (AgentError, DockerHost, PS_FORMAT, agent_of,
                          leftovers, parse_ps, valid_target)
from vpdual.config import ConfigError, parse_config
from vpdual.engine import Controller

from helpers import (FakeDocker, FakeParalon, FakeVast, TempDirMixin,
                     make_config)

INFER = ("inf1", "ghcr.io/paraloncloud/diffusion-server:latest",
         "paralon-inference-z-image-turbo")
DOWNLOAD = ("dl1", "python:3.11-slim", "paralon-hf-download-1")
BENCH = ("bench1", "ghcr.io/paraloncloud/gpu-init:latest", "paralon-gpu-benchmark")


class Base(TempDirMixin, unittest.TestCase):
    def ctl(self, vast, paralon, docker, agent="user@gpu1", dry_run=False):
        cfg = make_config(self.state_path, agent=agent)
        c = Controller(cfg, vast, paralon, self.store, dry_run=dry_run,
                       clock=lambda: 1000.0, docker=docker)
        if not dry_run:
            c.set_mode("dual")
        else:
            c.mstate(cfg.machines[0])["mode"] = "dual"
        return c


class VastRenterStopsTheAgent(Base):
    def test_agent_stopped_first_then_its_containers(self):
        d = FakeDocker().add(*INFER).add(*DOWNLOAD)
        self.ctl(FakeVast(running=1), FakeParalon(available=False), d).poll()
        self.assertEqual(d.writes, [("stop", ["agent1"]),
                                    ("kill", ["inf1", "dl1"])])
        self.assertTrue(self.mstate()["took_off"]["agent"])
        self.assertEqual(d.targets[0], "user@gpu1")

    def test_flag_is_on_disk_before_the_stop(self):
        d = FakeDocker()
        d.fail_write = True
        with self.assertLogs("vpdual", "WARNING"):
            self.ctl(FakeVast(running=1), FakeParalon(available=False), d).poll()
        self.assertTrue(self.mstate()["took_off"]["agent"])

    def test_a_benchmark_beside_the_renter_is_stopped(self):
        d = FakeDocker().add(*BENCH)
        self.ctl(FakeVast(running=1), FakeParalon(available=False), d).poll()
        self.assertIn(("kill", ["bench1"]), d.writes)

    def test_agent_already_stopped_leftovers_still_cleared(self):
        d = FakeDocker(agent_running=False).add(*INFER)
        self.ctl(FakeVast(running=1), FakeParalon(available=False), d).poll()
        self.assertEqual(d.writes, [("kill", ["inf1"])])

    def test_agent_restarted_by_docker_is_stopped_again(self):
        d = FakeDocker()
        c = self.ctl(FakeVast(running=1), FakeParalon(available=False), d)
        c.poll()
        d.get("agent1").running = True        # dockerd restart, restart=always
        c.poll()
        self.assertEqual(d.writes.count(("stop", ["agent1"])), 2)

    def test_agent_stays_up_while_a_paralon_rental_is_reclaimed(self):
        d = FakeDocker().add(*INFER)
        p = FakeParalon(available=False, rented=True)
        self.ctl(FakeVast(running=1), p, d).poll()
        self.assertEqual(d.writes, [])
        self.assertIn(("reclaim", "node-1"), p.writes)
        self.assertFalse(self.mstate()["took_off"]["agent"])

    def test_a_paralon_rental_container_is_never_stopped(self):
        d = FakeDocker().add("r1", "ghcr.io/paraloncloud/jupyter:latest",
                             "paralon-rental-abc", paralon_type="rental")
        self.ctl(FakeVast(running=1), FakeParalon(available=False), d).poll()
        self.assertEqual(d.writes, [("stop", ["agent1"])])

    def test_vast_containers_are_never_stopped(self):
        d = FakeDocker().add("v1", "pytorch/pytorch:latest", "C.123")
        self.ctl(FakeVast(running=1), FakeParalon(available=False), d).poll()
        self.assertNotIn("v1", [i for w in d.writes for i in w[1]])

    def test_dry_run_writes_nothing(self):
        d = FakeDocker()
        self.ctl(FakeVast(running=1), FakeParalon(available=False), d,
                 dry_run=True).poll()
        self.assertEqual(d.writes, [])


class RenterGoneStartsTheAgent(Base):
    def test_started_when_the_renter_leaves_then_flag_cleared(self):
        v, d = FakeVast(running=1), FakeDocker()
        c = self.ctl(v, FakeParalon(available=False), d)
        c.poll()
        v.running = 0
        c.poll()
        self.assertEqual(d.writes[-1], ("start", "agent1"))
        self.assertTrue(self.mstate()["took_off"]["agent"])   # until confirmed
        c.poll()
        self.assertFalse(self.mstate()["took_off"]["agent"])

    def test_an_agent_someone_else_stopped_is_left_alone(self):
        d = FakeDocker(agent_running=False)
        self.ctl(FakeVast(running=0), FakeParalon(available=False), d).poll()
        self.assertEqual(d.writes, [])

    def test_failed_start_is_retried(self):
        v, d = FakeVast(running=1), FakeDocker()
        c = self.ctl(v, FakeParalon(available=False), d)
        c.poll()
        v.running = 0
        d.fail_write = True
        with self.assertLogs("vpdual", "WARNING"):
            c.poll()
        d.fail_write = False
        c.poll()
        self.assertEqual(d.writes.count(("start", "agent1")), 2)
        self.assertTrue(d.get("agent1").running)

    def test_paralon_is_paused_in_the_same_pass_the_agent_stops(self):
        v, d = FakeVast(running=1), FakeDocker()
        p = FakeParalon(available=True)
        self.ctl(v, p, d).poll()
        self.assertEqual(p.writes, [("available", "node-1", False)])
        self.assertEqual(d.writes, [("stop", ["agent1"])])


class DockerUnreadable(Base):
    def test_holds_and_warns_beside_a_renter(self):
        d = FakeDocker()
        d.fail_read = True
        with self.assertLogs("vpdual", "WARNING") as logs:
            ok = self.ctl(FakeVast(running=1), FakeParalon(available=False), d).poll()
        self.assertFalse(ok)
        self.assertIn("cannot read Docker", "\n".join(logs.output))

    def test_idle_and_nothing_owed_does_not_read_docker(self):
        d = FakeDocker()
        d.fail_read = True
        self.assertTrue(self.ctl(FakeVast(), FakeParalon(), d).poll())
        self.assertEqual(d.reads, 0)          # only the Jupyter pin ran
        self.assertEqual(d.pins, 1)

    def test_not_configured_means_never_called(self):
        d = FakeDocker()
        self.ctl(FakeVast(running=1), FakeParalon(available=False), d,
                 agent="").poll()
        self.assertEqual(d.targets, [])


class StatusShowsTheAgent(Base):
    def test_status_line_and_no_writes(self):
        from vpdual.cli import cmd_status
        d, out = FakeDocker(), []
        c = self.ctl(FakeVast(running=1), FakeParalon(available=False), d)
        cmd_status(c, c.cfg, None, out=out.append)
        text = "\n".join(out)
        self.assertIn("Paralon agent via user@gpu1: running", text)
        self.assertIn("stop the Paralon agent", text)     # what a pass would do
        self.assertEqual(d.writes, [])


class JupyterPin(Base):
    def test_pinned_every_pass_and_logged_only_on_change(self):
        d = FakeDocker()
        d.pin_lines = ["pinned 526048b557bf", "removed vpdual-pin-jupyter-0ld1d0000000"]
        c = self.ctl(FakeVast(), FakeParalon(), d)
        with self.assertLogs("vpdual", "INFO") as logs:
            c.poll()
        self.assertIn("Paralon's Jupyter image: pinned 526048b557bf; removed",
                      "\n".join(logs.output))
        d.pin_lines = ["kept 526048b557bf"]
        c.pass_decisions = []
        c.poll()
        self.assertEqual(d.pins, 2)
        self.assertFalse([m for _, m in c.pass_decisions if "Jupyter" in m])

    def test_also_beside_a_vast_renter(self):
        d = FakeDocker()
        self.ctl(FakeVast(running=1), FakeParalon(available=False), d).poll()
        self.assertEqual(d.pins, 1)

    def test_failure_warns_and_holds_nothing(self):
        d = FakeDocker()
        d.fail_pin = True
        v, p = FakeVast(running=1), FakeParalon(available=True)
        with self.assertLogs("vpdual", "WARNING") as logs:
            self.ctl(v, p, d).poll()
        self.assertIn("Jupyter pin failed", "\n".join(logs.output))
        self.assertIn(("available", "node-1", False), p.writes)   # rule 1 still ran
        self.assertEqual(d.writes[0], ("stop", ["agent1"]))

    def test_dry_run_and_unmanaged_never_pin(self):
        d = FakeDocker()
        self.ctl(FakeVast(), FakeParalon(), d, dry_run=True).poll()
        self.ctl(FakeVast(), FakeParalon(), d, agent="").poll()
        self.assertEqual(d.pins, 0)


class DockerHostCommands(unittest.TestCase):
    def run_recorder(self, stdout="", returncode=0):
        calls = []

        def run(cmd, **kw):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, returncode, stdout, "boom")
        return calls, run

    def test_local_runs_docker_directly(self):
        calls, run = self.run_recorder()
        DockerHost("local", run=run).stop(["a", "b"])
        self.assertEqual(calls, [["docker", "stop", "-t", "2", "a", "b"]])
        calls.clear()
        DockerHost("local", run=run).kill(["c"])
        self.assertEqual(calls, [["docker", "kill", "c"]])

    def test_ssh_quotes_the_remote_command(self):
        calls, run = self.run_recorder()
        DockerHost("matt@10.0.0.5", run=run).containers()
        cmd = calls[0]
        self.assertEqual(cmd[:6], ["ssh", "-o", "BatchMode=yes", "-o",
                                   "ConnectTimeout=10", "matt@10.0.0.5"])
        self.assertIn("'" + PS_FORMAT + "'", cmd[6])

    def test_nonzero_exit_is_an_error(self):
        _, run = self.run_recorder(returncode=1)
        with self.assertRaises(AgentError):
            DockerHost("local", run=run).start("x")

    def test_missing_binary_is_an_error(self):
        def run(cmd, **kw):
            raise FileNotFoundError("ssh")
        with self.assertRaises(AgentError):
            DockerHost("h1", run=run).containers()

    def test_jupyter_pin_runs_one_shell_and_never_pulls(self):
        calls, run = self.run_recorder(stdout="pinned 526048b557bf\n")
        self.assertEqual(DockerHost("local", run=run).pin_jupyter(),
                         ["pinned 526048b557bf"])
        self.assertEqual(calls[0][:2], ["sh", "-c"])
        script = calls[0][2]
        self.assertNotIn("docker pull", script)
        self.assertNotIn("docker run", script)          # create, never start
        self.assertIn("docker image inspect", script)
        self.assertLess(script.index("image inspect"), script.index("docker create"))
        calls, run = self.run_recorder(stdout="absent\n")
        DockerHost("me@gpu1", run=run).pin_jupyter()
        self.assertEqual(calls[0][5], "me@gpu1")
        self.assertIn("docker create", calls[0][6])

    def test_option_like_targets_are_refused(self):
        for bad in ("-oProxyCommand=x", "", "a b", "user@-x", "x;rm"):
            self.assertFalse(valid_target(bad), bad)
        for good in ("local", "gpu1", "matt@gpu1", "matt@10.0.0.5", "a.b-c@h_1"):
            self.assertTrue(valid_target(good), good)
        with self.assertRaises(ValueError):
            DockerHost("-oProxyCommand=x")


class ParsingAndSelection(unittest.TestCase):
    PS = ("new1\tghcr.io/paraloncloud/agent:latest\tparalon-agent-2\trunning\t\n"
          "old1\tghcr.io/paraloncloud/agent:latest\tparalon-agent\texited\t\n"
          "inf1\tghcr.io/paraloncloud/diffusion-server:latest\tparalon-inference-x\trunning\tinference\n"
          "r1\tghcr.io/paraloncloud/jupyter:latest\tparalon-r\trunning\trental\n"
          "v1\tvastai/kvm:latest\tC.123\trunning\t\n"
          "\n")

    def test_agent_found_by_image_after_a_rename(self):
        cs = parse_ps(self.PS)
        self.assertEqual(agent_of(cs).id, "new1")
        self.assertEqual([c.id for c in leftovers(cs)], ["inf1"])

    def test_no_agent(self):
        self.assertIsNone(agent_of(parse_ps("v1\tx\tC.1\trunning\t\n")))


class ConfigKey(unittest.TestCase):
    RAW = {"on_paralon_rental": "keep_vast_listed",
           "machines": [{"vast_machine_id": 1, "paralon_node_id": "n"}]}

    def cfg(self, agent):
        raw = {**self.RAW, "machines": [{**self.RAW["machines"][0],
                                         "paralon_agent": agent}]}
        return parse_config(raw, {"PARALON_API_KEY": "prlc_x"})

    def test_accepted(self):
        self.assertEqual(self.cfg("local").machines[0].paralon_agent, "local")
        self.assertEqual(self.cfg("me@gpu1").machines[0].paralon_agent, "me@gpu1")

    def test_refused(self):
        for bad in ("-oProxyCommand=evil", 5, "a b"):
            with self.assertRaises(ConfigError):
                self.cfg(bad)

    def test_absent_is_unmanaged(self):
        cfg = parse_config(self.RAW, {"PARALON_API_KEY": "prlc_x"})
        self.assertEqual(cfg.machines[0].paralon_agent, "")


if __name__ == "__main__":
    unittest.main()
