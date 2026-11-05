"""v0.7.0 review fixes: the `run` service's status snapshot the web UI
serves (item 1)."""

import json
import os
import shutil
import tempfile
import unittest

from vpdual.cli import publish_status
from vpdual.state import StateStore
from vpdual.status import snapshot_path
from vpdual.webui import SNAPSHOT_MAX_AGE_PASSES, UIApp

from status_scenario import build_scenario


class Snapshot(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.cfg, self.ctl, self.vast, self.paralon, self.docker = \
            build_scenario(self.tmp)
        self.path = snapshot_path(self.cfg.state_file)

    def service_pass(self):
        ok = self.ctl.poll()
        publish_status(self.ctl, self.cfg, ok)
        return ok

    def ui(self, now):
        return UIApp(self.cfg, self.vast, self.paralon, docker=self.docker,
                     clock=lambda: now, monotonic=lambda: now)

    def calls(self):
        return (self.vast.reads, self.paralon.reads, self.docker.reads,
                self.docker.machine_reads)

    def test_written_after_a_pass_from_its_own_reads(self):
        self.ctl.poll()
        before = self.calls()
        publish_status(self.ctl, self.cfg, True)
        self.assertEqual(self.calls(), before)        # no read of its own
        with open(self.path, encoding="utf-8") as fh:
            snap = json.load(fh)
        self.assertEqual((snap["written_at"], snap["pass_ok"]), (1000, True))
        rows = {r["vast_machine_id"]: r for r in snap["machines"]}
        self.assertEqual(rows[111]["agent"]["state"], "running")
        self.assertIn("paralon-rental-x", rows[111]["machine"]["paralon_rentals"])
        self.assertIsNone(snap["read_error"])
        self.assertIsNotNone(snap["next_pass"])

    def test_a_held_pass_is_published_with_the_error(self):
        self.vast.fail_read = True
        ok = self.service_pass()
        with open(self.path, encoding="utf-8") as fh:
            snap = json.load(fh)
        self.assertFalse(snap["pass_ok"] or ok)
        self.assertTrue(snap["read_error"])

    def test_ui_serves_it_with_no_call(self):
        self.service_pass()
        before = self.calls()
        s = self.ui(1012.0).status()
        self.assertEqual(self.calls(), before)        # 0 vastai/Paralon/SSH
        self.assertEqual((s["source"], s["age_s"]), ("service", 12))
        self.assertTrue(s["ok"])

    def test_a_mode_saved_since_shows_at_once(self):
        self.service_pass()
        app = self.ui(1001.0)
        app.dry_run = False
        self.assertEqual(app.set_mode({"machine": 222, "mode": "dual"})[0], 200)
        rows = {r["vast_machine_id"]: r for r in app.status()["machines"]}
        self.assertEqual(rows[222]["mode"], "dual")

    def test_ui_falls_back_to_a_live_read_when_stale_or_missing(self):
        self.service_pass()
        stale = 1000.0 + SNAPSHOT_MAX_AGE_PASSES * self.cfg.poll_interval_s
        before = self.vast.reads
        s = self.ui(stale).status()
        self.assertEqual((s["source"], s["age_s"]), ("live", None))
        self.assertEqual(self.vast.reads, before + 1)
        os.unlink(self.path)
        self.assertEqual(self.ui(1001.0).status()["source"], "live")

    def test_dry_run_and_modeless_passes_publish_nothing(self):
        self.ctl.dry_run = True
        self.service_pass()
        self.assertFalse(os.path.exists(self.path))
        self.ctl.dry_run = False
        self.service_pass()
        self.assertTrue(os.path.exists(self.path))
        store = StateStore(self.cfg.state_file)
        st = store.load()
        for m in st["machines"].values():
            m["mode"] = None
        store.save(st)
        self.service_pass()                           # nothing read: removed
        self.assertFalse(os.path.exists(self.path))


if __name__ == "__main__":
    unittest.main()
