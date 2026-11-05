"""v0.8.0 item 3: notify_command -- the env and stdin it gets, the rate
limit, the hourly held-relist reminder, and a failing command never raises."""

import os
import subprocess
import unittest

from helpers import FakeParalon, FakeVast, TempDirMixin, make_config

from vpdual import notify
from vpdual.engine import Controller


class Runner:
    def __init__(self, rc=0, exc=None):
        self.rc, self.exc, self.calls = rc, exc, []

    def __call__(self, cmd, **kw):
        self.calls.append((cmd, kw["input"], {k: v for k, v in kw["env"].items()
                                              if k.startswith("VPDUAL_")}))
        if self.exc:
            raise self.exc
        return subprocess.CompletedProcess(cmd, self.rc, "", "")


class NotifyTests(TempDirMixin, unittest.TestCase):
    def ctl(self, vast, paralon, runner, dry_run=False, listing=None):
        self.now = [1000.0]
        cfg = make_config(self.state_path, "unlist_vast", listing=listing)
        cfg.notify_command = "notify-me"
        c = Controller(cfg, vast, paralon, self.store, dry_run=dry_run,
                       clock=lambda: self.now[0])
        c.notifier.runner, c.notifier.spawn = runner, (lambda fn: fn())
        if not dry_run:
            c.set_mode("dual")
        else:
            c.mstate(cfg.machines[0])["mode"] = "dual"
        return c

    def test_warning_runs_the_command_with_env_and_stdin(self):
        v, r = FakeVast(), Runner()
        v.fail_read = True
        self.ctl(v, FakeParalon(), r).poll()
        cmd, stdin, env = r.calls[0]
        self.assertEqual(cmd, "notify-me")
        self.assertIn("hold: Vast unreadable", stdin)
        self.assertEqual(env, {"VPDUAL_MACHINE": "111", "VPDUAL_LEVEL": "WARNING",
                               "VPDUAL_EVENT": "hold_vast_unreadable"})

    def test_confirmed_side_change(self):
        v, p, r = FakeVast(listed=True), FakeParalon(), Runner()
        c = self.ctl(v, p, r)
        c.poll()
        self.assertEqual(r.calls, [])               # first sight: no change
        v.listed = False
        p.available = False
        self.now[0] += 60
        c.poll()
        events = sorted(e["VPDUAL_EVENT"] for _, _, e in r.calls)
        self.assertEqual(events, ["paralon_paused", "vast_unlisted"])
        self.assertEqual({e["VPDUAL_LEVEL"] for _, _, e in r.calls}, {"INFO"})

    def test_rate_limit_per_machine_and_kind(self):
        v, r = FakeVast(), Runner()
        v.fail_read = True
        c = self.ctl(v, FakeParalon(), r)
        c.poll()
        self.now[0] += 300
        c.poll()
        self.assertEqual(len(r.calls), 1)
        self.now[0] += notify.RATE_S
        c.poll()
        self.assertEqual(len(r.calls), 2)

    def test_held_relist_repeats_hourly(self):
        n, r = notify.Notifier("x", clock=lambda: self.t, runner=Runner(),
                               spawn=lambda fn: fn()), None
        self.t = 0.0
        msg = "hold: cannot relist Vast: no saved price"
        n.warning(111, msg)
        self.t = notify.RATE_S + 1
        n.warning(111, msg)
        self.assertEqual(len(n.runner.calls), 1)
        self.t = notify.HELD_RELIST_S + 1
        n.warning(111, msg)
        self.assertEqual(len(n.runner.calls), 2)
        self.assertEqual(n.runner.calls[0][2]["VPDUAL_EVENT"], "relist_held")

    def test_failing_command_never_raises_and_logs_once(self):
        v = FakeVast()
        v.fail_read = True
        for r in (Runner(rc=1), Runner(exc=OSError("no such shell"))):
            c = self.ctl(v, FakeParalon(), r)
            with self.assertLogs("vpdual", "WARNING") as logs:
                c.poll()
                c.notifier.send(111, "WARNING", "other", "again")   # within 10 min
            failed = [x for x in logs.output if "notify_command failed" in x]
            self.assertEqual(len(failed), 1, logs.output)

    def test_dry_run_and_status_notify_nothing(self):
        v, r = FakeVast(), Runner()
        v.fail_read = True
        self.ctl(v, FakeParalon(), r, dry_run=True).poll()
        self.assertEqual(r.calls, [])

    def test_real_shell_gets_stdin_and_env(self):
        out = os.path.join(self.tmp, "got.txt")
        n = notify.Notifier(f'{{ cat; echo "$VPDUAL_MACHINE $VPDUAL_EVENT"; }} > {out}',
                            spawn=lambda fn: fn())
        n.send(7, "INFO", "vast_listed", "box: vast listed")
        with open(out) as fh:
            self.assertEqual(fh.read(), "box: vast listed\n7 vast_listed\n")


if __name__ == "__main__":
    unittest.main()
