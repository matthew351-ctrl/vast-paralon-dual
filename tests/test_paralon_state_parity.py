"""v0.9.1 (Unreleased): the Paralon state on the web page says only what is
true now.

1. "this tool paused it and owes the un-pause" only while the node reads
   PAUSED (online, closed, not rented): an OFFLINE node is off for another
   reason (its agent stopped under a Vast renter, say), whatever the
   took-off flag says.
2. A machine in mode vast reads "Vast only, not on Paralon", neutral; amber
   only when the node reads online AND available (listed on both).
3. Each machine's Paralon state shows the age of its node reading: the
   client's last good GET /nodes (no extra call), amber past the slow read
   cadence, red past the point where the service treats Paralon as
   unreadable.

The page has no JS test runner, so the JS parts pin the page's own source
(as test_webui_parity does); the data behind them is tested for real."""

import json
import shutil
import tempfile
import unittest

from vpdual.config import Config
from vpdual.paralon import BACKOFF_MAX_S, ParalonError
from vpdual.status import read_age_limits, status_data
from vpdual.webui import PAGE

from status_scenario import build_scenario
from test_clients import Clock, FakeHTTP, node, paralon
from test_webui import Server
from test_webui_parity import js_function


class NodesAge(unittest.TestCase):
    """ParalonClient.nodes_age: the age of the last GOOD GET /nodes."""

    def test_none_before_the_first_read(self):
        self.assertIsNone(paralon(FakeHTTP([node()]), Clock()).nodes_age())

    def test_cached_reads_age_a_fresh_read_resets(self):
        http, clock = FakeHTTP([node()]), Clock()
        c = paralon(http, clock)
        clock.t = 10
        c.list_nodes()
        clock.t = 100
        c.list_nodes()                      # served from the cache
        self.assertEqual(len(http.requests), 1)
        self.assertEqual(c.nodes_age(), 90)
        c.list_nodes(refresh=True)
        self.assertEqual(c.nodes_age(), 0)

    def test_a_failed_read_keeps_the_last_good_age(self):
        http, clock = FakeHTTP([node()]), Clock()
        c = paralon(http, clock)
        c.list_nodes()
        clock.t = 200
        http.fail_with = 503
        with self.assertRaises(ParalonError):
            c.list_nodes()
        self.assertEqual(c.nodes_age(), 200)
        self.assertEqual(len(http.requests), 2)   # no extra call of its own


class StatusCarriesIt(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg, self.ctl, _vast, self.paralon, _docker = build_scenario(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_read_at_is_the_clients_last_good_read(self):
        self.paralon.nodes_age = lambda: 30.0      # the ctl clock reads 1000
        s = status_data(self.ctl, self.cfg)
        rows = [m["paralon"] for m in s["machines"] if m["paralon"].get("error") is None]
        self.assertEqual(len(rows), 3)
        self.assertEqual({r["read_at"] for r in rows}, {970})
        self.assertEqual(json.loads(json.dumps(s)), s)

    def test_no_age_kept_is_none(self):
        self.assertFalse(hasattr(self.paralon, "nodes_age"))
        rows = [m["paralon"] for m in status_data(self.ctl, self.cfg)["machines"]]
        self.assertEqual({r.get("read_at") for r in rows}, {None})

    def test_online_is_the_engines_own_test(self):
        self.paralon.nodes["node-2"]["online"] = False
        s = status_data(self.ctl, self.cfg)
        online = {m["paralon_node_id"]: m["paralon"].get("online") for m in s["machines"]}
        self.assertEqual(online, {"node-1": True, "node-2": False,
                                  "node-3": True, "node-4": None})

    def test_unreadable_paralon_has_no_reading(self):
        self.paralon.nodes_age = lambda: 30.0
        self.paralon.fail_read = True
        s = status_data(self.ctl, self.cfg)
        self.assertEqual({m["paralon"] for m in s["machines"]}, {None})


class Limits(unittest.TestCase):
    def test_own_cadence(self):
        cfg = Config(machines=[])
        slow, stale = read_age_limits(cfg)
        # GET /nodes is due every paralon_min_get_interval_s, made at the next
        # pass; red adds the client's longest back-off after errors
        self.assertEqual(slow, cfg.paralon_min_get_interval_s + cfg.poll_interval_s)
        self.assertEqual(stale, slow + BACKOFF_MAX_S)
        self.assertEqual((slow, stale), (180, 780))

    def test_follows_the_config(self):
        cfg = Config(machines=[], poll_interval_s=30, paralon_min_get_interval_s=300)
        self.assertEqual(read_age_limits(cfg), (330, 330 + BACKOFF_MAX_S))


class LimitsInTheStatus(Server):
    def test_api_status_sends_them(self):
        self.start()
        _resp, text = self.request("GET", "/api/status")
        s = json.loads(text)
        self.assertEqual((s["paralon_read_slow_s"], s["paralon_read_stale_s"]),
                         read_age_limits(self.cfg))
        self.assertIn("read_at", s["machines"][0]["paralon"])


class PausedByUsOnlyWhilePaused(unittest.TestCase):
    def test_paused_is_online_closed_not_rented(self):
        src = js_function("paralonPaused")
        for part in ("p.error === null", "!p.available", "paralonOnline(p)",
                     "!paralonRented(m)"):
            self.assertIn(part, src)
        self.assertIn('p.online !== false && p.state !== "offline"',
                      js_function("paralonOnline"))

    def test_the_flag_alone_is_never_said(self):
        self.assertIn('Boolean(m.took_off.paralon) && m.mode !== "vast" && '
                      'paralonPaused(m)', js_function("pausedByUs"))
        state = js_function("paralonState")
        self.assertNotIn("took_off", state)
        self.assertEqual(state.count("pausedByUs(m)"), 2)   # off: on Vast, paused
        self.assertIn("owes the un-pause", state)
        owed = js_function("owedText")
        self.assertIn('if (pausedByUs(m)) out.push("Paralon un-pause");', owed)
        self.assertNotIn("t.paralon", owed)

    def test_offline_is_the_engines_offline(self):
        state = js_function("paralonState")
        self.assertIn('if (!online) return ["offline", "st-warn", ', state)
        self.assertNotIn('p.state === "offline"', state)


class VastOnly(unittest.TestCase):
    def test_neutral_unless_listed_on_both(self):
        src = js_function("paralonState")
        self.assertIn('if (m.mode === "vast") return p.available && online ?', src)
        self.assertIn('["Vast only: listed on both", "st-warn", ', src)
        self.assertIn('["Vast only, not on Paralon", "st-mut", ', src)
        # a Paralon rental still shows first; then Vast only, before "available"
        order = [src.index(w) for w in ('["rented"', 'm.mode === "vast"',
                                        '["available"')]
        self.assertEqual(order, sorted(order))

    def test_mode_controls_kept_and_no_on_demand_warning(self):
        cols = js_function("tableCols")
        self.assertIn("modeButtons(m, s)", cols)
        self.assertIn('!m.paralon.spot &&\n          m.mode !== "vast")', cols)
        self.assertIn("modeButtons(m, s, true)", js_function("card"))


class ReadAge(unittest.TestCase):
    def test_age_from_the_status_never_a_call(self):
        src = js_function("readAgeEl")
        self.assertIn("s.generated_at - p.read_at", src)
        self.assertIn("Date.now() - lastAt", src)
        self.assertIn('"read " + ago(ms)', src)
        self.assertNotIn("fetch(", src)

    def test_colours(self):
        src = js_function("readAgeEl")
        self.assertIn("const slow = s.paralon_read_slow_s, stale = s.paralon_read_stale_s;",
                      src)
        self.assertIn('ms > stale * 1000 ? "st-bad" : ms > slow * 1000 ? "st-warn" :\n'
                      '    "st-mut"', src)

    def test_on_every_paralon_state(self):
        self.assertIn("readAgeEl(m, s)", js_function("paralonStateEl"))
        self.assertIn("paralonStateEl(m, s)", js_function("card"))
        self.assertIn("paralonStateEl(m, s)", js_function("tableCols"))
        self.assertIn("read ... ago", PAGE)       # the (i) text says what it is


if __name__ == "__main__":
    unittest.main()
