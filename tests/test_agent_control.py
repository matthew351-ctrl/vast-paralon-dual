"""v0.8.0 item 6: agent_control docker | systemd | none."""

import subprocess
import unittest

from helpers import FakeDocker, FakeParalon, FakeVast, TempDirMixin, make_config

from vpdual.agent import DockerHost, SystemdControl
from vpdual.config import ConfigError, parse_config
from vpdual.engine import Controller

UNIT = "paralon-agent.service"
OFF = ("available", "node-1", False)


class Runner:
    def __init__(self, out="LoadState=loaded\nActiveState=active\n"):
        self.out, self.calls = out, []

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, self.out, "")


class SystemdCommands(unittest.TestCase):
    SYSTEMCTL = "XDG_RUNTIME_DIR=${XDG_RUNTIME_DIR:-/run/user/$(id -u)} systemctl --user"

    def test_ssh_and_local_commands(self):
        r = Runner()
        c = SystemdControl(DockerHost("me@gpu1", run=r), UNIT)
        self.assertTrue(c.agent().running)
        c.stop(None)
        c.start(None)
        ssh = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "me@gpu1"]
        self.assertEqual(r.calls, [
            ssh + [f"{self.SYSTEMCTL} show -p LoadState -p ActiveState -- {UNIT}"],
            ssh + [f"{self.SYSTEMCTL} stop -- {UNIT}"],
            ssh + [f"{self.SYSTEMCTL} start -- {UNIT}"]])
        r = Runner("LoadState=not-found\nActiveState=inactive\n")
        local = SystemdControl(DockerHost("local", run=r), UNIT)
        self.assertIsNone(local.agent())
        self.assertEqual(r.calls[0][:2], ["sh", "-c"])

    def test_unit_name_is_never_an_option(self):
        for bad in ("-H evil", "a;b", ""):
            with self.assertRaises(ValueError):
                SystemdControl(DockerHost("local", run=Runner()), bad)


class EngineControl(TempDirMixin, unittest.TestCase):
    def ctl(self, control, vast, paralon, docker):
        cfg = make_config(self.state_path, agent="me@gpu1")
        cfg.machines[0].agent_control = control
        cfg.machines[0].paralon_agent_unit = UNIT if control == "systemd" else ""
        c = Controller(cfg, vast, paralon, self.store, clock=lambda: 1000.0,
                       docker=docker)
        c.set_mode("dual")
        return c

    def test_systemd_stops_and_starts_the_unit(self):
        d = FakeDocker(agent=False)
        d.unit_state = ("loaded", "active")
        d.add("w1", "ghcr.io/paraloncloud/inference:1", "paralon-infer-1")
        v, p = FakeVast(running=1), FakeParalon(available=True)
        c = self.ctl("systemd", v, p, d)
        c.poll()
        self.assertEqual(d.writes, [("systemctl stop", UNIT), ("kill", ["w1"])])
        self.assertTrue(self.mstate()["took_off"]["agent"])
        v.running = 0
        c.poll()
        self.assertEqual(d.writes[-1], ("systemctl start", UNIT))

    def test_none_touches_nothing_and_waits_for_nothing(self):
        d = FakeDocker(agent=True, agent_running=True)
        v, p = FakeVast(running=1), FakeParalon(available=True)
        c = self.ctl("none", v, p, d)
        c.poll()
        self.assertEqual(d.writes, [])
        self.assertEqual(p.writes, [OFF])           # rule 1 still pauses Paralon
        self.assertFalse(self.mstate()["took_off"]["agent"])
        v.running = 0
        d.containers_[0].running = False             # an agent that is down
        c.poll()
        self.assertEqual(d.writes, [])
        self.assertEqual(p.writes[-1], ("available", "node-1", True))   # not held

    def test_docker_default_unchanged(self):
        d = FakeDocker()
        c = self.ctl("docker", FakeVast(running=1), FakeParalon(), d)
        c.poll()
        self.assertEqual(d.writes, [("stop", ["agent1"])])


class ConfigKeys(unittest.TestCase):
    ENV = {"PARALON_API_KEY": "prlc_test_secret"}

    def cfg(self, **entry):
        base = {"vast_machine_id": 1, "paralon_node_id": "n",
                "paralon_agent": "me@gpu1"}
        base.update(entry)
        return parse_config({"machines": [base]}, self.ENV)

    def test_default_and_valid(self):
        self.assertEqual(self.cfg().machines[0].agent_control, "docker")
        m = self.cfg(agent_control="systemd", paralon_agent_unit=UNIT).machines[0]
        self.assertEqual((m.agent_control, m.paralon_agent_unit), ("systemd", UNIT))
        self.assertEqual(self.cfg(agent_control="none").machines[0].agent_control,
                         "none")

    def test_systemd_needs_a_unit_and_bad_kind_refused(self):
        with self.assertRaises(ConfigError):
            self.cfg(agent_control="systemd")
        with self.assertRaises(ConfigError):
            self.cfg(agent_control="podman")


if __name__ == "__main__":
    unittest.main()
