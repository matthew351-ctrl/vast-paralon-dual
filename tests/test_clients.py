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

from helpers import TempDirMixin, make_config

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

    def test_requires_relist_price(self):
        data = self.good()
        del data["vast_listing"]["price_min_bid"]
        with self.assertRaisesRegex(ConfigError, "price_min_bid"):
            load_config(self.write(data), env={"PARALON_API_KEY": KEY})

    def test_requires_key(self):
        with self.assertRaisesRegex(ConfigError, "PARALON_API_KEY"):
            load_config(self.write(self.good()), env={})

    def test_loads_and_example_is_valid(self):
        cfg = load_config(self.write(self.good()), env={"PARALON_API_KEY": KEY})
        self.assertEqual(cfg.on_paralon_rental, "unlist_vast")
        example = os.path.join(os.path.dirname(__file__), "..",
                               "config.example.json")
        with self.assertRaisesRegex(ConfigError, "on_paralon_rental"):
            load_config(example, env={"PARALON_API_KEY": KEY})


if __name__ == "__main__":
    unittest.main()
