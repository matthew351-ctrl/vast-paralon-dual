"""`ui`: status_data, the HTTP server, and the mode switch (save-only)."""

import http.client
import json
import os
import shutil
import tempfile
import threading
import unittest

from vpdual.cli import build_parser
from vpdual.config import (ConfigError, Machine, MAX_NAME, clean_name,
                           parse_config)
from vpdual.state import StateStore
from vpdual.status import display_name, status_data
from vpdual.vast import VastError
from vpdual.webui import (PAGE, DEFAULT_HOST, DEFAULT_PORT, STATUS_MAX_AGE_S,
                          TOKEN_HEADER, UIApp, allowed_names, bind_warning,
                          host_allowed, make_server)

from status_scenario import KEY, NAME_111, build_scenario

MACHINE_KEYS = {
    "vast_machine_id", "paralon_node_id", "mode", "took_off",
    "idle_job_stopped", "paralon_agent", "agent", "machine", "saved_listing",
    "saved_listing_text", "relist", "relist_text", "vast", "paralon",
    "warnings", "decisions", "name", "name_from"}


class Scenario(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.state_path = os.path.join(self.tmp, "state.json")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def scenario(self, **kw):
        self.cfg, self.ctl, self.vast, self.paralon, self.docker = \
            build_scenario(self.tmp, **kw)
        return self.cfg

    def state_text(self):
        with open(self.state_path, encoding="utf-8") as fh:
            return fh.read()

    def mode_of(self, mid):
        return StateStore(self.state_path).load()["machines"][str(mid)]["mode"]


class StatusDataShape(Scenario):
    def test_shape_and_json_ready(self):
        self.scenario()
        s = status_data(self.ctl, self.cfg)
        self.assertEqual(set(s), {"policy", "read_error", "machines", "armed",
                                  "next_pass", "removed_owing"})
        self.assertEqual(json.loads(json.dumps(s)), s)   # plain JSON types
        self.assertIsNone(s["read_error"])
        self.assertEqual([m["vast_machine_id"] for m in s["machines"]],
                         [111, 222, 333, 444])
        for m in s["machines"]:
            self.assertEqual(set(m), MACHINE_KEYS)
        m111, m222, m333, m444 = s["machines"]
        self.assertEqual(m111["agent"], {"state": "running", "error": None})
        self.assertEqual(m111["machine"]["paralon_rentals"], ["paralon-rental-x"])
        self.assertEqual(m111["idle_job_stopped"], ["C.777"])
        self.assertEqual(m111["paralon"]["spot"], False)
        self.assertEqual(len(m111["warnings"]), 1)
        self.assertEqual(len(m111["decisions"]), 5)
        self.assertEqual(m111["decisions"][-1]["count"], 3)
        self.assertEqual(m222["vast"]["running"], 1)
        self.assertIsNone(m222["agent"])
        self.assertEqual(m333["vast"]["error"], "not in your account")
        self.assertIsNone(m444["mode"])
        self.assertIsNone(m444["relist"])
        self.assertEqual(m444["paralon"]["error"], "node not found in GET /nodes")
        self.assertIn({"machine": 222, "name": "222",
                       "msg": "take paralon off"},
                      s["next_pass"])
        self.assertEqual(s["removed_owing"],
                         [{"vast_machine_id": "999", "took_off":
                           {"vast": True, "paralon": False, "agent": False}}])

    def test_read_failure(self):
        self.scenario(vast_fail=True)
        s = status_data(self.ctl, self.cfg)
        self.assertEqual(s["read_error"], "cli error")
        self.assertIsNone(s["next_pass"])
        self.assertTrue(all(m["vast"] is None and m["paralon"] is None
                            for m in s["machines"]))

    def test_writes_nothing(self):
        self.scenario()
        before = self.state_text()
        status_data(self.ctl, self.cfg)
        self.assertEqual(self.state_text(), before)
        self.assertEqual(self.vast.writes + self.paralon.writes
                         + self.docker.writes, [])

    def test_fresh_ok_false_never_forces_get_nodes(self):
        self.scenario()
        self.ctl.paralon.list_nodes()          # node-1 rented in the cache
        self.ctl.recovering.add(111)           # would force a read every 30 s
        status_data(self.ctl, self.cfg, fresh_ok=False)
        self.assertEqual(self.paralon.refreshes, 0)
        status_data(self.ctl, self.cfg)        # `status` keeps its old read
        self.assertEqual(self.paralon.refreshes, 1)


class Server(Scenario):
    """A real server on 127.0.0.1, an OS-chosen port, fakes behind it."""

    def start(self, dry_run=False, **kw):
        self.scenario(**kw)
        self.now = [1000.0]
        self.app = UIApp(self.cfg, self.vast, self.paralon, dry_run=dry_run,
                         docker=self.docker, clock=lambda: 1000.0,
                         monotonic=lambda: self.now[0])
        self.server = make_server(self.app, port=0)
        self.port = self.server.server_address[1]
        t = threading.Thread(target=self.server.serve_forever,
                             kwargs={"poll_interval": 0.05}, daemon=True)
        t.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, method, path, body=None, headers=None, host=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        hdrs = {"Host": host or f"127.0.0.1:{self.port}"}
        hdrs.update(headers or {})
        data = None
        if body is not None:
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
        conn.request(method, path, body=data, headers=hdrs)
        resp = conn.getresponse()
        text = resp.read().decode()
        conn.close()
        return resp, text

    def post_mode(self, body, token=True, origin=True, ctype="application/json",
                  extra=None):
        h = {"Content-Type": ctype}
        if token is True:
            h[TOKEN_HEADER] = self.app.token
        elif token:
            h[TOKEN_HEADER] = token
        if origin is True:
            h["Origin"] = f"http://127.0.0.1:{self.port}"
        elif origin:
            h["Origin"] = origin
        h.update(extra or {})
        resp, text = self.request("POST", "/api/mode", body, h)
        return resp.status, json.loads(text)


class Pages(Server):
    def test_page_carries_the_token_and_strict_headers(self):
        self.start()
        resp, text = self.request("GET", "/")
        self.assertEqual(resp.status, 200)
        self.assertIn(self.app.token, text)
        csp = resp.getheader("Content-Security-Policy")
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertIn("connect-src 'self'", csp)
        nonce = csp.split("'nonce-")[1].split("'")[0]
        self.assertIn(f'<script nonce="{nonce}">', text)
        self.assertEqual(resp.getheader("Cache-Control"), "no-store")
        self.assertEqual(resp.getheader("X-Frame-Options"), "DENY")
        # works offline: nothing loaded from anywhere else
        self.assertNotIn("https://", text)
        self.assertNotIn("http://", text)

    def test_status_json(self):
        self.start()
        resp, text = self.request("GET", "/api/status")
        self.assertEqual(resp.status, 200)
        s = json.loads(text)
        self.assertTrue(s["ok"])
        self.assertEqual(s["modes"], ["vast", "paralon", "dual"])
        self.assertEqual(s["apply_within_s"], self.cfg.poll_interval_s)
        self.assertEqual(len(s["machines"]), 4)
        self.assertEqual(self.vast.writes + self.paralon.writes
                         + self.docker.writes, [])

    def test_status_is_cached_and_never_forces_get_nodes(self):
        self.start()
        self.request("GET", "/api/status")
        self.request("GET", "/api/status")
        self.assertEqual(self.vast.reads, 1)          # one read, two pages
        self.now[0] += STATUS_MAX_AGE_S
        self.request("GET", "/api/status")
        self.assertEqual(self.vast.reads, 2)
        self.assertEqual(self.paralon.refreshes, 0)   # node-1 is rented

    def test_read_failure_is_a_banner_not_an_error(self):
        self.start(paralon_fail=True)
        resp, text = self.request("GET", "/api/status")
        self.assertEqual(resp.status, 200)
        self.assertEqual(json.loads(text)["read_error"], "HTTP 503")

    def test_unknown_path_404(self):
        self.start()
        self.assertEqual(self.request("GET", "/etc/passwd")[0].status, 404)
        resp, _ = self.request("POST", "/api/other", b"{}",
                               {TOKEN_HEADER: self.app.token,
                                "Content-Type": "application/json"})
        self.assertEqual(resp.status, 404)


class NeverLeaksTheKey(Server):
    def test_key_absent_from_page_and_json(self):
        self.start()
        for path in ("/", "/api/status"):
            _, text = self.request("GET", path)
            self.assertNotIn(KEY, text)
            self.assertNotIn("paralon_api_key", text)
            self.assertNotIn(self.state_path, text)    # not the config either
        code, reply = self.post_mode({"machine": 111, "mode": "vast"})
        self.assertNotIn(KEY, json.dumps(reply))

    def test_key_in_an_error_message_is_redacted(self):
        self.start()

        def leaky():
            raise VastError(f"GET https://console.vast.ai/api/v0/machines?"
                            f"api_key=vastsecret123&x=1 failed; {KEY}; "
                            "Authorization: Bearer tok_abc.def")
        self.vast.read_machines = leaky
        _, text = self.request("GET", "/api/status")
        s = json.loads(text)                            # still valid JSON
        for secret in (KEY, "vastsecret123", "tok_abc.def"):
            self.assertNotIn(secret, text)
        self.assertIn("api_key=[redacted]", s["read_error"])


class ModeSwitchProtection(Server):
    def test_saved_with_token_and_same_origin(self):
        self.start()
        code, reply = self.post_mode({"machine": 222, "mode": "dual"})
        self.assertEqual((code, reply["ok"], reply["saved"]), (200, True, True))
        self.assertIn("next pass", reply["message"])
        self.assertEqual(self.mode_of(222), "dual")

    def test_no_origin_with_token_is_allowed(self):     # curl
        self.start()
        self.assertEqual(self.post_mode({"machine": 222, "mode": "dual"},
                                        origin=False)[0], 200)

    def test_missing_or_wrong_token_refused(self):
        self.start()
        before = self.state_text()
        for token in (False, "wrong", self.app.token + "x", "é"):
            code, reply = self.post_mode({"machine": 222, "mode": "dual"},
                                         token=token)
            self.assertEqual(code, 403, token)
            self.assertIn("token", reply["error"])
        self.assertEqual(self.state_text(), before)

    def test_cross_origin_refused_even_with_the_token(self):
        self.start()
        before = self.state_text()
        for origin in ("http://evil.example", "null",
                       f"https://127.0.0.1:{self.port}",
                       f"http://127.0.0.1:{self.port + 1}"):
            self.assertEqual(self.post_mode({"machine": 222, "mode": "dual"},
                                            origin=origin)[0], 403, origin)
        self.assertEqual(self.post_mode(
            {"machine": 222, "mode": "dual"},
            extra={"Sec-Fetch-Site": "cross-site"})[0], 403)
        self.assertEqual(self.state_text(), before)

    def test_rebinding_host_refused(self):
        self.start()
        for method, path in (("GET", "/"), ("GET", "/api/status")):
            resp, text = self.request(method, path,
                                      host=f"evil.example:{self.port}")
            self.assertEqual(resp.status, 403)
            self.assertNotIn(self.app.token, text)
        resp, _ = self.request(
            "POST", "/api/mode", b'{"machine": 222, "mode": "dual"}',
            {TOKEN_HEADER: self.app.token, "Content-Type": "application/json",
             "Origin": f"http://evil.example:{self.port}"},
            host=f"evil.example:{self.port}")
        self.assertEqual(resp.status, 403)
        self.assertEqual(self.mode_of(222), "vast")

    def test_host_rules(self):
        names = allowed_names("127.0.0.1")
        for ok in ("127.0.0.1:8787", "localhost:8787", "[::1]:8787",
                   "192.168.1.5:8787", "LOCALHOST", "localhost.:1"):
            self.assertTrue(host_allowed(ok, names), ok)
        for bad in ("", None, "evil.example:8787", "127.0.0.1.evil.example",
                    "[::1"):
            self.assertFalse(host_allowed(bad, names), bad)


class BadInput(Server):
    def test_400s(self):
        self.start()
        before = self.state_text()
        for body in ({"machine": 999, "mode": "dual"},     # removed machine
                     {"machine": 5, "mode": "dual"},
                     {"machine": 222, "mode": "both"},
                     {"machine": 222},
                     {"mode": "dual"},
                     {"machine": True, "mode": "dual"},
                     {"machine": 222.0, "mode": "dual"},
                     {"machine": "22x", "mode": "dual"},
                     {"machine": 222, "mode": ["dual"]},
                     [222, "dual"], "dual", None):
            code, reply = self.post_mode(body)
            self.assertEqual(code, 400, body)
            self.assertFalse(reply["ok"])
        self.assertEqual(self.post_mode(b"{not json")[0], 400)
        self.assertEqual(self.post_mode(b"\xff\xfe")[0], 400)
        self.assertEqual(self.post_mode({"machine": 222, "mode": "dual"},
                                        ctype="text/plain")[0], 415)
        self.assertEqual(self.post_mode(b"x" * 5000)[0], 413)
        self.assertEqual(self.state_text(), before)

    def test_machine_id_as_digits_string(self):
        self.start()
        self.assertEqual(self.post_mode({"machine": "222", "mode": "dual"})[0],
                         200)
        self.assertEqual(self.mode_of(222), "dual")


class SaveOnly(Server):
    def test_switch_saves_and_runs_no_pass(self):
        self.start()
        code, reply = self.post_mode({"machine": 111, "mode": "vast"})
        self.assertEqual(code, 200)
        self.assertEqual(self.mode_of(111), "vast")
        # nothing read, nothing written on either side or the machine
        self.assertEqual((self.vast.reads, self.paralon.reads), (0, 0))
        self.assertEqual(self.docker.machine_reads + self.docker.reads, 0)
        self.assertEqual(self.vast.writes + self.paralon.writes
                         + self.docker.writes, [])
        st = StateStore(self.state_path).load()["machines"]["111"]
        self.assertEqual(st["decisions"][-1]["msg"], "mode set to vast")
        self.assertTrue(st["took_off"]["vast"])     # owed relist untouched
        self.assertEqual(st["idle_job"][0]["id"], 777)

    def test_other_machines_untouched(self):
        self.start()
        before = StateStore(self.state_path).load()["machines"]
        self.post_mode({"machine": 111, "mode": "vast"})
        after = StateStore(self.state_path).load()["machines"]
        for mid in ("222", "333", "444", "999"):
            self.assertEqual(after[mid], before[mid], mid)

    def test_status_shows_the_new_mode_at_once(self):
        self.start()
        self.request("GET", "/api/status")                 # cached
        self.post_mode({"machine": 444, "mode": "paralon"})
        s = json.loads(self.request("GET", "/api/status")[1])
        self.assertEqual(s["machines"][3]["mode"], "paralon")
        self.assertEqual(self.vast.writes + self.paralon.writes, [])

    def test_dry_run_saves_nothing(self):
        self.start(dry_run=True)
        before = self.state_text()
        code, reply = self.post_mode({"machine": 111, "mode": "vast"})
        self.assertEqual((code, reply["saved"]), (200, False))
        self.assertEqual(self.state_text(), before)

    def test_waits_for_the_services_lock(self):
        """A pass in the `run` service holds the state lock: the save waits
        for it, then re-reads the file, so the pass's writes are kept."""
        import fcntl
        self.start()
        lock = open(self.state_path + ".lock", "w")
        fcntl.flock(lock, fcntl.LOCK_EX)
        result = {}
        t = threading.Thread(target=lambda: result.update(
            r=self.post_mode({"machine": 222, "mode": "dual"})))
        t.start()
        t.join(0.5)
        self.assertTrue(t.is_alive())                # blocked on the lock
        # the "service" writes during its pass, then releases
        store = StateStore(self.state_path)
        st = store.load()
        st["machines"]["333"]["took_off"]["vast"] = True
        store.save(st)
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()
        t.join(10)
        self.assertEqual(result["r"][0], 200)
        after = StateStore(self.state_path).load()["machines"]
        self.assertEqual(after["222"]["mode"], "dual")
        self.assertTrue(after["333"]["took_off"]["vast"])   # not lost


class MachineNames(Scenario):
    """Display name: the config's `name`, else Vast's `hostname`, else the
    Vast id. Never a key: the mode switch takes the id."""

    def test_fallback_chain(self):
        self.scenario(names=True)
        s = status_data(self.ctl, self.cfg)
        got = {m["vast_machine_id"]: (m["name"], m["name_from"])
               for m in s["machines"]}
        self.assertEqual(got, {
            111: (NAME_111, "config"),          # config beats the hostname
            222: ("gpu-host-b", "vast"),
            333: ("333", "id"),                 # Vast cannot read it
            444: ("gpu box 4", "vast")})        # hostname tidied
        self.assertIn({"machine": 222, "name": "gpu-host-b",
                       "msg": "take paralon off"}, s["next_pass"])

    def test_no_read_falls_back_to_the_id(self):
        self.scenario(names=True, vast_fail=True)
        s = status_data(self.ctl, self.cfg)
        self.assertEqual([m["name"] for m in s["machines"]],
                         [NAME_111, "222", "333", "444"])

    def test_display_name_unit(self):
        class VM:
            raw = {"hostname": "box\x07one"}
        self.assertEqual(display_name(Machine(5, "n"), None), ("5", "id"))
        self.assertEqual(display_name(Machine(5, "n"), VastError("x")),
                         ("5", "id"))
        self.assertEqual(display_name(Machine(5, "n"), VM()),
                         ("box one", "vast"))
        self.assertEqual(display_name(Machine(5, "n", name="Mine"), VM()),
                         ("Mine", "config"))
        self.assertEqual(clean_name(None), "")
        self.assertEqual(clean_name("x" * 100), "x" * MAX_NAME)

    def test_config_name_validated(self):
        def cfg(name):
            return parse_config({"machines": [
                {"vast_machine_id": 1, "paralon_node_id": "n", "name": name}]},
                env={"PARALON_API_KEY": "k"})
        self.assertEqual(cfg("GPU host A").machines[0].name, "GPU host A")
        self.assertEqual(cfg("<b>&\"'</b>").machines[0].name, "<b>&\"'</b>")
        self.assertEqual(cfg("").machines[0].name, "")          # = unset
        for bad in (5, None, ["a"], " lead", "trail ", "two  spaces",
                    "line\nbreak", "tab\there", "x" * (MAX_NAME + 1)):
            with self.assertRaises(ConfigError, msg=repr(bad)):
                cfg(bad)


class NamesInThePage(Server):
    ODD = "<img src=x onerror=alert(1)> & \"quotes\" 'n' </script>"

    def test_odd_name_is_data_never_markup(self):
        self.start()
        self.cfg.machines[0].name = self.ODD
        _, page = self.request("GET", "/")
        self.assertNotIn("<img src=x", page)    # the page has no data in it
        resp, text = self.request("GET", "/api/status")
        self.assertEqual(resp.getheader("X-Content-Type-Options"), "nosniff")
        for raw in ("<img", "</script>", "& "):
            self.assertNotIn(raw, text)         # \u003c etc. in the JSON
        self.assertEqual(json.loads(text)["machines"][0]["name"], self.ODD)

    def test_page_renders_text_only(self):
        # every string reaches the DOM through textContent / createElement
        for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML",
                     "document.write", "eval(", "new Function"):
            self.assertNotIn(sink, PAGE)

    def test_colours_come_only_from_the_tokens(self):
        import re
        head, _, rest = PAGE.partition("/* what the page uses")
        tokens, _, rules = rest.partition("* { box-sizing")
        rules = rules.split("</style>")[0]
        self.assertTrue(rules)
        self.assertEqual(re.findall(r"#[0-9a-fA-F]{3,8}\b|rgba?\(", rules), [])
        self.assertEqual(re.findall(r"#[0-9a-fA-F]{3,8}\b", tokens), [])
        # the saved theme is applied before the CSS (no flash), then the
        # switch: Auto / Light / Dark
        self.assertLess(PAGE.index('localStorage.getItem("vpdual.theme")'),
                        PAGE.index("<style"))
        for choice in ("auto", "light", "dark"):
            self.assertIn(f'data-theme-choice="{choice}"', PAGE)
        self.assertIn(':root[data-theme="light"]', PAGE)
        self.assertIn('@media (prefers-color-scheme: light)', PAGE)

    def test_tooltips_cannot_be_clipped_or_hidden(self):
        # one fixed layer, re-parented into an open modal <dialog> (the top
        # layer), placed against the visible viewport, never innerWidth/
        # innerHeight (they include scrollbars)
        self.assertIn('<div id="tip" role="tooltip" hidden></div>', PAGE)
        self.assertIn("#tip { position: fixed; z-index: 1000;", PAGE)
        self.assertIn('b.closest("dialog[open]") || document.body', PAGE)
        self.assertIn("window.visualViewport", PAGE)
        self.assertIn('tip.classList.add("capped")', PAGE)
        self.assertNotIn("innerHeight", PAGE)
        self.assertNotIn("innerWidth", PAGE)

    def test_mode_switch_takes_the_id_not_the_name(self):
        self.start(names=True)
        code, reply = self.post_mode({"machine": NAME_111, "mode": "vast"})
        self.assertEqual(code, 400)
        self.assertEqual(self.mode_of(111), "dual")


class Binding(unittest.TestCase):
    def test_cli_defaults_to_loopback_8787(self):
        args = build_parser().parse_args(["ui"])
        self.assertEqual((args.host, args.port), ("127.0.0.1", 8787))
        self.assertEqual((DEFAULT_HOST, DEFAULT_PORT), ("127.0.0.1", 8787))

    def test_make_server_binds_loopback_by_default(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        cfg, ctl, vast, paralon, docker = build_scenario(tmp)
        server = make_server(UIApp(cfg, vast, paralon, docker=docker), port=0)
        self.addCleanup(server.server_close)
        self.assertEqual(server.server_address[0], "127.0.0.1")

    def test_warning_only_off_loopback(self):
        for host in ("127.0.0.1", "localhost", "::1", "127.0.0.2"):
            self.assertIsNone(bind_warning(host, 8787), host)
        for host in ("0.0.0.0", "192.168.1.5", "::", "myhost.lan"):
            w = bind_warning(host, 8787)
            self.assertIn("SWITCH MODES", w)
            self.assertIn("anyone", w.lower())


if __name__ == "__main__":
    unittest.main()
