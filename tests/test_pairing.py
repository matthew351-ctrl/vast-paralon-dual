"""v0.6.1: pairing suggestions by name -- in `init` (a default the host
confirms) and in the web UI's "Add a machine" (GET /api/discover)."""

import json
import os
import unittest

from vpdual import pairing
from vpdual.config import load_config
from vpdual.install import run_init

from helpers import FakeParalon, FakeVast, TempDirMixin
from status_scenario import KEY
from test_init import Answers, init_args
from test_webui import Server


class Matcher(unittest.TestCase):
    def test_normalised_names(self):
        self.assertEqual(pairing.norm(" GPU-Box_1.local "), "gpubox1local")
        self.assertEqual(pairing.norm(None), "")

    def test_tiers(self):
        self.assertEqual(pairing.score("GPU Box 1", "gpu-box-1")[0], pairing.EXACT)
        self.assertEqual(pairing.score("gpu-box-1", "gpu_box_1_paralon")[0],
                         pairing.PREFIX)
        self.assertEqual(pairing.score("gpubox-11", "gpu-box-12")[0], pairing.FUZZY)
        self.assertEqual(pairing.score("alpha", "omega")[0], 0)
        self.assertEqual(pairing.score("", "x")[0], 0)

    def test_best_first_each_used_once(self):
        got = pairing.suggest(
            [(1, ["gpu-box-1"]), (2, ["gpu-box-2"]), (3, ["other"]), (4, [])],
            [("n-a", ["GPU Box 2"]), ("n-b", ["gpu_box_1"]),
             ("n-c", ["gpu-box-1-old"])])
        self.assertEqual(got, {1: ("n-b", pairing.EXACT),
                               2: ("n-a", pairing.EXACT)})

    def test_a_node_goes_to_its_best_machine(self):
        # n-a matches 2 exactly and 1 only by prefix: 2 gets it, 1 nothing
        got = pairing.suggest([(1, ["box"]), (2, ["box-a"])],
                              [("n-a", ["box_a"])])
        self.assertEqual(got, {2: ("n-a", pairing.EXACT)})

    def test_ties_go_to_the_lower_vast_id(self):
        got = pairing.suggest([(20, ["box"]), (3, ["box"])], [("n", ["box"])])
        self.assertEqual(got, {3: ("n", pairing.EXACT)})


class InitSuggests(TempDirMixin, unittest.TestCase):
    """Nodes are listed in this order: 1 node-1, 2 gpu_box_1, 3 GPU-Box-2."""

    def setUp(self):
        super().setUp()
        self.cfg_path = os.path.join(self.tmp, "conf", "config.json")
        self.out = []
        self.vast = FakeVast().add(222).add(333)
        self.vast.machines[111]["hostname"] = "gpu box 2"
        self.vast.machines[222]["hostname"] = "gpu-box-1x"
        self.paralon = FakeParalon().add("gpu_box_1").add("GPU-Box-2")

    def run_it(self, *answers):
        ask = Answers(*answers)
        code = run_init(init_args(self.cfg_path), env={"PARALON_API_KEY": KEY},
                        which=lambda b: "/usr/bin/vastai", ask=ask,
                        out=self.out.append, vast=self.vast, paralon=self.paralon)
        self.assertEqual(code, 0, self.out)
        cfg = load_config(self.cfg_path, env={"PARALON_API_KEY": KEY})
        return ask, [(m.vast_machine_id, m.paralon_node_id) for m in cfg.machines]

    def test_enter_takes_the_suggestion(self):
        # policy; 111 Enter (= node 3, exact); 222 Enter (= node 2, prefix);
        # 333 has no suggestion: Enter skips; then the two agent questions
        ask, pairs = self.run_it("", "", "", "", "", "")
        self.assertEqual(pairs, [(111, "GPU-Box-2"), (222, "gpu_box_1")])
        self.assertIn("[3]", ask.prompts[1])
        self.assertIn("[2]", ask.prompts[2])
        self.assertIn("(Enter = skip)", ask.prompts[3])
        text = "\n".join(self.out)
        self.assertIn("suggested: node 3", text)
        self.assertIn("exact name match", text)
        self.assertIn("prefix name match", text)

    def test_the_host_overrides_or_skips(self):
        # 111 picks node 1 instead; 222 skips with "-"; 333 takes node 3
        ask, pairs = self.run_it("", "1", "-", "3", "", "")
        self.assertEqual(pairs, [(111, "node-1"), (333, "GPU-Box-2")])

    def test_a_taken_suggestion_is_not_offered_again(self):
        # 111 takes node 2 (222's match) by hand: 222 then has no default
        ask, pairs = self.run_it("", "2", "", "", "")
        self.assertEqual(pairs, [(111, "gpu_box_1")])
        self.assertIn("(Enter = skip)", ask.prompts[2])

    def test_no_names_no_suggestions(self):
        for m in self.vast.machines.values():
            m.pop("hostname", None)
        ask, pairs = self.run_it("", "", "2", "", "")
        self.assertEqual(pairs, [(222, "gpu_box_1")])
        self.assertTrue(all("[" not in p for p in ask.prompts[1:4]))


class Discover(Server):
    def start(self, **kw):
        super().start(**kw)
        self.vast.machines[222]["hostname"] = "gpu-box-b"
        self.vast.add(555).machines[555]["hostname"] = "GPU Box E"
        self.vast.add(666).machines[666]["hostname"] = "unmatched"
        self.paralon.add("gpu_box_e").add("spare-node")

    def discover(self):
        resp, text = self.request("GET", "/api/discover")
        self.assertEqual(resp.status, 200)
        return json.loads(text), text

    def test_lists_the_users_machines_from_the_status_read(self):
        self.start()
        self.request("GET", "/api/status")
        reads = (self.vast.reads, self.paralon.reads)
        got, text = self.discover()
        self.assertEqual((self.vast.reads, self.paralon.reads), reads)  # no call
        self.assertTrue(got["read"])
        vast = {v["id"]: v for v in got["vast"]}
        self.assertTrue(vast[222]["configured"])
        self.assertFalse(vast[555]["configured"])
        self.assertEqual(vast[555]["hostname"], "GPU Box E")
        nodes = {n["id"]: n for n in got["paralon"]}
        self.assertTrue(nodes["node-1"]["configured"])
        self.assertFalse(nodes["gpu_box_e"]["configured"])
        # only machines and nodes not in the config are paired up
        self.assertEqual(got["suggest"], {"555": {"node": "gpu_box_e",
                                                  "match": "exact"}})
        self.assertNotIn(KEY, text)

    def test_reads_once_if_status_has_not_run(self):
        self.start()
        got, _ = self.discover()
        self.assertTrue(got["read"])
        self.assertEqual(self.vast.reads, 1)
        self.discover()
        self.assertEqual(self.vast.reads, 1)       # cached status read

    def test_unreadable_accounts_say_so(self):
        self.start(vast_fail=True)
        got, _ = self.discover()
        self.assertFalse(got["read"])
        self.assertEqual((got["vast"], got["paralon"], got["suggest"]),
                         ([], [], {}))
        self.assertIn("by hand", got["message"])

    def test_host_check_applies(self):
        self.start()
        resp, _ = self.request("GET", "/api/discover",
                               host=f"evil.example:{self.port}")
        self.assertEqual(resp.status, 403)

    def test_page_has_the_add_panel(self):
        from vpdual.webui import PAGE
        for needle in ('fetch("/api/discover"', '"Add this pair"',
                       '"Type the ids instead"', "D.suggest[vsel.value]"):
            self.assertIn(needle, PAGE)


if __name__ == "__main__":
    unittest.main()
