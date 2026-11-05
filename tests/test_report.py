"""v0.8.0 item 7: `status --json`, the bug-report attachment."""

import json
import platform
import unittest
from unittest import mock

from helpers import FakeDocker, FakeParalon, FakeVast, TempDirMixin, make_config

from vpdual import __version__, cli, doctor
from vpdual.engine import Controller


class StatusJson(TempDirMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.cfg = make_config(self.state_path, agent="hostuser@gpu-box.lan")
        self.vast, self.docker = FakeVast(), FakeDocker()
        self.docker.fail_read = True        # its error names the ssh target
        ctl = Controller(self.cfg, self.vast, FakeParalon(), self.store,
                         clock=lambda: 1000.0, docker=self.docker)
        ctl.set_mode("dual")
        st = self.store.load([111])
        st["machines"]["111"]["decisions"] = [
            {"t": f"2026-11-01T00:00:{i:02d}Z", "msg": f"decision {i}"}
            for i in range(12)]
        self.store.save(st)
        self.ctl = Controller(self.cfg, self.vast, FakeParalon(), self.store,
                              clock=lambda: 1000.0, docker=self.docker)

    def report(self):
        out = []
        rows = [doctor.Check("python + vastai", "python", doctor.PASS, "3.x"),
                doctor.Check("Paralon agents", "hostuser@gpu-box.lan", doctor.FAIL,
                             "hostuser@gpu-box.lan: refused", "ssh-copy-id")]
        with mock.patch.object(doctor, "run_checks", return_value=rows), \
                mock.patch.object(doctor, "vastai_version", return_value=(1, 3, 0)):
            cli.cmd_status(self.ctl, self.cfg, None, out=out.append,
                           as_json=True, cfg_path="/x/config.json")
        return out[0], json.loads(out[0])

    def test_has_what_a_bug_report_needs(self):
        _, data = self.report()
        self.assertEqual(data["version"], __version__)
        self.assertEqual(data["python"], platform.python_version())
        self.assertEqual(data["vastai_version"], "1.3.0")
        self.assertEqual(data["doctor"]["fail"], 1)
        self.assertEqual(data["doctor"]["problems"],
                         [{"group": "Paralon agents", "status": "FAIL",
                           "check": "agent"}])
        msgs = [d["msg"] for d in data["machines"][0]["decisions"]]
        self.assertEqual(msgs, [f"decision {i}" for i in range(2, 12)])

    def test_no_personal_data(self):
        text, _ = self.report()
        for secret in ("hostuser", "gpu-box", "prlc_test_secret", "ssh-copy-id",
                       "/x/config.json"):
            self.assertNotIn(secret, text)
        self.assertIn("user@host", text)          # where the target was


if __name__ == "__main__":
    unittest.main()
