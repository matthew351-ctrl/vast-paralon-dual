"""Second review batch (2026-10-26): a refused `vastai list/unlist` (exit 0,
success:false) is an error; a flag clears only once the next read confirms
the side is back; an owed Vast relist runs even when the Paralon node is gone;
an offline node is not made available; empty rental records; config scalar
types; no surprise volume offer; the docker events stream is reopened."""
import io
import json
import subprocess
import threading
import unittest

from vpdual import docker_events
from vpdual.config import ConfigError, parse_config
from vpdual.engine import Controller
from vpdual.paralon import ParalonNode
from vpdual.state import StateStore
from vpdual.vast import VastCLI, VastError, VastMachine

from helpers import FakeParalon, FakeVast, TempDirMixin, make_config


class JsonRunner:
    """vastai with --raw prints the server's JSON reply; a refusal exits 0."""

    def __init__(self, reply):
        self.reply, self.calls = reply, []

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        out = json.dumps(self.reply) if cmd[1] != "show" else "{}"
        return subprocess.CompletedProcess(cmd, 0, out, "")


class VastRefusalIsNotSuccess(unittest.TestCase):
    def test_success_false_raises_even_with_exit_0(self):
        cli = VastCLI(runner=JsonRunner({"success": False, "msg": "no such machine"}))
        with self.assertRaises(VastError):
            cli.unlist_machine(1)
        with self.assertRaises(VastError):
            cli.list_machine(1, {"price_gpu": 0.4, "price_min_bid": 0.3})

    def test_success_true_passes_and_raw_is_requested(self):
        r = JsonRunner({"success": True, "msg": "ok"})
        VastCLI(runner=r).unlist_machine(1)
        self.assertEqual(r.calls[0][-1], "--raw")

    def test_plain_text_refusal_is_caught(self):
        with self.assertRaises(VastError):
            VastCLI._check_reply("Machine 999 not found")


class FlagsClearOnlyWhenConfirmed(TempDirMixin, unittest.TestCase):
    def test_a_relist_vast_silently_ignores_stays_owed(self):
        """The write 'succeeds' but the next read still shows unlisted: the
        tool keeps the flag and tries again instead of forgetting."""
        v, p = FakeVast(listed=True), FakeParalon(rented=True)
        real_list = v.list_machine
        v.list_machine = lambda mid, listing: v.writes.append(("list", mid, listing))
        cfg = make_config(self.state_path, "unlist_vast", listing={})
        c = Controller(cfg, v, p, StateStore(self.state_path), clock=lambda: 1000.0)
        c.set_mode("dual")
        c.poll()                        # unlist
        p.rented = False
        c.poll()                        # relist attempt, ignored by "Vast"
        self.assertTrue(self.mstate()["took_off"]["vast"])
        c.poll()                        # tries again
        self.assertEqual(sum(1 for w in v.writes if w[0] == "list"), 2)
        v.list_machine = real_list
        c.poll()                        # a real relist
        c.poll()                        # the confirming read
        self.assertFalse(self.mstate()["took_off"]["vast"])


class MissingNodeStillRelistsVast(TempDirMixin, unittest.TestCase):
    def test_owed_vast_relist_runs_when_the_node_is_gone(self):
        v, p = FakeVast(listed=True), FakeParalon(rented=True)
        cfg = make_config(self.state_path, "unlist_vast", listing={})
        c = Controller(cfg, v, p, StateStore(self.state_path), clock=lambda: 1000.0)
        c.set_mode("dual")
        c.poll()                        # unlisted for the Paralon rental
        self.assertFalse(v.listed)
        p.nodes.clear()                 # node deleted on Paralon
        c.poll()
        self.assertTrue(v.listed)       # Vast given back, not stranded


class OfflineNodeIsNotMadeAvailable(TempDirMixin, unittest.TestCase):
    def test_force_up_skips_an_offline_node(self):
        v, p = FakeVast(), FakeParalon(available=False)
        orig = p.list_nodes

        def offline(refresh=False):
            nodes = orig()
            for n in nodes:
                n.raw["online"] = False
            return nodes
        p.list_nodes = offline
        cfg = make_config(self.state_path, "unlist_vast")
        c = Controller(cfg, v, p, StateStore(self.state_path), clock=lambda: 1000.0)
        c.set_mode("paralon")
        c.poll()
        self.assertEqual(p.writes, [])


class RentalRecords(unittest.TestCase):
    def _node(self, state, rental):
        return ParalonNode(id="n", state=state, available=True, spot=True,
                           price=None, rental=rental)

    def test_empty_record_is_not_rented(self):
        self.assertFalse(self._node("available", {}).rented)
        self.assertFalse(self._node("available", None).rented)

    def test_running_and_unknown_status_are_rented(self):
        self.assertTrue(self._node("available", {"status": "running"}).rented)
        self.assertTrue(self._node("available", {"id": "x"}).rented)
        self.assertFalse(self._node("available", {"status": "ended"}).rented)
        self.assertTrue(self._node("rented", None).rented)


class ConfigTypes(unittest.TestCase):
    def _raw(self, **extra):
        raw = {"on_paralon_rental": "unlist_vast",
               "machines": [{"vast_machine_id": 1, "paralon_node_id": "n"}]}
        raw.update(extra)
        return raw

    def test_scalars_must_have_the_right_type(self):
        env = {"PARALON_API_KEY": "prlc_test"}
        for bad in ({"poll_interval_s": "60"}, {"paralon_max_calls_per_min": 0},
                    {"docker_events": "false"}, {"reclaim_retry_s": True},
                    {"vastai_bin": ""},
                    {"vast_listing": {"price_gpu": True, "price_min_bid": 0.3}}):
            with self.assertRaises(ConfigError, msg=str(bad)):
                parse_config(self._raw(**bad), env)
        parse_config(self._raw(poll_interval_s=30, docker_events=True), env)


class NoVolumeOffer(unittest.TestCase):
    def test_vol_size_0_when_the_machine_has_no_volume_offer(self):
        m = VastMachine.from_raw({"id": 1, "listed": True, "listed_gpu_cost": 0.4,
                                  "min_bid_price": 0.3, "listed_volume_cost": None,
                                  "volume_total_size": 0.0})
        self.assertEqual(m.listing_terms["vol_size"], 0)
        self.assertNotIn("vol_price", m.listing_terms)

    def test_volume_offer_is_saved(self):
        m = VastMachine.from_raw({"id": 1, "listed": True, "listed_gpu_cost": 0.4,
                                  "min_bid_price": 0.3, "listed_volume_cost": 0.05,
                                  "volume_total_size": 500.0})
        self.assertEqual(m.listing_terms["vol_size"], 500)
        self.assertEqual(m.listing_terms["vol_price"], 0.05)


class DockerEventsReopen(unittest.TestCase):
    def test_stream_is_reopened_after_it_ends(self):
        opened, slept = [], []

        class Proc:
            def __init__(self):
                self.stdout = io.StringIO("abc\n")

        def popen(cmd, **kw):
            opened.append(cmd)
            return Proc()
        wake = threading.Event()
        t = docker_events.start_watcher(wake, popen=popen, sleep=slept.append,
                                        retries=2)
        t.join(5)
        self.assertEqual(len(opened), 3)
        self.assertEqual(slept, [30, 30, 30])
        self.assertTrue(wake.is_set())


if __name__ == "__main__":
    unittest.main()
