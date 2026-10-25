"""Fixes from the 2026-10-26 review: the service re-reads the state file
each pass and locks it (a `mode` change from another process was being
overwritten); a rolling offer relists as rolling; min_chunk is saved; prices
go out with 10 significant digits; a config cannot set both offer ends."""
import os
import unittest

from vpdual.config import ConfigError, parse_config
from vpdual.engine import Controller
from vpdual.state import StateStore
from vpdual.vast import VastCLI, VastMachine

from helpers import FakeParalon, FakeVast, TempDirMixin, make_config


class ModeFromAnotherProcess(TempDirMixin, unittest.TestCase):
    def _ctl(self, vast, paralon):
        cfg = make_config(self.state_path, "unlist_vast")
        return Controller(cfg, vast, paralon, StateStore(self.state_path),
                          clock=lambda: 1000.0)

    def test_service_picks_up_a_mode_set_by_another_controller(self):
        v, p = FakeVast(), FakeParalon(available=True)
        service = self._ctl(v, p)
        service.set_mode("dual")
        service.poll()
        other = self._ctl(v, p)                 # the `mode` command
        other.set_mode("vast")
        service.poll()                          # must see "vast", not overwrite it
        self.assertEqual(service.mstate(service.cfg.machines[0])["mode"], "vast")
        self.assertIn(("available", "node-1", False), p.writes)
        on_disk = StateStore(self.state_path).load()["machines"]["111"]
        self.assertEqual(on_disk["mode"], "vast")

    def test_set_mode_keeps_flags_written_by_the_service(self):
        v, p = FakeVast(running=1), FakeParalon(available=True)
        service = self._ctl(v, p)
        service.set_mode("dual")
        service.poll()                          # pauses Paralon, flag on disk
        other = self._ctl(v, p)
        other.set_mode("dual")                  # loaded before? no: reloads first
        on_disk = StateStore(self.state_path).load()["machines"]["111"]
        self.assertTrue(on_disk["took_off"]["paralon"])

    def test_lock_file_sits_next_to_the_state(self):
        v, p = FakeVast(), FakeParalon()
        c = self._ctl(v, p)
        c.set_mode("dual")
        self.assertTrue(os.path.exists(self.state_path + ".lock"))


class RollingOfferStaysRolling(TempDirMixin, unittest.TestCase):
    def test_duration_is_saved_and_relisted_without_end_date(self):
        terms = {"listed_gpu_cost": 0.45, "min_bid_price": 0.35,
                 "listed_storage_cost": 0.2, "end_date": 5000.0,
                 "duration": 1209600.0, "listed_min_gpu_count": 2}
        v, p = FakeVast(terms=terms), FakeParalon(available=True, rented=True)
        cfg = make_config(self.state_path, "unlist_vast", listing={})
        c = Controller(cfg, v, p, StateStore(self.state_path),
                       clock=lambda: 1000.0)
        c.set_mode("dual")
        c.poll()                                # Paralon rented -> unlist Vast
        saved = c.mstate(cfg.machines[0])["saved_listing"]
        self.assertEqual(saved["duration"], 1209600.0)
        self.assertEqual(saved["min_chunk"], 2)
        p.rented = False
        c.poll()                                # rental over -> relist
        listing = v.writes[-1][2]
        self.assertEqual(listing["duration"], 1209600)
        self.assertNotIn("end_date", listing)
        self.assertEqual(listing["min_chunk"], 2)

    def test_fixed_end_date_still_used_when_not_rolling(self):
        v, p = FakeVast(), FakeParalon(available=True, rented=True)
        cfg = make_config(self.state_path, "unlist_vast", listing={})
        c = Controller(cfg, v, p, StateStore(self.state_path),
                       clock=lambda: 1000.0)
        c.set_mode("dual")
        c.poll()
        p.rented = False
        c.poll()
        listing = v.writes[-1][2]
        self.assertEqual(listing["end_date"], 5000.0)
        self.assertNotIn("duration", listing)


class CommandLineValues(unittest.TestCase):
    def test_numbers_are_clean_and_ints_are_whole(self):
        args = VastCLI.list_args(1, {
            "price_gpu": 0.43000000000000005, "price_min_bid": 0.38,
            "price_disk": 0.09999999999999998, "price_inetu": 4.0 / 1024,
            "min_chunk": 2, "duration": 1209600.0})
        got = dict(zip(args[3::2], args[4::2]))
        self.assertEqual(got, {"--price_gpu": "0.43", "--price_min_bid": "0.38",
                               "--price_disk": "0.1",
                               "--price_inetu": "0.00390625",
                               "--min_chunk": "2", "--duration": "1209600"})

    def test_config_duration_string_passes_through(self):
        args = VastCLI.list_args(1, {"price_gpu": 0.5, "price_min_bid": 0.3,
                                     "duration": "14 days"})
        self.assertEqual(args[-2:], ["--duration", "14 days"])

    def test_zero_duration_is_not_a_rolling_offer(self):
        m = VastMachine.from_raw({"id": 1, "listed": True, "duration": 0,
                                  "listed_gpu_cost": 0.4, "min_bid_price": 0.3})
        self.assertNotIn("duration", m.listing_terms)


class ConfigOfferEnd(unittest.TestCase):
    def test_both_offer_ends_refused(self):
        raw = {"on_paralon_rental": "unlist_vast",
               "machines": [{"vast_machine_id": 1, "paralon_node_id": "n"}],
               "vast_listing": {"price_gpu": 0.4, "price_min_bid": 0.3,
                                "end_date": "12/31/2026", "duration": "14 days"}}
        with self.assertRaises(ConfigError):
            parse_config(raw, {"PARALON_API_KEY": "prlc_test"})


if __name__ == "__main__":
    unittest.main()
