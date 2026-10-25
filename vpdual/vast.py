"""Read and (un)list a Vast.ai machine through the official `vastai` CLI.

Only these commands are used (checked against vastai CLI 1.3.0 --help):
  vastai show machines --raw
  vastai list machine <id> --price_gpu X --price_min_bid Y --price_disk Z
      --price_inetu U --price_inetd D --end_date E [...]
  vastai unlist machine <id>

This module never touches the Vast daemon, its packages or its containers.
Unlisting does not evict a running renter; it only stops new bookings.
"""

import json
import logging
import subprocess
from dataclasses import dataclass, field

from .config import LISTING_FLAGS

log = logging.getLogger("vpdual.vast")


class VastError(Exception):
    """A vastai command failed or returned something unreadable."""


# `show machines --raw` field -> `list machine` key. These are the terms
# `vastai list machine` resets to its defaults when not passed, so all of
# them are saved before an unlist and passed back on the relist.
SAVED_FIELDS = {
    "listed_gpu_cost": "price_gpu",
    "min_bid_price": "price_min_bid",
    "listed_storage_cost": "price_disk",
    "listed_inet_up_cost": "price_inetu",
    "listed_inet_down_cost": "price_inetd",
    "end_date": "end_date",
}


def _num(value):
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


@dataclass
class VastMachine:
    id: int
    listed: bool
    running: int
    resident: int
    raw: dict = field(repr=False, default_factory=dict)

    @property
    def listing_terms(self):
        """The current listing terms as `list machine` keys (known only)."""
        terms = {}
        for src, key in SAVED_FIELDS.items():
            val = _num(self.raw.get(src))
            if val is not None:
                terms[key] = val
        return terms

    @classmethod
    def from_raw(cls, item):
        return cls(
            id=int(item["id"]),
            listed=bool(item["listed"]),
            running=int(item.get("current_rentals_running") or 0),
            resident=int(item.get("current_rentals_resident") or 0),
            raw=item,
        )


class VastCLI:
    def __init__(self, binary="vastai", *, runner=subprocess.run, timeout=90):
        self.binary = binary
        self._run = runner
        self.timeout = timeout

    def _call(self, args):
        cmd = [self.binary] + list(args)
        try:
            proc = self._run(cmd, capture_output=True, text=True,
                             timeout=self.timeout)
        except FileNotFoundError:
            raise VastError(f"{self.binary} not found; install it with "
                            "`pip install vastai` and run `vastai set api-key`")
        except subprocess.TimeoutExpired:
            raise VastError(f"{' '.join(cmd[:4])} timed out")
        out = (proc.stdout or "").strip()
        if proc.returncode != 0:
            err = (proc.stderr or out).strip().splitlines()
            raise VastError(f"{' '.join(cmd[:4])} exited {proc.returncode}: "
                            f"{err[-1] if err else ''}")
        return out

    def read_machines(self):
        """ONE `show machines --raw` call -> {machine id: VastMachine}.

        A record that cannot be read maps to a VastError instead, so only
        that machine holds.
        """
        out = self._call(["show", "machines", "--raw"])
        try:
            data = json.loads(out)
        except ValueError:
            raise VastError("`vastai show machines --raw` returned non-JSON")
        if isinstance(data, dict):
            data = data.get("machines")
        if not isinstance(data, list):
            raise VastError("`vastai show machines --raw`: no machine list")
        machines = {}
        for item in data:
            if not isinstance(item, dict) or item.get("id") is None:
                continue
            try:
                machines[int(item["id"])] = VastMachine.from_raw(item)
            except (KeyError, TypeError, ValueError) as exc:
                try:
                    machines[int(item["id"])] = VastError(
                        f"machine {item['id']}: unreadable ({exc})")
                except (TypeError, ValueError):
                    continue
        return machines

    def read_machine(self, machine_id):
        found = self.read_machines().get(int(machine_id))
        if found is None:
            raise VastError(f"machine {machine_id} not found in your Vast "
                            "account")
        if isinstance(found, VastError):
            raise found
        return found

    @staticmethod
    def list_args(machine_id, listing):
        args = ["list", "machine", str(machine_id)]
        for key, flag in LISTING_FLAGS.items():
            val = listing.get(key)
            if val is None:
                continue
            if key == "end_date" and isinstance(val, float) and val.is_integer():
                val = int(val)      # unix seconds, as the CLI expects
            args += [flag, str(val)]
        return args

    def list_machine(self, machine_id, listing):
        out = self._call(self.list_args(machine_id, listing))
        self._check_reply(out)

    def unlist_machine(self, machine_id):
        out = self._call(["unlist", "machine", str(machine_id)])
        self._check_reply(out)

    @staticmethod
    def _check_reply(out):
        # The CLI prints a server message; a JSON reply may carry success=false.
        try:
            data = json.loads(out)
        except ValueError:
            data = None
        if isinstance(data, dict) and data.get("success") is False:
            raise VastError(f"vastai refused: {data.get('msg') or data}")
        low = out.lower()
        if low.startswith("error") or "failed with error" in low:
            raise VastError(f"vastai refused: {out.splitlines()[0]}")
