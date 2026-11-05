"""v0.8.0 item 1: `doctor` -- each check passes with a good stub and fails
with a bad one; `run` refuses to start on a blocking FAIL; --skip-doctor."""

import json
import os
import subprocess
import unittest
from unittest import mock

from helpers import FakeDocker, FakeParalon, FakeVast, TempDirMixin

from vpdual import cli, doctor
from vpdual.doctor import FAIL, PASS, SKIP, WARN

KEY = "prlc_test_secret"


class Runner:
    """subprocess.run for vastai / systemctl, by argv."""

    def __init__(self, version="1.3.0", logged_in=True, unit="loaded",
                 active="active"):
        self.version, self.logged_in = version, logged_in
        self.unit, self.active = unit, active
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        out, rc = "", 0
        if argv[1:] == ["--version"]:
            out = self.version
        elif argv[1:3] == ["show", "user"]:
            rc, out = (0, '{"email": "never shown"}') if self.logged_in else (1, "")
        elif argv[0] == "systemctl":
            out = f"LoadState={self.unit}\nActiveState={self.active}\n"
        return subprocess.CompletedProcess(argv, rc, out, "")


class DoctorTests(TempDirMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.cfg_path = os.path.join(self.tmp, "config.json")
        self.raw = {"on_paralon_rental": "unlist_vast",
                    "state_file": self.state_path,
                    "vast_listing": {"price_gpu": 0.5, "price_min_bid": 0.3},
                    "machines": [{"vast_machine_id": 111,
                                  "paralon_node_id": "node-1",
                                  "paralon_agent": "me@gpu1"}]}
        self.runner, self.vast = Runner(), FakeVast()
        self.paralon, self.docker = FakeParalon(), FakeDocker()
        self.env = {"PARALON_API_KEY": KEY}
        self.which = lambda b: "/opt/bin/vastai"

    def rows(self, **kw):
        with open(self.cfg_path, "w") as fh:
            json.dump(self.raw, fh)
        args = dict(env=self.env, runner=self.runner, which=self.which,
                    vast=self.vast, paralon=self.paralon, docker=self.docker,
                    python=(3, 10, 0), sleep=lambda s: None)
        args.update(kw)
        return doctor.run_checks(self.cfg_path, **args)

    def status(self, rows, name):
        found = [r.status for r in rows if r.name == name]
        self.assertTrue(found, f"no row {name!r}: {rows}")
        return found[0]

    def test_all_pass(self):
        rows = self.rows()
        self.assertEqual({r.status for r in rows}, {PASS}, doctor.lines(rows))
        self.assertEqual(doctor.exit_code(rows), 0)
        self.assertNotIn("never shown", "\n".join(doctor.lines(rows)))

    def test_python_too_old(self):
        self.assertEqual(self.status(self.rows(python=(3, 7, 9)), "python"), FAIL)

    def test_vastai_missing(self):
        rows = self.rows(which=lambda b: None)
        self.assertEqual(self.status(rows, "vastai found"), FAIL)
        self.assertEqual(doctor.exit_code(rows), 1)

    def test_vastai_version(self):
        self.runner.version = "1.2.9"
        self.assertEqual(self.status(self.rows(), "vastai version"), FAIL)
        self.runner.version = "vastai 1.4.0"
        self.assertEqual(self.status(self.rows(), "vastai version"), WARN)

    def test_vastai_not_logged_in(self):
        self.runner.logged_in = False
        self.assertEqual(self.status(self.rows(), "vastai logged in"), FAIL)

    def test_key_missing_and_env_file_mode(self):
        rows = self.rows(env={})
        self.assertEqual(self.status(rows, "key present"), FAIL)
        env_file = os.path.join(self.tmp, "env")
        with open(env_file, "w") as fh:
            fh.write(f"PARALON_API_KEY={KEY}\n")
        os.chmod(env_file, 0o644)
        self.assertEqual(self.status(self.rows(env={}), "key present"), WARN)
        os.chmod(env_file, 0o600)
        self.assertEqual(self.status(self.rows(env={}), "key present"), PASS)

    def test_nodes_get_fails(self):
        self.paralon.fail_read = True
        rows = self.rows()
        self.assertEqual(self.status(rows, "GET /nodes"), FAIL)
        self.assertEqual(self.status(rows, "node node-1"), SKIP)

    def test_config_invalid_skips_the_rest(self):
        self.raw["poll_interval_s"] = 0
        rows = self.rows()
        self.assertEqual(self.status(rows, "loads and validates"), FAIL)
        self.assertIn(SKIP, [r.status for r in rows if r.group == "state file"])

    def test_ids_missing(self):
        self.raw["machines"][0].update(vast_machine_id=999,
                                       paralon_node_id="gone")
        rows = self.rows()
        self.assertEqual(self.status(rows, "vast 999"), FAIL)
        self.assertEqual(self.status(rows, "node gone"), FAIL)

    def test_agent_docker_fails(self):
        self.docker.fail_read = True
        rows = self.rows()
        self.assertEqual(self.status(rows, "me@gpu1"), FAIL)
        self.assertIn("ssh-copy-id me@gpu1", [r.hint for r in rows
                                              if r.name == "me@gpu1"][0])
        self.assertEqual(self.docker.writes, [])

    def test_no_agent_is_a_warning(self):
        del self.raw["machines"][0]["paralon_agent"]
        self.assertEqual(self.status(self.rows(), "machine 111"), WARN)

    def test_agent_control_systemd_and_none(self):
        self.raw["machines"][0].update(agent_control="systemd",
                                       paralon_agent_unit="paralon-agent.service")
        self.docker.unit_state = ("loaded", "active")
        name = "paralon-agent.service on me@gpu1"
        self.assertEqual(self.status(self.rows(), name), PASS)
        self.docker.unit_state = ("not-found", "inactive")
        self.assertEqual(self.status(self.rows(), name), FAIL)
        self.raw["machines"][0] = {"vast_machine_id": 111, "paralon_node_id": "node-1",
                                   "agent_control": "none"}
        self.assertEqual(self.status(self.rows(), "machine 111"), PASS)

    def test_relist_prices(self):
        names = [r for r in self.rows() if r.group == "relist prices"]
        self.assertEqual(names[0].status, PASS)
        del self.raw["vast_listing"]
        self.assertEqual(self.status(self.rows(), "machine 111"), WARN)
        st = self.store.load([111])
        st["machines"]["111"]["took_off"]["vast"] = True
        self.store.save(st)
        self.assertEqual(self.status(self.rows(), "machine 111"), FAIL)
        self.raw["vast_listing"] = {"price_gpu": 0.5, "price_min_bid": 0.3}
        self.assertEqual(self.status(self.rows(), "machine 111"), WARN)  # fallback used

    def test_state_not_writable_and_lock_held(self):
        self.raw["state_file"] = "/proc/nope/state.json"
        self.assertEqual(self.status(self.rows(), "writable"), FAIL)
        self.raw["state_file"] = self.state_path
        import fcntl
        with open(self.state_path + ".lock", "a") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            # flock is per open file: a second open in this process blocks too
            self.assertEqual(self.status(self.rows(), ".lock"), WARN)

    def test_service_not_installed_is_only_a_warning(self):
        self.runner.unit = "not-found"
        rows = self.rows()
        self.assertEqual(self.status(rows, "installed"), WARN)
        self.assertEqual(doctor.exit_code(rows), 0)
        self.runner.unit, self.runner.active = "loaded", "inactive"
        self.assertEqual(self.status(self.rows(), "active"), WARN)

    def test_blocking_is_the_first_five_groups(self):
        rows = [doctor.Check("relist prices", "m", FAIL, "x"),
                doctor.Check("state file", "w", FAIL, "x")]
        self.assertEqual(doctor.blocking(rows), [])
        rows.append(doctor.Check("Paralon agents", "a", FAIL, "x"))
        self.assertEqual(len(doctor.blocking(rows)), 1)


class RunRefusesTests(TempDirMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.cfg_path = os.path.join(self.tmp, "config.json")
        with open(self.cfg_path, "w") as fh:
            json.dump({"state_file": self.state_path,
                       "machines": [{"vast_machine_id": 111,
                                     "paralon_node_id": "node-1"}]}, fh)
        patcher = mock.patch.dict(os.environ, {"PARALON_API_KEY": KEY})
        patcher.start()
        self.addCleanup(patcher.stop)

    def main(self, *argv, fail=True):
        rows = [doctor.Check("Paralon key", "GET /nodes", FAIL if fail else PASS,
                             "HTTP 403", "permission")]
        with mock.patch.object(doctor, "run_checks", return_value=rows) as rc, \
                mock.patch.object(cli, "cmd_run", return_value=0) as run:
            code = cli.main(["-c", self.cfg_path, "run", *argv])
        return code, rc.called, run.called

    def test_run_refuses_on_a_blocking_fail(self):
        self.assertEqual(self.main(), (2, True, False))

    def test_run_starts_when_clean(self):
        self.assertEqual(self.main(fail=False), (0, True, True))

    def test_skip_doctor_proceeds(self):
        self.assertEqual(self.main("--skip-doctor"), (0, False, True))

    def test_doctor_command_exit_code(self):
        rows = [doctor.Check("config", "x", FAIL, "bad", "fix it")]
        with mock.patch.object(doctor, "run_checks", return_value=rows):
            self.assertEqual(cli.main(["-c", self.cfg_path, "doctor"]), 1)


if __name__ == "__main__":
    unittest.main()
