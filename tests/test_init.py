"""`init`, `service` and `status`, with fakes (no network, no vastai)."""

import argparse
import io
import json
import os
import stat
import subprocess
import unittest
from contextlib import redirect_stdout

from vpdual.cli import cmd_status, main
from vpdual.config import load_config
from vpdual.engine import Controller
from vpdual.install import add_init_args, render_service, run_init
from vpdual.paralon import ParalonNode
from vpdual.state import StateStore
from vpdual.vast import VastCLI

from helpers import FakeParalon, FakeVast, TempDirMixin, fixture, make_config

KEY = "prlc_init_secret"


def init_args(config, *extra):
    p = argparse.ArgumentParser()
    add_init_args(p)
    state = os.path.join(os.path.dirname(os.path.dirname(config)),
                         "state.json")
    args = p.parse_args(["--state-file", state] + list(extra))
    args.config = config
    return args


class Answers:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.prompts = []

    def __call__(self, prompt=""):
        self.prompts.append(prompt)
        return self.answers.pop(0)


class FixtureParalon:
    """list_nodes() from the real GET /nodes fixture."""

    def __init__(self):
        self.reads = 0

    def list_nodes(self):
        self.reads += 1
        return [ParalonNode.from_api(n)
                for n in fixture("fixture_paralon_nodes.json")["nodes"]]


class FixtureVast:
    """read_machines() parsed by the real VastCLI from the real fixture."""

    def __init__(self):
        payload = json.dumps(fixture("fixture_vast_show_machines.json"))
        self.cli = VastCLI(runner=lambda cmd, **kw: subprocess.CompletedProcess(
            cmd, 0, payload, ""))

    def read_machines(self):
        return self.cli.read_machines()


class Init(TempDirMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.cfg_path = os.path.join(self.tmp, "conf", "config.json")
        self.env = {"PARALON_API_KEY": KEY}
        self.out = []

    def run_init(self, args, ask=None, env=None, which=lambda b: "/usr/bin/vastai",
                 vast=None, paralon=None):
        return run_init(args, env=self.env if env is None else env,
                        which=which, ask=ask or Answers(), out=self.out.append,
                        vast=vast or FixtureVast(),
                        paralon=paralon or FixtureParalon())

    def loaded(self):
        return load_config(self.cfg_path, env={"PARALON_API_KEY": KEY})

    def test_interactive_writes_a_valid_config(self):
        node = fixture("fixture_paralon_nodes.json")["nodes"][0]["id"]
        ask = Answers("2", "1")           # unlist_vast; pair 12345 with node 1
        args = init_args(self.cfg_path)
        code = self.run_init(args, ask=ask)
        self.assertEqual(code, 0, self.out)
        cfg = self.loaded()
        self.assertEqual(cfg.on_paralon_rental, "unlist_vast")
        self.assertEqual([(m.vast_machine_id, m.paralon_node_id)
                          for m in cfg.machines], [(12345, node)])
        self.assertEqual(cfg.vastai_bin, "/usr/bin/vastai")
        text = "\n".join(self.out)
        self.assertIn("status", text)
        self.assertIn("systemctl enable --now vast-paralon-dual", text)
        self.assertNotIn(KEY, text)

    def test_env_file_and_mode(self):
        args = init_args(self.cfg_path, "--policy", "keep_vast_listed",
                         "--pair", "12345=00000000-0000-0000-0000-000000000001")
        self.assertEqual(self.run_init(args), 0, self.out)
        env_path = os.path.join(os.path.dirname(self.cfg_path), "env")
        with open(env_path) as fh:
            self.assertEqual(fh.read(), f"PARALON_API_KEY={KEY}\n")
        self.assertEqual(stat.S_IMODE(os.stat(env_path).st_mode), 0o600)
        st = StateStore(self.state_path).load([12345])
        self.assertEqual(st["machines"]["12345"]["mode"], "dual")
        self.assertFalse(any(st["machines"]["12345"]["took_off"].values()))

    def test_non_interactive_asks_nothing(self):
        ask = Answers()                   # any prompt would raise IndexError
        args = init_args(self.cfg_path, "--policy", "keep_vast_listed",
                         "--pair", "12345=00000000-0000-0000-0000-000000000001",
                         "--price-gpu", "0.4", "--price-min-bid", "0.3")
        self.assertEqual(self.run_init(args, ask=ask), 0, self.out)
        self.assertEqual(ask.prompts, [])
        cfg = self.loaded()
        self.assertEqual(cfg.machines[0].vast_listing,
                         {"price_gpu": 0.4, "price_min_bid": 0.3})

    def test_multi_machine_pairing_and_skip(self):
        v = FakeVast().add(222).add(333, listed=False)
        p = FakeParalon().add("node-2")
        ask = Answers("1", "2", "", "1", "", "")   # 111->node-2, 222 skip...
        # policy 1; 111 -> node #2 ("node-2"); 222 -> skip; 333 -> node #1;
        # 333 is unlisted -> asked for a fallback price -> skip
        args = init_args(self.cfg_path)
        self.assertEqual(self.run_init(args, ask=ask, vast=v, paralon=p), 0,
            self.out)
        pairs = [(m.vast_machine_id, m.paralon_node_id)
                 for m in self.loaded().machines]
        self.assertEqual(pairs, [(111, "node-2"), (333, "node-1")])
        self.assertIn("unlisted now", "\n".join(self.out))
        self.assertEqual((v.reads, p.reads), (1, 1))

    def test_refuses_without_key(self):
        code = self.run_init(init_args(self.cfg_path), env={})
        self.assertEqual(code, 2)
        self.assertIn("PARALON_API_KEY is not set", "\n".join(self.out))
        self.assertFalse(os.path.exists(self.cfg_path))

    def test_refuses_without_vastai(self):
        code = run_init(init_args(self.cfg_path), env=self.env,
                        which=lambda b: None, ask=Answers(),
                        out=self.out.append, paralon=FixtureParalon())
        self.assertEqual(code, 2)
        self.assertIn("pip install --user vastai", "\n".join(self.out))
        self.assertFalse(os.path.exists(self.cfg_path))

    def test_refuses_when_vastai_not_logged_in(self):
        v = FakeVast()
        v.fail_read = True
        code = self.run_init(init_args(self.cfg_path), vast=v)
        self.assertEqual(code, 2)
        self.assertIn("vastai set api-key", "\n".join(self.out))

    def test_refuses_unknown_pair_and_existing_config(self):
        args = init_args(self.cfg_path, "--policy", "unlist_vast",
                         "--pair", "999=nope")
        self.assertEqual(self.run_init(args), 2)
        self.assertIn("999 is not in your account", "\n".join(self.out))
        os.makedirs(os.path.dirname(self.cfg_path))
        open(self.cfg_path, "w").close()
        args = init_args(self.cfg_path, "--policy", "unlist_vast",
                         "--pair", "12345=00000000-0000-0000-0000-000000000001")
        self.assertEqual(self.run_init(args), 2)
        self.assertIn("--force", self.out[-1])


class Service(unittest.TestCase):
    def test_render_fills_every_placeholder(self):
        text = render_service("/home/h/.config/vast-paralon-dual/config.json",
                              user="hostuser", python="/usr/bin/python3")
        self.assertNotIn("@", text)
        self.assertIn("User=hostuser", text)
        self.assertIn("EnvironmentFile=/home/h/.config/vast-paralon-dual/env",
                      text)
        self.assertIn("-c /home/h/.config/vast-paralon-dual/config.json run",
                      text)
        self.assertIn("vast_paralon_dual.py", text)

    def test_service_command_needs_no_config(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(main(["-c", "/nonexistent/c.json", "service"]), 0)
        self.assertIn("ExecStart=", buf.getvalue())


class Status(TempDirMixin, unittest.TestCase):
    def test_status_shows_plan_and_writes_nothing(self):
        v = FakeVast(running=1).add(222)
        p = FakeParalon(available=True).add("node-2")
        cfg = make_config(self.state_path,
                          machines=[(111, "node-1"), (222, "node-2")])
        ctl = Controller(cfg, v, p, self.store, clock=lambda: 1000.0)
        ctl.set_mode("dual")
        with open(self.state_path) as fh:
            before = fh.read()
        out = []
        cmd_status(ctl, cfg, None, out=out.append)
        text = "\n".join(out)
        self.assertIn("[111] take paralon off", text)
        self.assertIn("Vast 222 <-> Paralon node-2", text)
        self.assertEqual(v.writes + p.writes, [])
        self.assertEqual((v.reads, p.reads), (1, 1))
        with open(self.state_path) as fh:
            self.assertEqual(fh.read(), before)


if __name__ == "__main__":
    unittest.main()
