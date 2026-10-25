import json
import os
import unittest

from vpdual.engine import Controller
from vpdual.state import StateStore

from helpers import FakeParalon, FakeVast, TempDirMixin, make_config

OFF = ("available", "node-1", False)
ON = ("available", "node-1", True)
RECLAIM = ("reclaim", "node-1")


class Base(TempDirMixin, unittest.TestCase):
    def ctl(self, vast, paralon, policy="keep_vast_listed", mode="dual",
            dry_run=False, clock=None, machines=None, listing=None):
        cfg = make_config(self.state_path, policy, machines, listing)
        c = Controller(cfg, vast, paralon, self.store, dry_run=dry_run,
                       clock=clock or (lambda: 1000.0))
        if mode and not dry_run:
            c.set_mode(mode)
        elif mode:
            for m in cfg.machines:
                c.mstate(m)["mode"] = mode
        return c


class Rule1VastRenterWins(Base):
    def test_vast_renter_pauses_paralon(self):
        v, p = FakeVast(running=1), FakeParalon(available=True)
        self.ctl(v, p).poll()
        self.assertEqual(p.writes, [OFF])
        self.assertTrue(self.mstate()["took_off"]["paralon"])

    def test_vast_renter_reclaims_spot_rental(self):
        v, p = FakeVast(running=1), FakeParalon(available=True, rented=True)
        self.ctl(v, p).poll()
        self.assertIn(RECLAIM, p.writes)
        self.assertIn(OFF, p.writes)

    def test_reclaim_not_repeated_within_retry_window(self):
        v, p = FakeVast(running=1), FakeParalon(available=False, rented=True)
        now = [1000.0]
        c = self.ctl(v, p, clock=lambda: now[0])
        c.poll()
        now[0] += 60
        c.poll()
        self.assertEqual(p.writes.count(RECLAIM), 1)
        now[0] += 400
        c.poll()
        self.assertEqual(p.writes.count(RECLAIM), 2)

    def test_on_demand_rental_is_not_reclaimed_and_warns(self):
        v = FakeVast(running=1)
        p = FakeParalon(available=True, rented=True, spot=False)
        with self.assertLogs("vpdual", "WARNING") as logs:
            self.ctl(v, p).poll()
        self.assertNotIn(RECLAIM, p.writes)
        text = "\n".join(logs.output)
        self.assertIn("ON-DEMAND", text)
        self.assertIn("cannot reclaim", text)

    def test_on_demand_warning_in_dual_even_when_idle(self):
        with self.assertLogs("vpdual", "WARNING") as logs:
            self.ctl(FakeVast(), FakeParalon(spot=False)).poll()
        self.assertIn("ON-DEMAND, not spot", "\n".join(logs.output))


class Rule2ParalonRented(Base):
    def test_keep_vast_listed(self):
        v, p = FakeVast(listed=True), FakeParalon(rented=True)
        self.ctl(v, p, policy="keep_vast_listed").poll()
        self.assertEqual(v.writes, [])
        self.assertEqual(p.writes, [])

    def test_unlist_vast(self):
        v, p = FakeVast(listed=True), FakeParalon(rented=True)
        self.ctl(v, p, policy="unlist_vast").poll()
        self.assertEqual(v.writes, [("unlist", 111)])
        self.assertTrue(self.mstate()["took_off"]["vast"])


class Rule3RelistOnlyWhatWeTookOff(Base):
    def test_paralon_restored_after_vast_rental_ends(self):
        v, p = FakeVast(running=1), FakeParalon(available=True)
        c = self.ctl(v, p)
        c.poll()
        v.running = 0
        c.poll()
        self.assertEqual(p.writes, [OFF, ON])
        self.assertFalse(self.mstate()["took_off"]["paralon"])

    def test_side_the_host_took_off_is_left_alone(self):
        v = FakeVast(listed=False)            # host unlisted it by hand
        p = FakeParalon(available=False)      # host paused it by hand
        self.ctl(v, p).poll()
        self.assertEqual(v.writes, [])
        self.assertEqual(p.writes, [])

    def test_failed_relist_keeps_flag_and_retries(self):
        v, p = FakeVast(listed=True), FakeParalon(rented=True)
        c = self.ctl(v, p, policy="unlist_vast")
        c.poll()
        p.rented = False
        v.fail_write = True
        c.poll()
        self.assertTrue(self.mstate()["took_off"]["vast"])
        self.assertIsNotNone(self.mstate()["saved_listing"])
        v.fail_write = False
        c.poll()
        self.assertTrue(v.listed)
        self.assertFalse(self.mstate()["took_off"]["vast"])
        self.assertIsNone(self.mstate()["saved_listing"])


class RelistPrice(Base):
    """The relist uses the price the machine had; config is a fallback."""

    def unlist_then_relist(self, v, listing=None, clock=None):
        p = FakeParalon(rented=True)
        c = self.ctl(v, p, policy="unlist_vast", listing=listing, clock=clock)
        c.poll()                       # Paralon rented -> unlist Vast
        p.rented = False
        c.poll()                       # rental over -> relist
        return c

    def test_relists_at_the_saved_price_not_the_config(self):
        v = FakeVast(listed=True)
        self.unlist_then_relist(v)
        kind, mid, listing = v.writes[-1]
        self.assertEqual((kind, mid), ("list", 111))
        self.assertEqual(listing["price_gpu"], 0.45)       # saved, not 0.5
        self.assertEqual(listing["price_min_bid"], 0.35)   # saved, not 0.3

    def test_saved_before_the_unlist_write(self):
        v, p = FakeVast(listed=True), FakeParalon(rented=True)
        c = self.ctl(v, p, policy="unlist_vast")
        seen = {}

        def unlist(mid):
            seen["state"] = self.mstate()
            v.listed = False
        v.unlist_machine = unlist
        c.poll()
        self.assertTrue(seen["state"]["took_off"]["vast"])
        self.assertEqual(seen["state"]["saved_listing"]["price_gpu"], 0.45)

    def test_relist_never_omits_saved_disk_and_bandwidth(self):
        v = FakeVast(listed=True)
        self.unlist_then_relist(v, listing={})   # no config fallbacks at all
        listing = v.writes[-1][2]
        self.assertEqual(listing["price_disk"], 0.2)
        self.assertEqual(listing["price_inetu"], 4.0 / 1024)
        self.assertEqual(listing["price_inetd"], 3.0 / 1024)
        self.assertEqual(listing["end_date"], 5000.0)
        self.assertNotIn("duration", listing)   # one offer end, not two

    def test_expired_offer_end_falls_back_to_config_duration(self):
        v = FakeVast(listed=True)
        now = [1000.0]
        p = FakeParalon(rented=True)
        c = self.ctl(v, p, policy="unlist_vast", clock=lambda: now[0])
        c.poll()
        p.rented = False
        now[0] = 9000.0                          # saved end_date 5000 passed
        c.poll()
        listing = v.writes[-1][2]
        self.assertNotIn("end_date", listing)
        self.assertEqual(listing["duration"], "14 days")
        self.assertEqual(listing["price_disk"], 0.2)

    def test_fallback_config_price_when_nothing_was_saved(self):
        v = FakeVast(listed=True, terms={})      # Vast reported no terms
        self.unlist_then_relist(v)
        listing = v.writes[-1][2]
        self.assertEqual(listing["price_gpu"], 0.5)
        self.assertEqual(listing["price_min_bid"], 0.3)

    def test_mode_vast_on_hand_unlisted_machine_uses_fallback(self):
        v, p = FakeVast(listed=False, terms={}), FakeParalon(available=False)
        self.ctl(v, p, mode="vast").poll()
        self.assertEqual(v.writes[-1][2]["price_gpu"], 0.5)

    def test_no_saved_and_no_config_price_holds(self):
        v, p = FakeVast(listed=False, terms={}), FakeParalon(available=False)
        with self.assertLogs("vpdual", "WARNING") as logs:
            ok = self.ctl(v, p, mode="vast", listing={}).poll()
        self.assertFalse(ok)
        self.assertEqual(v.writes, [])
        self.assertIn("cannot relist Vast: no saved price",
                      "\n".join(logs.output))
        self.assertIn("hold", self.mstate()["decisions"][-1]["msg"])


class Rule4HoldOnUnreadableState(Base):
    def test_paralon_down_holds(self):
        v, p = FakeVast(running=1), FakeParalon()
        p.fail_read = True
        self.assertFalse(self.ctl(v, p).poll())
        self.assertEqual(v.writes + p.writes, [])
        self.assertIn("hold", self.mstate()["decisions"][-1]["msg"])

    def test_vast_cli_error_holds(self):
        v, p = FakeVast(), FakeParalon(rented=True)
        v.fail_read = True
        self.ctl(v, p, policy="unlist_vast").poll()
        self.assertEqual(v.writes + p.writes, [])

    def test_no_mode_does_nothing(self):
        v, p = FakeVast(running=1), FakeParalon()
        c = self.ctl(v, p, mode=None)
        self.assertFalse(c.poll())
        self.assertEqual(p.writes, [])

    def test_machine_missing_from_read_holds_alone(self):
        v = FakeVast(running=1).add(222, running=1)
        p = FakeParalon()                  # node-2 does not exist
        c = self.ctl(v, p, machines=[(111, "node-1"), (222, "node-2")])
        self.assertFalse(c.poll())
        self.assertEqual(p.writes, [OFF])  # 111 still handled
        self.assertIn("not found", self.mstate(222)["decisions"][-1]["msg"])


class MultiMachine(Base):
    def setup3(self):
        v = FakeVast(running=1).add(222, listed=True).add(333, listed=True)
        p = FakeParalon(available=True)
        p.add("node-2", rented=True).add("node-3", available=False)
        machines = [(111, "node-1"), (222, "node-2"), (333, "node-3")]
        return v, p, machines

    def test_one_pass_one_read_each_side(self):
        v, p, machines = self.setup3()
        c = self.ctl(v, p, policy="unlist_vast", machines=machines)
        v.reads = p.reads = 0
        c.poll()
        self.assertEqual((v.reads, p.reads), (1, 1))
        self.assertIn(OFF, p.writes)                    # 111: Vast renter
        self.assertIn(("unlist", 222), v.writes)        # 222: Paralon rented
        self.assertNotIn(("unlist", 333), v.writes)     # 333: idle
        self.assertEqual(p.writes, [OFF])               # node-3 left alone

    def test_state_is_per_machine(self):
        v, p, machines = self.setup3()
        self.ctl(v, p, policy="unlist_vast", machines=machines).poll()
        with open(self.state_path) as fh:
            saved = json.load(fh)["machines"]
        self.assertEqual(set(saved), {"111", "222", "333"})
        self.assertTrue(saved["111"]["took_off"]["paralon"])
        self.assertTrue(saved["222"]["took_off"]["vast"])
        self.assertEqual(saved["222"]["saved_listing"]["price_gpu"], 0.45)
        self.assertFalse(any(saved["333"]["took_off"].values()))

    def test_mode_for_one_machine(self):
        v, p, machines = self.setup3()
        c = self.ctl(v, p, machines=machines)          # all dual
        p.writes.clear()
        v.writes.clear()
        c.set_mode("paralon", 333)
        c.poll(333)
        self.assertEqual(v.writes, [("unlist", 333)])
        self.assertEqual(p.writes, [("available", "node-3", True)])
        modes = {mid: StateStore(self.state_path).load()["machines"][mid]
                 ["mode"] for mid in ("111", "222", "333")}
        self.assertEqual(modes, {"111": "dual", "222": "dual",
                                 "333": "paralon"})

    def test_unknown_machine_id_is_refused(self):
        v, p, machines = self.setup3()
        c = self.ctl(v, p, machines=machines)
        with self.assertRaisesRegex(ValueError, "not in the config"):
            c.set_mode("vast", 999)

    def test_legacy_single_machine_state_is_migrated(self):
        with open(self.state_path, "w") as fh:
            json.dump({"mode": "dual", "took_off": {"vast": False,
                       "paralon": True}, "reclaim_sent_at": None,
                       "decisions": []}, fh)
        v, p = FakeVast(), FakeParalon(available=False)
        self.ctl(v, p, mode=None).poll()
        self.assertEqual(p.writes, [ON])      # the owed relist is honoured


class Modes(Base):
    def test_mode_vast(self):
        v, p = FakeVast(listed=False), FakeParalon(available=True)
        self.ctl(v, p, mode="vast").poll()
        self.assertEqual(p.writes, [OFF])
        self.assertEqual(v.writes[0][0], "list")

    def test_mode_paralon(self):
        v, p = FakeVast(listed=True), FakeParalon(available=False)
        self.ctl(v, p, mode="paralon").poll()
        self.assertEqual(v.writes, [("unlist", 111)])
        self.assertEqual(p.writes, [ON])

    def test_mode_paralon_waits_for_running_vast_renter(self):
        v, p = FakeVast(listed=True, running=1), FakeParalon(available=False)
        c = self.ctl(v, p, mode="paralon")
        c.poll()
        self.assertEqual(v.writes, [("unlist", 111)])
        self.assertEqual(p.writes, [])     # GPU still busy with Vast renter
        v.running = 0
        c.poll()
        self.assertEqual(p.writes, [ON])

    def test_switch_back_to_dual_restores_both(self):
        v, p = FakeVast(listed=True), FakeParalon(available=True)
        c = self.ctl(v, p, mode="vast")
        c.poll()
        c.set_mode("paralon")
        c.poll()
        c.set_mode("dual")
        c.poll()
        self.assertTrue(v.listed)
        self.assertTrue(p.available)
        self.assertEqual(self.mstate()["took_off"],
                         {"vast": False, "paralon": False})
        self.assertEqual(v.writes[-1][2]["price_gpu"], 0.45)  # its own price

    def test_mode_is_persisted(self):
        self.ctl(FakeVast(), FakeParalon(), mode="paralon")
        self.assertEqual(self.mstate()["mode"], "paralon")


class DryRun(Base):
    def test_dry_run_writes_nothing(self):
        for mode in ("vast", "paralon", "dual"):
            v = FakeVast(listed=True, running=1)
            p = FakeParalon(available=True, rented=True)
            c = self.ctl(v, p, mode=None, dry_run=True, policy="unlist_vast")
            c.set_mode(mode)
            c.poll()
            self.assertEqual(v.writes + p.writes, [], mode)
            self.assertFalse(os.path.exists(self.state_path), mode)

    def test_dry_run_logs_intent(self):
        v, p = FakeVast(running=1), FakeParalon(available=True)
        c = self.ctl(v, p, mode="dual", dry_run=True)
        with self.assertLogs("vpdual", "INFO") as logs:
            c.poll()
        self.assertIn("DRY-RUN would: take paralon off", "\n".join(logs.output))


class Restart(Base):
    def test_flag_survives_restart(self):
        v, p = FakeVast(running=1), FakeParalon(available=True)
        self.ctl(v, p).poll()                       # takes Paralon off
        v.running = 0
        cfg = make_config(self.state_path)
        fresh = Controller(cfg, v, p, StateStore(self.state_path),
                           clock=lambda: 2000.0)     # "after a restart"
        fresh.poll()
        self.assertEqual(p.writes[-1], ON)

    def test_saved_price_survives_restart(self):
        v, p = FakeVast(listed=True), FakeParalon(rented=True)
        self.ctl(v, p, policy="unlist_vast").poll()
        p.rented = False
        cfg = make_config(self.state_path, "unlist_vast")
        Controller(cfg, v, p, StateStore(self.state_path),
                   clock=lambda: 2000.0).poll()
        self.assertEqual(v.writes[-1][2]["price_gpu"], 0.45)

    def test_flag_saved_before_the_write(self):
        v, p = FakeVast(running=1), FakeParalon(available=True)
        c = self.ctl(v, p)
        seen = {}

        def set_available(node_id, available):
            seen["flag_on_disk"] = self.mstate()["took_off"]["paralon"]
            p.available = available
        p.set_available = set_available
        c.poll()
        self.assertTrue(seen["flag_on_disk"])


if __name__ == "__main__":
    unittest.main()
