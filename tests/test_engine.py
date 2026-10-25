import os
import unittest

from vpdual.engine import Controller
from vpdual.state import StateStore

from helpers import FakeParalon, FakeVast, TempDirMixin, make_config


class Base(TempDirMixin, unittest.TestCase):
    def ctl(self, vast, paralon, policy="keep_vast_listed", mode="dual",
            dry_run=False, clock=None):
        cfg = make_config(self.state_path, policy)
        c = Controller(cfg, vast, paralon, self.store, dry_run=dry_run,
                       clock=clock or (lambda: 1000.0))
        if mode and not dry_run:
            c.set_mode(mode)
        elif mode:
            c.state["mode"] = mode
        return c


class Rule1VastRenterWins(Base):
    def test_vast_renter_pauses_paralon(self):
        v, p = FakeVast(running=1), FakeParalon(available=True)
        self.ctl(v, p).poll()
        self.assertEqual(p.writes, [("available", False)])
        self.assertTrue(self.store.load()["took_off"]["paralon"])

    def test_vast_renter_reclaims_spot_rental(self):
        v, p = FakeVast(running=1), FakeParalon(available=True, rented=True)
        self.ctl(v, p).poll()
        self.assertIn(("reclaim",), p.writes)
        self.assertIn(("available", False), p.writes)

    def test_reclaim_not_repeated_within_retry_window(self):
        v, p = FakeVast(running=1), FakeParalon(available=False, rented=True)
        now = [1000.0]
        c = self.ctl(v, p, clock=lambda: now[0])
        c.poll()
        now[0] += 60
        c.poll()
        self.assertEqual(p.writes.count(("reclaim",)), 1)
        now[0] += 400
        c.poll()
        self.assertEqual(p.writes.count(("reclaim",)), 2)

    def test_on_demand_rental_is_not_reclaimed_and_warns(self):
        v = FakeVast(running=1)
        p = FakeParalon(available=True, rented=True, spot=False)
        with self.assertLogs("vpdual", "WARNING") as logs:
            self.ctl(v, p).poll()
        self.assertNotIn(("reclaim",), p.writes)
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
        self.assertTrue(self.store.load()["took_off"]["vast"])


class Rule3RelistOnlyWhatWeTookOff(Base):
    def test_paralon_restored_after_vast_rental_ends(self):
        v, p = FakeVast(running=1), FakeParalon(available=True)
        c = self.ctl(v, p)
        c.poll()
        v.running = 0
        c.poll()
        self.assertEqual(p.writes, [("available", False), ("available", True)])
        self.assertFalse(self.store.load()["took_off"]["paralon"])

    def test_vast_relisted_with_configured_price(self):
        v, p = FakeVast(listed=True), FakeParalon(rented=True)
        c = self.ctl(v, p, policy="unlist_vast")
        c.poll()
        p.rented = False
        c.poll()
        self.assertEqual(v.writes[-1][0], "list")
        self.assertEqual(v.writes[-1][2]["price_gpu"], 0.5)
        self.assertEqual(v.writes[-1][2]["price_min_bid"], 0.3)

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
        self.assertTrue(self.store.load()["took_off"]["vast"])
        v.fail_write = False
        c.poll()
        self.assertTrue(v.listed)
        self.assertFalse(self.store.load()["took_off"]["vast"])


class Rule4HoldOnUnreadableState(Base):
    def test_paralon_down_holds(self):
        v, p = FakeVast(running=1), FakeParalon()
        p.fail_read = True
        self.assertFalse(self.ctl(v, p).poll())
        self.assertEqual(v.writes + p.writes, [])
        self.assertIn("hold", self.store.load()["decisions"][-1]["msg"])

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


class Modes(Base):
    def test_mode_vast(self):
        v, p = FakeVast(listed=False), FakeParalon(available=True)
        self.ctl(v, p, mode="vast").poll()
        self.assertEqual(p.writes, [("available", False)])
        self.assertEqual(v.writes[0][0], "list")

    def test_mode_paralon(self):
        v, p = FakeVast(listed=True), FakeParalon(available=False)
        self.ctl(v, p, mode="paralon").poll()
        self.assertEqual(v.writes, [("unlist", 111)])
        self.assertEqual(p.writes, [("available", True)])

    def test_mode_paralon_waits_for_running_vast_renter(self):
        v, p = FakeVast(listed=True, running=1), FakeParalon(available=False)
        c = self.ctl(v, p, mode="paralon")
        c.poll()
        self.assertEqual(v.writes, [("unlist", 111)])
        self.assertEqual(p.writes, [])     # GPU still busy with Vast renter
        v.running = 0
        c.poll()
        self.assertEqual(p.writes, [("available", True)])

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
        self.assertEqual(self.store.load()["took_off"],
                         {"vast": False, "paralon": False})

    def test_mode_is_persisted(self):
        self.ctl(FakeVast(), FakeParalon(), mode="paralon")
        self.assertEqual(StateStore(self.state_path).load()["mode"], "paralon")


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
        c = self.ctl(v, p, mode=None, dry_run=True)
        c.state["mode"] = "dual"
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
        self.assertEqual(p.writes[-1], ("available", True))

    def test_flag_saved_before_the_write(self):
        v, p = FakeVast(running=1), FakeParalon(available=True)
        c = self.ctl(v, p)
        seen = {}

        def set_available(node_id, available):
            seen["flag_on_disk"] = StateStore(self.state_path).load()[
                "took_off"]["paralon"]
            p.available = available
        p.set_available = set_available
        c.poll()
        self.assertTrue(seen["flag_on_disk"])


if __name__ == "__main__":
    unittest.main()
