"""The real clients, with HTTP and the vastai subprocess mocked."""

import io
import json
import logging
import os
import subprocess
import unittest
import urllib.error

from vpdual.config import ConfigError, load_config
from vpdual.engine import Controller
from vpdual.paralon import ParalonClient, ParalonError
from vpdual.vast import VastCLI, VastError

from helpers import TempDirMixin, fixture, make_config

KEY = "prlc_super_secret_key"


class FakeHTTP:
    """Stands in for urllib.request.urlopen."""

    def __init__(self, nodes):
        self.nodes = nodes
        self.requests = []
        self.fail_with = None

    def __call__(self, req, timeout=None):
        body = json.loads(req.data) if req.data else None
        self.requests.append((req.get_method(), req.full_url, body,
                              req.get_header("Authorization")))
        if self.fail_with:
            code = self.fail_with
            raise urllib.error.HTTPError(req.full_url, code, "err", {}, None)
        payload = json.dumps(self.nodes if req.get_method() == "GET" else {})
        return io.BytesIO(payload.encode())


def node(**kw):
    base = {"id": "node-1", "state": "available", "available": True,
            "spot": True, "price": 0.9, "rental": None}
    base.update(kw)
    return base


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def paralon(http, clock):
    return ParalonClient("https://paraloncloud.com/api/v1", KEY, opener=http,
                         clock=clock, sleep=clock.sleep)


class ParalonClientTests(unittest.TestCase):
    def test_bearer_key_and_endpoints(self):
        http, clock = FakeHTTP([node()]), Clock()
        c = paralon(http, clock)
        self.assertTrue(c.get_node("node-1").available)
        c.set_available("node-1", False)
        c.reclaim("node-1")
        self.assertEqual([(m, u.rsplit("v1", 1)[1], b) for m, u, b, _ in http.requests], [
            ("GET", "/nodes", None),
            ("PATCH", "/nodes/node-1", {"available": False}),
            ("POST", "/nodes/node-1/reclaim", None)])
        self.assertEqual(http.requests[0][3], "Bearer " + KEY)

    def test_get_nodes_cached_for_a_minute(self):
        http, clock = FakeHTTP([node()]), Clock()
        c = paralon(http, clock)
        c.get_node("node-1")
        clock.t = 30
        c.get_node("node-1")
        self.assertEqual(len(http.requests), 1)
        clock.t = 61
        c.get_node("node-1")
        self.assertEqual(len(http.requests), 2)

    def test_patch_updates_cache(self):
        http, clock = FakeHTTP([node()]), Clock()
        c = paralon(http, clock)
        c.get_node("node-1")
        c.set_available("node-1", False)
        self.assertFalse(c.get_node("node-1").available)

    def test_never_more_than_four_calls_a_minute(self):
        http, clock = FakeHTTP([node()]), Clock()
        times = []

        def opener(req, timeout=None):
            times.append(clock.t)
            return http(req, timeout)
        c = ParalonClient("https://x/api/v1", KEY, opener=opener, clock=clock,
                          sleep=clock.sleep)
        for _ in range(40):
            try:
                c.set_available("node-1", True)
            except ParalonError:
                pass
            clock.t += 7
        self.assertGreater(len(times), 8)
        for i in range(len(times) - 4):
            self.assertGreaterEqual(times[i + 4] - times[i], 60)

    def test_refuses_rather_than_waiting_long(self):
        http, clock = FakeHTTP([node()]), Clock()
        c = paralon(http, clock)
        for _ in range(4):
            c.set_available("node-1", True)
        clock.t = 5   # the next free slot is 55 s away
        with self.assertRaisesRegex(ParalonError, "rate limit"):
            c.set_available("node-1", True)

    def test_backoff_on_429_409_5xx(self):
        for code in (429, 409, 503):
            http, clock = FakeHTTP([node()]), Clock()
            c = paralon(http, clock)
            http.fail_with = code
            with self.assertRaises(ParalonError):
                c.list_nodes()
            http.fail_with = None
            with self.assertRaisesRegex(ParalonError, "backing off"):
                c.list_nodes()
            self.assertEqual(len(http.requests), 1, code)
            clock.t += 31
            c.list_nodes()
            self.assertEqual(len(http.requests), 2, code)

    def test_key_never_in_errors_or_logs(self):
        http, clock = FakeHTTP([node()]), Clock()
        c = paralon(http, clock)
        http.fail_with = 401
        with self.assertRaises(ParalonError) as cm:
            c.list_nodes()
        self.assertNotIn(KEY, str(cm.exception))
        self.assertIn("My nodes", str(cm.exception))
        self.assertNotIn(KEY, repr(c))
        self.assertNotIn(KEY, repr(make_config("/x")))


class ParalonFixtureTests(unittest.TestCase):
    """The real GET /nodes shape (tests/fixture_paralon_nodes.json)."""

    def test_parses_real_nodes_payload(self):
        http, clock = FakeHTTP(fixture("fixture_paralon_nodes.json")), Clock()
        nodes = paralon(http, clock).list_nodes()
        self.assertEqual(len(nodes), 1)
        n = nodes[0]
        self.assertEqual(n.id, "00000000-0000-0000-0000-000000000001")
        self.assertEqual(n.state, "rented")
        self.assertTrue(n.available)
        self.assertTrue(n.spot)
        self.assertTrue(n.rented)
        self.assertEqual(n.price_text, "0.477 USD/hour")
        self.assertIn("RTX 4090", n.label)

    def test_ended_rental_record_is_not_rented(self):
        data = fixture("fixture_paralon_nodes.json")
        item = data["nodes"][0]
        item["state"] = "available"
        item["rental"]["status"] = "ended"
        http, clock = FakeHTTP(data), Clock()
        self.assertFalse(paralon(http, clock).list_nodes()[0].rented)


class FakeRunner:
    def __init__(self, machines):
        self.machines = machines
        self.calls = []
        self.returncode = 0

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        out = json.dumps(self.machines) if cmd[1] == "show" else "ok"
        return subprocess.CompletedProcess(cmd, self.returncode, out, "boom")


def machine(**kw):
    base = {"id": 111, "listed": True, "current_rentals_running": 0,
            "current_rentals_resident": 2}
    base.update(kw)
    return base


class VastCLITests(unittest.TestCase):
    def test_reads_machine_from_list_or_wrapped(self):
        for payload in ([machine()], {"machines": [machine()]}):
            r = FakeRunner(payload)
            m = VastCLI(runner=r).read_machine(111)
            self.assertEqual((m.listed, m.running, m.resident), (True, 0, 2))
            self.assertEqual(r.calls[0], ["vastai", "show", "machines", "--raw"])

    def test_parses_real_show_machines_payload(self):
        r = FakeRunner(fixture("fixture_vast_show_machines.json"))
        machines = VastCLI(runner=r).read_machines()
        self.assertEqual(r.calls, [["vastai", "show", "machines", "--raw"]])
        m = machines[12345]
        self.assertEqual((m.listed, m.running, m.resident), (True, 1, 2))
        self.assertEqual(m.listing_terms, {
            "price_gpu": 0.43, "price_min_bid": 0.38, "price_disk": 0.333,
            "price_inetu": 4.0 / 1024, "price_inetd": 3.0 / 1024,
            "end_date": 1798761600.0})

    def test_relist_args_from_saved_terms(self):
        r = FakeRunner(fixture("fixture_vast_show_machines.json"))
        cli = VastCLI(runner=r)
        terms = cli.read_machines()[12345].listing_terms
        self.assertEqual(cli.list_args(12345, terms), [
            "list", "machine", "12345", "--price_gpu", "0.43",
            "--price_min_bid", "0.38", "--price_disk", "0.333",
            "--price_inetu", "0.00390625", "--price_inetd", "0.0029296875",
            "--end_date", "1798761600"])

    def test_one_bad_record_only_fails_that_machine(self):
        r = FakeRunner({"machines": [machine(), {"id": 222}]})
        machines = VastCLI(runner=r).read_machines()
        self.assertTrue(machines[111].listed)
        self.assertIsInstance(machines[222], VastError)

    def test_list_uses_configured_prices(self):
        r = FakeRunner([])
        VastCLI(runner=r).list_machine(
            111, {"price_gpu": 0.5, "price_min_bid": 0.3, "duration": "14 days"})
        self.assertEqual(r.calls[0], [
            "vastai", "list", "machine", "111", "--price_gpu", "0.5",
            "--price_min_bid", "0.3", "--duration", "14 days"])

    def test_unlist(self):
        r = FakeRunner([])
        VastCLI(runner=r).unlist_machine(111)
        self.assertEqual(r.calls[0], ["vastai", "unlist", "machine", "111"])

    def test_errors(self):
        r = FakeRunner([machine()])
        r.returncode = 1
        with self.assertRaises(VastError):
            VastCLI(runner=r).read_machine(111)
        with self.assertRaises(VastError):
            VastCLI(runner=FakeRunner([machine(id=5)])).read_machine(111)
        bad = FakeRunner([])
        bad.machines = "not json"

        def runner(cmd, **kw):
            return subprocess.CompletedProcess(cmd, 0, "<html>", "")
        with self.assertRaises(VastError):
            VastCLI(runner=runner).read_machine(111)


class EndToEnd(TempDirMixin, unittest.TestCase):
    def build(self, nodes, machines, dry_run):
        http, clock, runner = FakeHTTP(nodes), Clock(), FakeRunner(machines)
        cfg = make_config(self.state_path)
        ctl = Controller(cfg, VastCLI(runner=runner), paralon(http, clock),
                         self.store, dry_run=dry_run)
        return ctl, http, runner

    def test_dry_run_sends_no_write(self):
        ctl, http, runner = self.build(
            [node(state="rented", rental={"id": "r"})],
            [machine(current_rentals_running=1)], dry_run=True)
        ctl.set_mode("dual")
        with self.assertLogs("vpdual", logging.INFO):
            ctl.poll()
        self.assertEqual([r[0] for r in http.requests], ["GET"])
        self.assertEqual([c[1] for c in runner.calls], ["show"])
        self.assertFalse(os.path.exists(self.state_path))

    def test_real_fixtures_unlist_save_and_relist(self):
        """Both real payload shapes through the engine: the Vast terms are
        saved on unlist and passed back in full on relist."""
        vast_data = fixture("fixture_vast_show_machines.json")
        vast_data["machines"][0]["current_rentals_running"] = 0
        nodes = fixture("fixture_paralon_nodes.json")
        node_id = nodes["nodes"][0]["id"]
        http, clock, runner = FakeHTTP(nodes), Clock(), FakeRunner(vast_data)
        cfg = make_config(self.state_path, "unlist_vast",
                          machines=[(12345, node_id)], listing={})
        ctl = Controller(cfg, VastCLI(runner=runner), paralon(http, clock),
                         self.store, clock=lambda: 1793000000.0)
        ctl.set_mode("dual")
        ctl.poll()
        self.assertEqual(runner.calls[-1], ["vastai", "unlist", "machine",
                                            "12345"])
        saved = self.mstate(12345)["saved_listing"]
        self.assertEqual(saved["price_disk"], 0.333)
        # Paralon rental ends; Vast reports the machine unlisted (no terms).
        vast_data["machines"][0].update(listed=False, listed_gpu_cost=None)
        nodes["nodes"][0].update(state="available", rental=None)
        clock.t += 61
        ctl.poll()
        self.assertEqual(runner.calls[-1][:3], ["vastai", "list", "machine"])
        for flag, val in (("--price_gpu", "0.43"), ("--price_min_bid", "0.38"),
                          ("--price_disk", "0.333"),
                          ("--price_inetu", "0.00390625"),
                          ("--price_inetd", "0.0029296875"),
                          ("--end_date", "1798761600")):
            i = runner.calls[-1].index(flag)
            self.assertEqual(runner.calls[-1][i + 1], val)
        self.assertIsNone(self.mstate(12345)["saved_listing"])

    def test_live_rule1_reclaims_and_pauses(self):
        ctl, http, runner = self.build(
            [node(state="rented", rental={"id": "r"})],
            [machine(current_rentals_running=1)], dry_run=False)
        ctl.set_mode("dual")
        ctl.poll()
        self.assertEqual([r[0] for r in http.requests], ["GET", "POST", "PATCH"])


class ConfigTests(TempDirMixin, unittest.TestCase):
    def write(self, data):
        path = os.path.join(self.tmp, "c.json")
        with open(path, "w") as fh:
            json.dump(data, fh)
        return path

    def good(self):
        return {"vast_machine_id": 1, "paralon_node_id": "n",
                "on_paralon_rental": "unlist_vast",
                "vast_listing": {"price_gpu": 0.5, "price_min_bid": 0.3}}

    def test_refuses_without_policy(self):
        data = self.good()
        del data["on_paralon_rental"]
        with self.assertRaisesRegex(ConfigError, "on_paralon_rental"):
            load_config(self.write(data), env={"PARALON_API_KEY": KEY})

    def test_refuses_bad_policy(self):
        data = self.good()
        data["on_paralon_rental"] = "whatever"
        with self.assertRaises(ConfigError):
            load_config(self.write(data), env={"PARALON_API_KEY": KEY})

    def test_relist_prices_are_optional(self):
        data = self.good()
        del data["vast_listing"]
        cfg = load_config(self.write(data), env={"PARALON_API_KEY": KEY})
        self.assertEqual(cfg.machines[0].vast_listing, {})

    def test_old_single_machine_keys_still_load(self):
        cfg = load_config(self.write(self.good()), env={"PARALON_API_KEY": KEY})
        self.assertEqual([(m.vast_machine_id, m.paralon_node_id)
                          for m in cfg.machines], [(1, "n")])

    def test_machines_list_with_per_machine_override(self):
        data = {"on_paralon_rental": "keep_vast_listed",
                "vast_listing": {"price_gpu": 0.5, "price_min_bid": 0.3},
                "machines": [
                    {"vast_machine_id": 1, "paralon_node_id": "a"},
                    {"vast_machine_id": "2", "paralon_node_id": "b",
                     "vast_listing": {"price_gpu": 0.9}}]}
        cfg = load_config(self.write(data), env={"PARALON_API_KEY": KEY})
        self.assertEqual(cfg.machines[0].vast_listing["price_gpu"], 0.5)
        self.assertEqual(cfg.machines[1].vast_machine_id, 2)
        self.assertEqual(cfg.machines[1].vast_listing,
                         {"price_gpu": 0.9, "price_min_bid": 0.3})

    def test_refuses_duplicates_and_empty_list(self):
        base = {"on_paralon_rental": "keep_vast_listed"}
        for machines in ([], [{"vast_machine_id": 1, "paralon_node_id": "a"},
                              {"vast_machine_id": 1, "paralon_node_id": "b"}]):
            with self.assertRaises(ConfigError):
                load_config(self.write(dict(base, machines=machines)),
                            env={"PARALON_API_KEY": KEY})

    def test_requires_key(self):
        with self.assertRaisesRegex(ConfigError, "PARALON_API_KEY"):
            load_config(self.write(self.good()), env={})

    def test_key_read_from_env_file_next_to_config(self):
        path = self.write(self.good())
        with open(os.path.join(self.tmp, "env"), "w") as fh:
            fh.write(f"PARALON_API_KEY={KEY}\n")
        self.assertEqual(load_config(path, env={}).paralon_api_key, KEY)

    def test_loads_and_example_is_valid(self):
        cfg = load_config(self.write(self.good()), env={"PARALON_API_KEY": KEY})
        self.assertEqual(cfg.on_paralon_rental, "unlist_vast")
        example = os.path.join(os.path.dirname(__file__), "..",
                               "config.example.json")
        with self.assertRaisesRegex(ConfigError, "on_paralon_rental"):
            load_config(example, env={"PARALON_API_KEY": KEY})
        with open(example) as fh:
            data = json.load(fh)
        data["on_paralon_rental"] = "keep_vast_listed"
        cfg = load_config(self.write(data), env={"PARALON_API_KEY": KEY})
        self.assertEqual(len(cfg.machines), 2)


if __name__ == "__main__":
    unittest.main()
