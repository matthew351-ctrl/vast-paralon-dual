"""v0.7.0 review fixes: the `run` service's status snapshot the web UI
serves (item 1)."""

import io
import json
import os
import re
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr
from urllib.parse import urlencode

from vpdual.cli import build_parser, publish_status
from vpdual.engine import RELIST_BACKOFF_S, RELIST_FAST_TRIES, Controller
from vpdual.state import StateStore
from vpdual.status import snapshot_path
from vpdual.webui import (LOCKOUT_FAILS, LOCKOUT_S, PASSWORD_ENV,
                          SNAPSHOT_MAX_AGE_PASSES, TOKEN_HEADER, Auth, UIApp,
                          bind_warning, serve)

from helpers import FakeParalon, FakeVast, TempDirMixin, make_config
from status_scenario import build_scenario
from test_webui import Server


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


class RefusingVast(FakeVast):
    """`vastai list machine` exits 0 but Vast does not list the machine."""
    refuse = True

    def list_machine(self, machine_id, listing):
        self.writes.append(("list", machine_id, dict(listing)))
        if not self.refuse:
            self.machines[machine_id]["listed"] = True


class RelistBackoff(TempDirMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.now = [1000.0]
        self.vast = RefusingVast(listed=False)
        cfg = make_config(self.state_path, "unlist_vast")
        self.store.save({"machines": {"111": {
            "mode": "dual",
            "took_off": {"vast": True, "paralon": False, "agent": False}}}})
        self.ctl = Controller(cfg, self.vast, FakeParalon(), self.store,
                              clock=lambda: self.now[0])

    def lists(self):
        return sum(1 for w in self.vast.writes if w[0] == "list")

    def test_five_at_poll_rate_then_every_ten_minutes(self):
        with self.assertLogs("vpdual", "WARNING") as logs:
            for _ in range(8):
                self.ctl.poll()
                self.now[0] += 30
        self.assertEqual(self.lists(), RELIST_FAST_TRIES)
        warned = [r for r in logs.output if "not accepted the relist" in r]
        self.assertEqual(len(warned), 1)
        self.assertIn("retrying every 10 min", warned[0])
        st = self.mstate()
        self.assertEqual(st["relist_attempts"], RELIST_FAST_TRIES)
        self.assertEqual(st["relist_next_at"], 1000.0 + 4 * 30 + RELIST_BACKOFF_S)
        self.now[0] = st["relist_next_at"]
        self.ctl.poll()
        self.assertEqual(self.lists(), RELIST_FAST_TRIES + 1)
        self.ctl.poll()
        self.assertEqual(self.lists(), RELIST_FAST_TRIES + 1)   # throttled

    def test_cleared_when_confirmed_back(self):
        for _ in range(RELIST_FAST_TRIES + 1):
            self.ctl.poll()
        self.vast.refuse = False
        self.now[0] += RELIST_BACKOFF_S
        self.ctl.poll()                       # accepted this time
        self.ctl.poll()                       # the read confirms it
        st = self.mstate()
        self.assertFalse(st["took_off"]["vast"])
        self.assertEqual((st["relist_attempts"], st["relist_next_at"]), (0, None))

    def test_state_file_keeps_both_keys(self):
        self.ctl.poll()
        with open(self.state_path, encoding="utf-8") as fh:
            raw = json.load(fh)["machines"]["111"]
        self.assertEqual(raw["relist_attempts"], 1)
        raw.update(relist_attempts=True, relist_next_at="soon")
        self.assertEqual(StateStore(self.state_path).load()["machines"]["111"]
                         ["relist_attempts"], 1)   # unchanged on disk
        from vpdual.state import _clean
        bad = _clean(raw)
        self.assertEqual((bad["relist_attempts"], bad["relist_next_at"]),
                         (0, None))


PW = "synthetic-test-password"


class Login(Server):
    def start(self, **kw):
        super().start(**kw)
        self.t = [5000.0]
        self.slept = []
        self.app.auth = Auth(PW, clock=lambda: self.t[0],
                             sleep=self.slept.append)

    def login_form(self):
        resp, text = self.request("GET", "/login")
        self.assertEqual(resp.status, 200)
        pre = re.search(r'name="csrf" value="([^"]+)"', text).group(1)
        self.assertIn(f"vpdual_login={pre}", resp.getheader("Set-Cookie"))
        return pre

    def post_login(self, password, pre=None):
        pre = pre or self.login_form()
        body = urlencode({"password": password, "csrf": pre}).encode()
        return self.request("POST", "/login", body, {
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": f"http://127.0.0.1:{self.port}",
            "Cookie": f"vpdual_login={pre}"})

    def session(self):
        resp, _ = self.post_login(PW)
        self.assertEqual((resp.status, resp.getheader("Location")), (303, "/"))
        cookie = resp.getheader("Set-Cookie")
        for attr in ("HttpOnly", "SameSite=Strict", f"Max-Age={12 * 3600}"):
            self.assertIn(attr, cookie)
        sid = re.search(r"vpdual_session=([^;]+)", cookie).group(1)
        self.assertGreaterEqual(len(sid), 43)          # 256 bits, base64
        return f"vpdual_session={sid}"

    def test_login_required_when_set(self):
        self.start()
        resp, _ = self.request("GET", "/")
        self.assertEqual((resp.status, resp.getheader("Location")),
                         (303, "/login"))
        for path in ("/api/status", "/api/config", "/api/discover"):
            self.assertEqual(self.request("GET", path)[0].status, 401, path)
        cookie = self.session()
        resp, text = self.request("GET", "/", headers={"Cookie": cookie})
        self.assertEqual(resp.status, 200)
        self.assertIn('name="vpdual-login" content="1"', text)
        self.assertEqual(self.request("GET", "/api/status",
                                      headers={"Cookie": cookie})[0].status, 200)
        self.t[0] += 12 * 3600                         # the session expires
        self.assertEqual(self.request("GET", "/api/status",
                                      headers={"Cookie": cookie})[0].status, 401)

    def test_post_without_session_is_401_and_token_still_needed(self):
        self.start()
        code, _ = self.post_mode({"machine": 111, "mode": "vast"})
        self.assertEqual(code, 401)
        cookie = self.session()
        code, _ = self.post_mode({"machine": 111, "mode": "vast"}, token=False,
                                 extra={"Cookie": cookie})
        self.assertEqual(code, 403)
        code, j = self.post_mode({"machine": 111, "mode": "vast"},
                                 extra={"Cookie": cookie})
        self.assertEqual((code, j["saved"]), (200, True))

    def test_logout_ends_the_session(self):
        self.start()
        cookie = self.session()
        resp, _ = self.request("POST", "/logout", b"{}", {
            "Cookie": cookie, TOKEN_HEADER: self.app.token,
            "Content-Type": "application/json"})
        self.assertEqual(resp.status, 200)
        self.assertEqual(self.request("GET", "/api/status",
                                      headers={"Cookie": cookie})[0].status, 401)

    def test_wrong_password_delays_then_locks_out(self):
        self.start()
        for _ in range(LOCKOUT_FAILS):
            self.assertEqual(self.post_login("wrong")[0].status, 401)
        self.assertEqual(self.slept, [1.0] * LOCKOUT_FAILS)
        resp, text = self.post_login(PW)               # right, but locked
        self.assertEqual(resp.status, 429)
        self.assertIn("wait 5 minutes", text)
        self.t[0] += LOCKOUT_S
        self.assertEqual(self.post_login(PW)[0].status, 303)

    def test_login_form_csrf_and_origin(self):
        self.start()
        pre = self.login_form()
        body = urlencode({"password": PW, "csrf": pre}).encode()
        resp, _ = self.request("POST", "/login", body, {
            "Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(resp.status, 403)             # no pre-login cookie
        resp, _ = self.request("POST", "/login", body, {
            "Cookie": f"vpdual_login={pre}", "Origin": "http://evil.example"})
        self.assertEqual(resp.status, 403)             # cross-origin

    def test_no_password_no_login(self):
        super().start()                                # Auth(None)
        self.assertEqual(self.request("GET", "/api/status")[0].status, 200)
        self.assertEqual(self.request("GET", "/login")[0].status, 303)


class NonLoopbackBind(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        self.cfg, _, self.vast, self.paralon, self.docker = build_scenario(tmp)

    def test_refused_without_password(self):
        err = io.StringIO()
        with redirect_stderr(err):
            code = serve(self.cfg, self.vast, self.paralon, host="0.0.0.0",
                         port=0, docker=self.docker)
        self.assertEqual(code, 2)
        self.assertIn(PASSWORD_ENV, err.getvalue())
        self.assertIn("--no-login", err.getvalue())

    def test_warning_names_the_env_var(self):
        self.assertIn(PASSWORD_ENV, bind_warning("0.0.0.0", 8787))
        self.assertIn("plain HTTP", bind_warning("0.0.0.0", 8787, login=True))
        args = build_parser().parse_args(["ui", "--host", "0.0.0.0",
                                          "--no-login"])
        self.assertTrue(args.no_login)


if __name__ == "__main__":
    unittest.main()
