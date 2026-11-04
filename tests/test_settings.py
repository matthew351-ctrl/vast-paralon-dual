"""The web UI's Settings tab: check_config reuse, round-trip of custom keys,
backup + atomic write, owed machines, the key, dry run, token/Origin."""

import json
import os
import stat
import unittest

from vpdual import config as config_mod
from vpdual import settings
from vpdual.config import Config, ConfigError, check_config, parse_config
from vpdual.webui import TOKEN_HEADER

from status_scenario import KEY
from test_webui import Server

ENV = {"PARALON_API_KEY": KEY}


def scenario_raw(state_path):
    """The status scenario's machines as a config file, with custom data."""
    return {
        "_comment": "my notes, keep me",
        "on_paralon_rental": "unlist_vast",
        "machines": [
            {"vast_machine_id": 111, "paralon_node_id": "node-1",
             "paralon_agent": "user@gpu1", "_location": "rack-1",
             "vast_listing": {"_note": "per-machine note", "price_gpu": 0.5}},
            {"vast_machine_id": 222, "paralon_node_id": "node-2"},
            {"vast_machine_id": 333, "paralon_node_id": "node-3"},
            {"vast_machine_id": 444, "paralon_node_id": "node-4"}],
        "vast_listing": {"price_gpu": 0.5, "price_min_bid": 0.3,
                         "duration": "14 days"},
        "state_file": state_path,
        "idle_job_images": ["me/miner"],
        "poll_interval_s": 60,
    }


class Base(Server):
    def start(self, dry_run=False, raw=None, **kw):
        super().start(dry_run=dry_run, **kw)
        self.cfg_path = os.path.join(self.tmp, "config.json")
        with open(self.cfg_path, "w", encoding="utf-8") as fh:
            json.dump(raw or scenario_raw(self.state_path), fh, indent=1)
            fh.write("\n")
        self.rebuilt = []
        self.app.config_path = self.cfg_path
        self.app.environ = {}
        self.app.rebuild = lambda cfg: (self.rebuilt.append(cfg) or
                                        (self.vast, self.paralon))

    def get_config(self):
        resp, text = self.request("GET", "/api/config")
        return resp.status, json.loads(text), text

    def post(self, path, body, **kw):
        h = {"Content-Type": "application/json",
             TOKEN_HEADER: kw.pop("token", self.app.token),
             "Origin": kw.pop("origin", f"http://127.0.0.1:{self.port}")}
        h = {k: v for k, v in h.items() if v is not None}
        resp, text = self.request("POST", path, body, h, **kw)
        return resp.status, json.loads(text), text

    def save(self, cfg, base=None, **kw):
        if base is None:
            base = self.get_config()[1]["base"]
        return self.post("/api/config", {"config": cfg, "base": base}, **kw)

    def file_text(self):
        with open(self.cfg_path, encoding="utf-8") as fh:
            return fh.read()

    def file_raw(self):
        return json.loads(self.file_text())

    def backups(self):
        return sorted(f for f in os.listdir(self.tmp)
                      if f.startswith("config.json.bak-"))


class ValidatorReuse(unittest.TestCase):
    def test_settings_uses_the_config_validator(self):
        self.assertIs(settings.check_config, config_mod.check_config)

    def test_every_error_with_its_field(self):
        raw = {"machines": [{"vast_machine_id": "x", "paralon_node_id": ""},
                            {"vast_machine_id": 2, "paralon_node_id": "n",
                             "paralon_agent": "bad target", "name": " x"}],
               "poll_interval_s": 0, "docker_events": "yes",
               "vast_listing": {"price_gpu": "cheap", "end_date": 1,
                                "duration": "1 day"}}
        cfg, errors = check_config(raw, ENV)
        self.assertIsNone(cfg)
        self.assertEqual([f for f, _ in errors], [
            "vast_listing.price_gpu", "vast_listing.duration",
            "machines[0].vast_machine_id", "machines[0].paralon_node_id",
            "machines[1].paralon_agent", "machines[1].name",
            "poll_interval_s", "docker_events"])
        # load_config still reports the first one, as before
        with self.assertRaises(ConfigError) as cm:
            parse_config(raw, ENV)
        self.assertEqual(str(cm.exception), errors[0][1])
        self.assertEqual(cm.exception.errors, errors)

    def test_settings_check_is_check_config_plus_its_rules(self):
        raw = scenario_raw("/nonexistent/state.json")
        bad = dict(raw, poll_interval_s=-1)
        _, errors, _, cfg = settings.check(raw, bad, key=KEY)
        self.assertIsNone(cfg)
        self.assertEqual(errors, check_config(bad, ENV)[1])

    def test_schema_covers_every_setting(self):
        settable = set(Config.__dataclass_fields__) - {
            "machines", "vast_listing", "paralon_api_key"}
        self.assertEqual(settings.shown_keys(), settable)
        s = settings.schema()
        f = {x["key"]: x for g in s["groups"] for x in g["fields"]}
        self.assertEqual(f["poll_interval_s"],
                         {"key": "poll_interval_s", "type": "int",
                          "default": 60, "unit": "s", "options": None})
        self.assertEqual(f["docker_events"]["type"], "bool")
        self.assertEqual(f["idle_job_images"]["type"], "list")
        self.assertEqual(f["on_paralon_rental"]["options"],
                         ["unlist_vast", "keep_vast_listed"])
        self.assertEqual([x["key"] for x in s["listing"]],
                         list(config_mod.LISTING_FLAGS))


class RoundTrip(Base):
    def test_custom_keys_order_and_indent_kept(self):
        self.start()
        _, got, _ = self.get_config()
        cfg = got["config"]
        # what a client that knows nothing of the _ keys sends back
        cfg = {k: v for k, v in cfg.items() if not k.startswith("_")}
        cfg["machines"] = [{k: v for k, v in m.items() if not k.startswith("_")}
                           for m in cfg["machines"]]
        cfg["machines"][0]["vast_listing"] = {"price_gpu": 0.6}
        cfg["poll_interval_s"] = 30
        cfg["machines"][1]["name"] = "GPU host B"
        code, reply, _ = self.save(cfg, base=got["base"])
        self.assertEqual((code, reply["saved"]), (200, True), reply)
        raw = self.file_raw()
        self.assertEqual(raw["_comment"], "my notes, keep me")
        self.assertEqual(raw["machines"][0]["_location"], "rack-1")
        self.assertEqual(raw["machines"][0]["vast_listing"],
                         {"_note": "per-machine note", "price_gpu": 0.6})
        self.assertEqual(raw["poll_interval_s"], 30)
        self.assertEqual(list(raw), list(scenario_raw(self.state_path)))
        self.assertTrue(self.file_text().startswith('{\n "_comment"'))  # indent 1
        self.assertEqual({r["path"] for r in reply["diff"]},
                         {"poll_interval_s", "machine 222.name",
                          "machine 111.vast_listing.price_gpu"})

    def test_custom_data_follows_its_machine_not_its_position(self):
        self.start()
        raw = self.file_raw()
        raw["machines"][3]["_location"] = "rack-4"
        with open(self.cfg_path, "w", encoding="utf-8") as fh:
            json.dump(raw, fh, indent=1)
        _, got, _ = self.get_config()
        cfg = got["config"]
        # drop 333, put 444 first, strip every _ key the page might lose
        ms = {m["vast_machine_id"]: {k: v for k, v in m.items()
                                     if not k.startswith("_")}
              for m in cfg["machines"]}
        cfg["machines"] = [ms[444], ms[111], ms[222]]
        self.assertEqual(self.save(cfg, base=got["base"])[0], 200)
        saved = {m["vast_machine_id"]: m for m in self.file_raw()["machines"]}
        self.assertEqual(saved[444]["_location"], "rack-4")
        self.assertEqual(saved[111]["_location"], "rack-1")
        self.assertNotIn("_location", saved[222])

    def test_a_redacted_value_is_never_written_back(self):
        self.start()
        raw = self.file_raw()
        raw["paralon_base_url"] = "https://example.invalid/api/v1?api_key=s3cret"
        with open(self.cfg_path, "w", encoding="utf-8") as fh:
            json.dump(raw, fh, indent=1)
        _, got, text = self.get_config()
        self.assertNotIn("s3cret", text)
        cfg = got["config"]                     # holds api_key=[redacted]
        cfg["poll_interval_s"] = 45
        code, reply, _ = self.save(cfg, base=got["base"])
        self.assertEqual(code, 200, reply)
        self.assertEqual(self.file_raw()["paralon_base_url"],
                         "https://example.invalid/api/v1?api_key=s3cret")
        self.assertEqual([r["path"] for r in reply["diff"]], ["poll_interval_s"])

    def test_legacy_single_machine_file_opens_as_a_list(self):
        self.start(raw={"vast_machine_id": 111, "paralon_node_id": "node-1",
                        "state_file": self.state_path})
        _, got, _ = self.get_config()
        self.assertEqual(got["config"]["machines"],
                         [{"vast_machine_id": 111, "paralon_node_id": "node-1"}])
        self.assertNotIn("vast_machine_id", got["config"])

    def test_nothing_changed_writes_nothing(self):
        self.start()
        before = self.file_text()
        _, got, _ = self.get_config()
        code, reply, _ = self.save(got["config"], base=got["base"])
        self.assertEqual((code, reply["saved"]), (200, False))
        self.assertEqual(self.file_text(), before)
        self.assertEqual(self.backups(), [])


class BackupAndAtomicWrite(Base):
    def test_backup_then_replace_mode_600(self):
        self.start()
        before = self.file_text()
        _, got, _ = self.get_config()
        cfg = got["config"]
        cfg["poll_interval_s"] = 45
        code, reply, _ = self.save(cfg, base=got["base"])
        self.assertEqual(code, 200, reply)
        [bak] = self.backups()
        self.assertEqual(reply["backup"], bak)
        with open(os.path.join(self.tmp, bak), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), before)
        for path in (self.cfg_path, os.path.join(self.tmp, bak)):
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600, path)
        self.assertEqual([f for f in os.listdir(self.tmp)
                          if f.startswith(".config-")], [])   # no temp left
        self.assertIn("restart", reply["message"])
        self.assertIn(settings.RESTART_COMMAND, reply["message"])

    def test_keeps_the_last_ten_and_never_other_files(self):
        self.start()
        for i in range(12):
            open(os.path.join(self.tmp, f"config.json.bak-20260101-0000{i:02d}"),
                 "w").close()
        open(os.path.join(self.tmp, "config.json.bak-before-names"), "w").close()
        _, got, _ = self.get_config()
        cfg = got["config"]
        cfg["poll_interval_s"] = 45
        self.assertEqual(self.save(cfg, base=got["base"])[0], 200)
        stamped = [f for f in self.backups() if f != "config.json.bak-before-names"]
        self.assertEqual(len(stamped), settings.KEEP_BACKUPS)
        self.assertIn("config.json.bak-before-names", self.backups())
        self.assertNotIn("config.json.bak-20260101-000000", stamped)

    def test_a_failed_write_leaves_the_old_file(self):
        self.start()
        before = self.file_text()
        raw = self.file_raw()
        raw["poll_interval_s"] = 45
        real = os.replace

        def boom(*a):
            raise OSError("disk full")
        settings.os.replace = boom
        try:
            with self.assertRaises(OSError):
                settings.write_atomic(self.cfg_path, raw)
        finally:
            settings.os.replace = real
        self.assertEqual(self.file_text(), before)
        self.assertEqual([f for f in os.listdir(self.tmp)
                          if f.startswith(".config-")], [])

    def test_changed_on_disk_is_refused(self):
        self.start()
        _, got, _ = self.get_config()
        with open(self.cfg_path, "a", encoding="utf-8") as fh:
            fh.write(" ")                       # someone else edited it
        cfg = got["config"]
        cfg["poll_interval_s"] = 45
        code, reply, _ = self.save(cfg, base=got["base"])
        self.assertEqual(code, 409)
        self.assertEqual(self.backups(), [])

    def test_the_page_uses_the_new_config_after_a_save(self):
        self.start()
        _, got, _ = self.get_config()
        cfg = got["config"]
        cfg["machines"][1]["name"] = "GPU host B"
        self.assertEqual(self.save(cfg, base=got["base"])[0], 200)
        self.assertEqual(self.app.cfg.machine(222).name, "GPU host B")
        self.assertEqual(self.app.cfg.paralon_api_key, KEY)    # key kept
        self.assertEqual(len(self.rebuilt), 1)
        s = json.loads(self.request("GET", "/api/status")[1])
        self.assertEqual(s["machines"][1]["name"], "GPU host B")


class Refusals(Base):
    def test_invalid_config_saves_nothing_and_says_where(self):
        self.start()
        before = self.file_text()
        _, got, _ = self.get_config()
        cfg = got["config"]
        cfg["poll_interval_s"] = "abc"
        cfg["machines"][2]["paralon_agent"] = "bad target"
        code, reply, _ = self.post("/api/config/check", {"config": cfg})
        self.assertEqual((code, reply["valid"]), (200, False))
        self.assertEqual([e["field"] for e in reply["errors"]],
                         ["machines[2].paralon_agent", "poll_interval_s"])
        code, reply, _ = self.save(cfg, base=got["base"])
        self.assertEqual(code, 400)
        self.assertEqual(len(reply["errors"]), 2)
        self.assertEqual(self.file_text(), before)
        self.assertEqual(self.backups(), [])

    def test_owed_machine_cannot_be_removed(self):
        self.start()                       # 111 owes a Vast relist + idle job
        before = self.file_text()
        _, got, _ = self.get_config()
        self.assertIn("a Vast relist", got["owed"]["111"]["what"])
        cfg = got["config"]
        cfg["machines"] = cfg["machines"][1:]
        code, reply, _ = self.save(cfg, base=got["base"])
        self.assertEqual(code, 400)
        [err] = reply["errors"]
        self.assertEqual(err["field"], "machines")
        self.assertIn("Machine 111 cannot be removed", err["message"])
        self.assertIn("idle job", err["message"])
        self.assertEqual(self.file_text(), before)

    def test_a_machine_owing_nothing_can_be_removed(self):
        self.start()
        _, got, _ = self.get_config()
        cfg = got["config"]
        cfg["machines"] = [m for m in cfg["machines"]
                           if m["vast_machine_id"] != 333]
        self.assertEqual(self.save(cfg, base=got["base"])[0], 200)
        self.assertEqual([m["vast_machine_id"] for m in self.file_raw()["machines"]],
                         [111, 222, 444])

    def test_owed_paralon_node_cannot_change(self):
        self.start()
        store = settings.StateStore(self.state_path)
        st = store.load()
        st["machines"]["222"]["took_off"]["paralon"] = True
        store.save(st)
        _, got, _ = self.get_config()
        cfg = got["config"]
        cfg["machines"][1]["paralon_node_id"] = "node-new"
        code, reply, _ = self.save(cfg, base=got["base"])
        self.assertEqual(code, 400)
        self.assertEqual(reply["errors"][0]["field"],
                         "machines[1].paralon_node_id")

    def test_state_file_cannot_move_while_owed(self):
        self.start()
        _, got, _ = self.get_config()
        cfg = got["config"]
        cfg["state_file"] = os.path.join(self.tmp, "other.json")
        code, reply, _ = self.save(cfg, base=got["base"])
        self.assertEqual(code, 400)
        self.assertEqual(reply["errors"][0]["field"], "state_file")


class TheKey(Base):
    def test_never_in_any_response(self):
        self.start()
        raw = scenario_raw(self.state_path)
        raw["_comment"] = f"oops, pasted my key here: {KEY}"
        with open(self.cfg_path, "w", encoding="utf-8") as fh:
            json.dump(raw, fh)
        self.app.environ = {"PARALON_API_KEY": KEY}
        code, got, text = self.get_config()
        self.assertEqual(code, 200)
        self.assertNotIn(KEY, text)
        self.assertEqual(got["key"]["set"], True)
        self.assertIn("environment", got["key"]["source"])
        cfg = got["config"]
        cfg["poll_interval_s"] = 45
        for path, body in (("/api/config/check", {"config": cfg}),
                           ("/api/config", {"config": cfg, "base": got["base"]})):
            _, _, text = self.post(path, body)
            self.assertNotIn(KEY, text)
        self.assertIn(KEY, self.file_text())   # the user's own data, kept

    def test_key_source_is_the_env_file(self):
        self.start()
        with open(os.path.join(self.tmp, "env"), "w") as fh:
            fh.write(f"PARALON_API_KEY={KEY}\n")
        _, got, text = self.get_config()
        self.assertEqual(got["key"], {"set": True,
                                      "source": os.path.join(self.tmp, "env"),
                                      "env_file": os.path.join(self.tmp, "env")})
        self.assertNotIn(KEY, text)


class DryRun(Base):
    def test_shows_the_diff_and_saves_nothing(self):
        self.start(dry_run=True)
        before = self.file_text()
        _, got, _ = self.get_config()
        self.assertTrue(got["dry_run"])
        cfg = got["config"]
        cfg["poll_interval_s"] = 45
        code, reply, _ = self.save(cfg, base=got["base"])
        self.assertEqual((code, reply["saved"]), (200, False))
        self.assertEqual(reply["diff"], [{"path": "poll_interval_s",
                                          "kind": "changed", "old": 60,
                                          "new": 45}])
        self.assertEqual(self.file_text(), before)
        self.assertEqual(self.backups(), [])


class Protection(Base):
    def test_token_origin_host_and_size(self):
        self.start()
        before = self.file_text()
        _, got, _ = self.get_config()
        cfg = got["config"]
        cfg["poll_interval_s"] = 45
        body = {"config": cfg, "base": got["base"]}
        for path in ("/api/config", "/api/config/check"):
            self.assertEqual(self.post(path, body, token=None)[0], 403)
            self.assertEqual(self.post(path, body, token="wrong")[0], 403)
            self.assertEqual(self.post(path, body,
                                       origin="http://evil.example")[0], 403)
            self.assertEqual(self.post(path, body,
                                       host=f"evil.example:{self.port}")[0], 403)
        big = json.dumps({"config": cfg, "pad": "x" * settings.MAX_CONFIG_BODY})
        self.assertEqual(self.post("/api/config", big.encode())[0], 413)
        resp, _ = self.request("GET", "/api/config",
                               host=f"evil.example:{self.port}")
        self.assertEqual(resp.status, 403)
        self.assertEqual(self.file_text(), before)

    def test_bad_bodies(self):
        self.start()
        for body in ([], "x", {"config": []}, {"config": "x"}):
            code, reply, _ = self.post("/api/config", body)
            self.assertEqual(code, 400, body)
        self.assertEqual(self.post("/api/config/check", [1])[0], 400)

    def test_no_config_path_means_no_settings(self):
        self.start()
        self.app.config_path = None
        self.assertEqual(self.get_config()[0], 404)


if __name__ == "__main__":
    unittest.main()
