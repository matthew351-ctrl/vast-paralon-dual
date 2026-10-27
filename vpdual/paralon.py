"""Minimal Paralon Cloud Provider API client (standard library only).

Endpoints used:
  GET   /nodes                  list your nodes
  PATCH /nodes/{id}             {"available": true|false}
  POST  /nodes/{id}/reclaim     end a SPOT rental (the renter gets 2 min notice)

Be gentle with the API: GET /nodes is served from a cache for
`min_get_interval_s` (120 s by default), all calls share a sliding window of
`max_calls_per_min` (6 by default), and every HTTP error and network error
starts an exponential back-off during which no call is made.
"""

import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

log = logging.getLogger("vpdual.paralon")

from . import __version__

USER_AGENT = f"vast-paralon-dual/{__version__}"
BACKOFF_BASE_S = 30
BACKOFF_MAX_S = 600
MAX_WAIT_FOR_SLOT_S = 20
ENDED = {"ended", "completed", "cancelled", "canceled", "stopped",
         "terminated", "expired", "finished"}


class ParalonError(Exception):
    """A Paralon call failed or was not attempted. The caller must hold."""


@dataclass
class ParalonNode:
    id: str
    state: str
    available: bool
    spot: bool
    price: object
    rental: object
    raw: dict = field(repr=False, default_factory=dict)

    @property
    def rented(self):
        if self.state == "rented":
            return True
        if isinstance(self.rental, dict):
            if not self.rental:
                return False            # an empty record is no rental
            # A record that says it is over does not count; any other (or
            # missing) status is treated as rented, the safe side.
            return str(self.rental.get("status", "")).lower() not in ENDED
        return bool(self.rental)

    @property
    def price_text(self):
        p = self.price
        if isinstance(p, dict):
            # What a renter actually pays: renter_pays (a spot node pays less
            # than the default), else the custom price, else the default.
            for key in ("renter_pays", "custom", "default"):
                if p.get(key) is not None:
                    amount = p[key]
                    break
            else:
                amount = "?"
            return f"{amount} {p.get('currency', '')}/{p.get('unit', 'hour')}"
        return str(p)

    @classmethod
    def from_api(cls, item):
        if not isinstance(item, dict) or "id" not in item:
            raise ParalonError(f"unexpected node record: {item!r:.200}")
        return cls(
            id=str(item["id"]),
            state=str(item.get("state") or "unknown"),
            available=bool(item.get("available")),
            spot=bool(item.get("spot")),
            price=item.get("price"),
            rental=item.get("rental"),
            raw=item,
        )

    @property
    def label(self):
        gpus = self.raw.get("gpus") or []
        models = ", ".join(sorted({str(g.get("model", "?")) for g in gpus
                                   if isinstance(g, dict)}))
        count = f"{len(gpus)}x {models}" if gpus else "no GPU info"
        name = self.raw.get("name") or ""
        return f"{name} ({count})".strip()


class ParalonClient:
    def __init__(self, base_url, api_key, *, min_get_interval_s=120,
                 max_calls_per_min=6, opener=None, clock=time.monotonic,
                 sleep=time.sleep, timeout=20):
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self.min_get_interval_s = min_get_interval_s
        self.max_calls_per_min = max_calls_per_min
        self._open = opener or urllib.request.urlopen
        self._clock = clock
        self._sleep = sleep
        self.timeout = timeout
        self._calls = []            # monotonic timestamps of recent calls
        self._backoff_until = 0.0
        self._failures = 0
        self._nodes_cache = None    # (fetched_at, [ParalonNode])

    def __repr__(self):  # never show the key
        return f"ParalonClient({self.base_url!r})"

    # -- rate limiting -------------------------------------------------
    def _take_slot(self):
        now = self._clock()
        if now < self._backoff_until:
            raise ParalonError(
                f"backing off for {self._backoff_until - now:.0f}s after "
                "earlier errors; not calling Paralon")
        self._calls = [t for t in self._calls if now - t < 60]
        if len(self._calls) >= self.max_calls_per_min:
            wait = 60 - (now - self._calls[0])
            if wait > MAX_WAIT_FOR_SLOT_S:
                raise ParalonError(
                    f"local rate limit ({self.max_calls_per_min}/min) reached")
            log.info("Paralon rate limit: waiting %.0fs for a call slot", wait)
            self._sleep(wait)
            now = self._clock()
            self._calls = [t for t in self._calls if now - t < 60]
        self._calls.append(now)

    def _start_backoff(self, retry_after=None):
        self._failures += 1
        delay = min(BACKOFF_BASE_S * 2 ** (self._failures - 1), BACKOFF_MAX_S)
        if retry_after is not None:
            delay = max(delay, retry_after)
        self._backoff_until = self._clock() + delay
        return delay

    # -- HTTP ----------------------------------------------------------
    def _request(self, method, path, body=None):
        self._take_slot()
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base_url + path, data=data,
                                     method=method)
        req.add_header("Authorization", "Bearer " + self._api_key)
        req.add_header("Accept", "application/json")
        # Paralon's edge refuses Python's default "Python-urllib/x.y" agent
        # with HTTP 403 (found on the first real run), so name the tool.
        req.add_header("User-Agent", USER_AGENT)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with self._open(req, timeout=self.timeout) as resp:
                payload = resp.read()
        except urllib.error.HTTPError as exc:
            code = exc.code
            try:                       # Paralon's own reason, when it gives one
                detail = exc.read()[:200].decode("utf-8", "replace").strip()
            except Exception:
                detail = ""
            if detail:
                log.warning("%s %s -> HTTP %s: %s", method, path, code, detail)
            # EVERY HTTP error backs off (v0.4.3): a 400/404 that kept
            # failing used to be retried every pass.
            retry_after = None
            try:
                retry_after = float(exc.headers.get("Retry-After"))
            except (TypeError, ValueError, AttributeError):
                pass
            delay = self._start_backoff(retry_after)
            if code in (401, 403):
                raise ParalonError(
                    f"{method} {path} -> HTTP {code}: check PARALON_API_KEY "
                    "and that it has the \"My nodes\" permission; backing off "
                    f"{delay:.0f}s")
            raise ParalonError(
                f"{method} {path} -> HTTP {code}; backing off {delay:.0f}s")
        except (urllib.error.URLError, OSError) as exc:
            delay = self._start_backoff()
            raise ParalonError(
                f"{method} {path} failed ({exc.__class__.__name__}); "
                f"backing off {delay:.0f}s")
        self._failures = 0
        if not payload:
            return None
        try:
            return json.loads(payload)
        except ValueError:
            raise ParalonError(f"{method} {path} returned non-JSON")

    # -- API -----------------------------------------------------------
    def list_nodes(self, refresh=False):
        """GET /nodes, at most one per min_get_interval_s unless `refresh`
        (a machine recovering after a Vast rental); every call still counts
        toward max_calls_per_min."""
        now = self._clock()
        if (not refresh and self._nodes_cache
                and now - self._nodes_cache[0] < self.min_get_interval_s):
            return self._nodes_cache[1]
        data = self._request("GET", "/nodes")
        if isinstance(data, dict):
            data = data.get("nodes", data.get("data"))
        if not isinstance(data, list):
            raise ParalonError("GET /nodes: expected a list of nodes")
        nodes = [ParalonNode.from_api(item) for item in data]
        self._nodes_cache = (self._clock(), nodes)
        return nodes

    def cached_node(self, node_id):
        """The node from the last GET /nodes, or None. No API call."""
        for node in (self._nodes_cache or (0, []))[1]:
            if node.id == str(node_id):
                return node
        return None

    def get_node(self, node_id):
        for node in self.list_nodes():
            if node.id == str(node_id):
                return node
        raise ParalonError(f"node {node_id} not found in GET /nodes")

    def set_available(self, node_id, available):
        self._request("PATCH", f"/nodes/{node_id}",
                      {"available": bool(available)})
        # Keep the cache truthful so the next poll does not repeat the write.
        if self._nodes_cache:
            for node in self._nodes_cache[1]:
                if node.id == str(node_id):
                    node.available = bool(available)

    def reclaim(self, node_id):
        self._request("POST", f"/nodes/{node_id}/reclaim")
