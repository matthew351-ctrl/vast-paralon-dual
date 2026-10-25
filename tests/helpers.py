"""Fakes for the two sides. They record every write."""

import os
import shutil
import tempfile

from vpdual.config import Config
from vpdual.paralon import ParalonError, ParalonNode
from vpdual.state import StateStore
from vpdual.vast import VastError, VastMachine


class FakeVast:
    def __init__(self, listed=True, running=0, resident=0):
        self.listed, self.running, self.resident = listed, running, resident
        self.fail_read = False
        self.fail_write = False
        self.writes = []

    def read_machine(self, machine_id):
        if self.fail_read:
            raise VastError("cli error")
        return VastMachine(machine_id, self.listed, self.running, self.resident)

    def list_machine(self, machine_id, listing):
        self.writes.append(("list", machine_id, dict(listing)))
        if self.fail_write:
            raise VastError("list failed")
        self.listed = True

    def unlist_machine(self, machine_id):
        self.writes.append(("unlist", machine_id))
        if self.fail_write:
            raise VastError("unlist failed")
        self.listed = False


class FakeParalon:
    def __init__(self, available=True, rented=False, spot=True):
        self.available, self.rented, self.spot = available, rented, spot
        self.fail_read = False
        self.writes = []

    def get_node(self, node_id):
        if self.fail_read:
            raise ParalonError("HTTP 503")
        return ParalonNode(
            id=node_id, state="rented" if self.rented else
            ("available" if self.available else "paused"),
            available=self.available, spot=self.spot, price=1.0,
            rental={"id": "r1"} if self.rented else None)

    def set_available(self, node_id, available):
        self.writes.append(("available", available))
        self.available = available

    def reclaim(self, node_id):
        self.writes.append(("reclaim",))


def make_config(state_file, policy="keep_vast_listed"):
    return Config(
        vast_machine_id=111, paralon_node_id="node-1",
        on_paralon_rental=policy,
        vast_listing={"price_gpu": 0.5, "price_min_bid": 0.3,
                      "duration": "14 days"},
        state_file=state_file, paralon_api_key="prlc_test_secret")


class TempDirMixin:
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.state_path = os.path.join(self.tmp, "state.json")
        self.store = StateStore(self.state_path)

    def tearDown(self):
        shutil.rmtree(self.tmp)
