"""v0.8.0 item 2: the armed switch. Unarmed = every decision, none of the
writes; a config without the key is armed (pre-v0.8.0 installs)."""

import json
import os
import unittest
from unittest import mock

from helpers import FakeDocker, FakeParalon, FakeVast, TempDirMixin, make_config
from test_settings import Base as SettingsBase

from vpdual import cli, doctor
from vpdual.config import ConfigError, parse_config
from vpdual.engine import Controller
from vpdual.status import status_data, status_lines
from vpdual.webui import PAGE

KEY = "prlc_test_secret"


class Unarmed(TempDirMixin, unittest.TestCase):
    def ctl(self, armed, vast, paralon, docker=None, policy="unlist_vast"):
        cfg = make_config(self.state_path, policy, agent="me@gpu1" if docker else "")
        cfg.armed = armed
        c = Controller(cfg, vast, paralon, self.store, clock=lambda: 1000.0,
                       docker=docker or FakeDocker())
        c.set_mode("dual")
        return c

    def test_unarmed_pass_writes_nothing_and_logs_would(self):
        # A Paralon rental in dual + unlist_vast: unlist Vast; and a Vast
        # renter elsewhere: pause Paralon, stop the agent.
        v, p = FakeVast(listed=True), FakeParalon(available=True, rented=True)
        with self.assertLogs("vpdual", "INFO") as logs:
            self.ctl(False, v, p).poll()
        self.assertEqual((v.writes, p.writes), ([], []))
        self.assertTrue(any("NOT ARMED: would take vast off" in ln
                            for ln in logs.output), logs.output)
        self.assertFalse(self.mstate()["took_off"]["vast"])   # no flag saved
        self.assertEqual(self.mstate()["mode"], "dual")        # the mode is

        v, p, d = FakeVast(running=1), FakeParalon(available=True), FakeDocker()
        with self.assertLogs("vpdual", "INFO") as logs:
            self.ctl(False, v, p, docker=d).poll()
        self.assertEqual((v.writes, p.writes, d.writes), ([], [], []))
        text = "\n".join(logs.output)
        self.assertIn("NOT ARMED: would take paralon off", text)
        self.assertIn("NOT ARMED: would Vast renter running: stop the Paralon agent",
                      text)

    def test_arming_flips_the_behaviour(self):
        v, p = FakeVast(listed=True), FakeParalon(available=True, rented=True)
        self.ctl(True, v, p).poll()
        self.assertEqual(v.writes, [("unlist", 111)])
        self.assertTrue(self.mstate()["took_off"]["vast"])

    def test_unarmed_docker_event_pauses_nothing(self):
        v, p, d = FakeVast(), FakeParalon(available=True), FakeDocker()
        c = self.ctl(False, v, p, docker=d)
        c.poll()                                    # cache the node
        self.assertTrue(c.container_started("me@gpu1", "C.42", "pytorch/pytorch"))
        self.assertEqual(p.writes, [])
        self.assertFalse(self.mstate()["took_off"]["paralon"])

    def test_status_carries_armed(self):
        c = self.ctl(False, FakeVast(), FakeParalon())
        s = status_data(c, c.cfg)
        self.assertIs(s["armed"], False)
        self.assertIn("NOT ARMED", "\n".join(status_lines(s)))


class ConfigKey(unittest.TestCase):
    RAW = {"machines": [{"vast_machine_id": 1, "paralon_node_id": "n"}]}

    def test_absent_is_armed(self):
        self.assertTrue(parse_config(dict(self.RAW), {"PARALON_API_KEY": KEY}).armed)

    def test_false_and_bad_values(self):
        env = {"PARALON_API_KEY": KEY}
        self.assertFalse(parse_config(dict(self.RAW, armed=False), env).armed)
        with self.assertRaises(ConfigError):
            parse_config(dict(self.RAW, armed="false"), env)


class ArmCommand(TempDirMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.cfg_path = os.path.join(self.tmp, "config.json")
        with open(self.cfg_path, "w") as fh:
            json.dump({"armed": False, "machines": [
                {"vast_machine_id": 1, "paralon_node_id": "n"}]}, fh, indent=2)
        self.out = []

    def arm(self, rows):
        with mock.patch.object(doctor, "run_checks", return_value=rows):
            return cli.cmd_arm(self.cfg_path, out=self.out.append)

    def raw(self):
        with open(self.cfg_path) as fh:
            return json.load(fh)

    def test_arm_refuses_on_a_blocking_fail(self):
        rows = [doctor.Check("config", "loads and validates", doctor.FAIL, "x")]
        self.assertEqual(self.arm(rows), 2)
        self.assertIs(self.raw()["armed"], False)

    def test_arm_sets_true_and_keeps_a_backup(self):
        self.assertEqual(self.arm([]), 0, self.out)
        self.assertIs(self.raw()["armed"], True)
        self.assertTrue(any(f.startswith("config.json.bak-")
                            for f in os.listdir(self.tmp)))
        self.assertIn("Restart the service", self.out[-1])


class SettingsArming(SettingsBase):
    def test_banner_is_on_the_page(self):
        self.assertIn('id="not-armed"', PAGE)
        self.assertIn("Not armed: decisions are logged, nothing is changed. "
                      "Arm it in Settings.", PAGE)

    def test_arming_in_settings_runs_doctor(self):
        self.start()
        ran = []
        self.app.doctor = lambda path: ran.append(path) or [
            doctor.Check("Paralon agents", "me@gpu1", doctor.FAIL, "ssh refused")]
        _, got, _ = self.get_config()
        cfg = dict(got["config"], armed=False)
        self.assertEqual(self.save(cfg, base=got["base"])[0], 200)
        self.assertEqual(ran, [])                   # disarming runs nothing
        _, got, _ = self.get_config()
        code, reply, _ = self.save(dict(got["config"], armed=True),
                                   base=got["base"])
        self.assertEqual((code, ran), (200, [self.cfg_path]), reply)
        self.assertIn("ssh refused", reply["message"])
        self.assertIs(self.file_raw()["armed"], True)


if __name__ == "__main__":
    unittest.main()
