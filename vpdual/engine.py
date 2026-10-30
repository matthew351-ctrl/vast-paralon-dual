"""The decision rules. One call to `Controller.poll()` = one pass over every
configured machine (or one, if a machine id is given).

Every pass reads BOTH sides first, with ONE `vastai show machines --raw` and
ONE `GET /nodes` shared by all machines. If either read fails, nothing is
written (rule 4: never act on unreadable state). A machine missing from a
read holds on its own.

Rules (all modes):
  1. A Vast renter is running -> Paralon goes unavailable. If Paralon also
     has a running rental on a SPOT node, reclaim it (Vast wins). An
     on-demand rental cannot be reclaimed: warn.
     For a machine with `paralon_agent` set, the Paralon agent is stopped
     too, then the containers it started (inference, downloads, benchmarks):
     a paused node's agent keeps starting them, and they take the Vast
     renter's GPU. While a Paralon rental is being reclaimed the agent stays
     up (it runs the reclaim). When no Vast renter is left, the agent is
     started again -- only if this tool stopped it, and only once the
     machine shows the GPU free (no compute process, memory near 0).
     "A Vast renter is running" = Vast's running count OR a running Vast
     container (C.<id>) on the machine itself: the count is a hint, the
     machine decides. Paralon is made available again only when the machine
     shows no Vast container, the agent running and the node online. While a
     machine recovers after a Vast rental, passes run every fast_poll_s.
     ANY node, spot or on-demand, stays paused while the Vast machine has a
     STORED instance (stopped, can resume: resident > running) -- unlisting
     Vast cannot stop a resume, and a resume beside a spot rental shares the
     GPU for the 2-minute reclaim (v0.5.0; on-demand only before). The node
     stays registered (available:false, never deleted) and is made available
     again once nothing is stored. A node already RENTED when an instance is
     stored cannot be paused out of its rental: warn; a resume is rule 1.
     With docker_events, a non-Paralon container starting on the machine
     pauses its Paralon node at once; the full pass follows.
  Every pass, for a machine with `paralon_agent` set: keep Paralon's Jupyter
  image pinned (Paralon rents a node only while it is present, and Vast's
  daemon deletes unused images within minutes). Never pulls.

Mode rules:
  vast     Paralon unavailable, Vast listed.
  paralon  Vast unlisted (a running Vast renter is never evicted), Paralon
           available once no Vast renter is running.
  dual     2. Paralon rented and no Vast renter -> per `on_paralon_rental`:
              keep Vast listed, or unlist it. "Rented" = the API says so OR a
              Paralon RENTAL container (label paralon.type=rental) runs on the
              machine; with docker_events the unlist happens the moment that
              container starts (v0.5.0), not at the next pass.
           3. When the reason ends, relist ONLY what this tool took off. After
              a Paralon rental, for a machine with `paralon_agent`, Vast is
              relisted only once no Paralon rental container runs and the GPU
              is free (v0.5.0).

The host's idle job (`idle_job_images`, machines with `paralon_agent`): it
never runs under a Paralon rental (v0.5.0). While one runs (and no Vast
renter), the idle job is stopped -- a Vast default job through Vast (`vastai
stop instance`; a plain `docker stop` would be undone by Vast's daemon), a
plain container with `docker stop` -- and started again once the rental is
over and the GPU free, in the same pass AFTER the Vast relist. Only what this
tool stopped is started (the `idle_job` list in the state file).

"Took off" flags live in the state file. A flag is saved BEFORE the
unlist/pause call, and cleared only AFTER the relist call succeeds, so a crash
or restart at any point leaves a flag that the next pass resolves.

Before unlisting Vast, the machine's current listing terms (GPU price, min
bid, disk, bandwidth, offer end) are saved with the flag, from the same read.
The relist passes them all back, because `vastai list machine` resets any term
it is not given. Config prices are only a fallback when nothing was saved; if
neither has a GPU price and min bid, the relist holds.
"""

import fcntl
import logging
import threading
import os
import time
from contextlib import contextmanager
from datetime import datetime, timezone

from .agent import (AGENT_IMAGE, AgentError, DockerHost, PARALON_IMAGE_PREFIX, PARALON_NAME_PREFIX,
                    PIN_PREFIX, RENTAL, VAST_NAME_RE, agent_of, leftovers)
from .config import REQUIRED_PRICES
from .paralon import ParalonError
from .state import MAX_DECISIONS, MODES
from .vast import VastError

log = logging.getLogger("vpdual")


class _NotRented:
    rented = False


_NOT_RENTED = _NotRented()


def _needs_the_minute(node):
    """Read once a minute: a node rented, or an ON-DEMAND node waiting online.
    A Paralon rental whose container shows up late is seen only through the
    API, and an on-demand rental cannot be reclaimed if a Vast renter takes
    the GPU meanwhile (v0.4.5). A spot node waiting is read every 2 minutes."""
    if node is None:
        return False
    if node.rented:
        return True
    return bool(node.available) and node.spot is False and not _offline(node)


def _offline(node):
    """The node reads OFFLINE on Paralon (its agent is not connected)."""
    raw = getattr(node, "raw", None) or {}
    return raw.get("online") is False or raw.get("state") == "offline"

ON_DEMAND_WARN_EVERY_S = 3600
FORCED_NODES_EVERY_S = 60     # fresh GET /nodes while a node is rented or an on-demand node waits -- as our fleet does
# BURSTS at critical moments (v0.4.4): after a write that landed, a Vast renter
# or a Paralon container starting on a machine, and while a machine recovers
# after a Vast rental, GET /nodes is read fresh every BURST_EVERY_S. 30 s keeps
# the burst read, a node write and a reclaim inside 4 calls a minute.
BURST_EVERY_S = 30
BURST_S = 240
# After `vastai stop instance` on the host's idle job, do not re-read `show
# instances` for this long while its container winds down (passes can run
# every 5 s in a burst).
IDLE_STOP_WAIT_S = 60


def describe_listing(listing):
    parts = [f"gpu ${listing['price_gpu']:g}" if "price_gpu" in listing else "",
             f"min bid ${listing['price_min_bid']:g}"
             if "price_min_bid" in listing else ""]
    extra = sorted(k for k in listing if k not in REQUIRED_PRICES)
    if extra:
        parts.append("+ " + ", ".join(extra))
    return " ".join(p for p in parts if p) or "no prices"


class Controller:
    def __init__(self, cfg, vast, paralon, store, *, dry_run=False,
                 clock=time.time, docker=DockerHost):
        self.cfg = cfg
        self.docker = docker          # target -> DockerHost (a fake in tests)
        self.vast = vast
        self.paralon = paralon
        self.store = store
        self.dry_run = dry_run
        self.clock = clock
        self.state = store.load([m.vast_machine_id for m in cfg.machines])
        self._dirty = False
        self._last_on_demand_warn = {}
        self._told_no_mode = set()
        self.pass_decisions = []      # (machine id, msg) made this pass
        self.recovering = set()       # machine ids waiting on GPU / agent / node
        self._last_forced_nodes = 0.0
        self.burst_until = 0.0
        self._jupyter_refresh = {}    # machine id -> (last start, running thread)
        self._idle_stop_sent = {}     # machine id -> when `stop instance` was sent

    # -- bookkeeping ---------------------------------------------------
    @property
    def bursting(self):
        return self.clock() < self.burst_until

    def burst(self, why):
        """Read GET /nodes every BURST_EVERY_S for the next BURST_S."""
        if not self.bursting:
            log.info("burst reads for %ds: %s", BURST_S, why)
        self.burst_until = max(self.burst_until, self.clock() + BURST_S)

    def mstate(self, machine):
        return self.state["machines"][str(machine.vast_machine_id)]

    def targets(self, machine_id=None):
        if machine_id is None:
            return list(self.cfg.machines)
        m = self.cfg.machine(machine_id)
        if m is None:
            raise ValueError(
                f"machine {machine_id} is not in the config (configured: "
                + ", ".join(str(x.vast_machine_id) for x in self.cfg.machines)
                + ")")
        return [m]

    def _decide(self, machine, msg, level=logging.INFO):
        prefix = "DRY-RUN would: " if self.dry_run and level == logging.INFO \
            and not msg.startswith(("hold", "ok")) else ""
        log.log(level, "[%s] %s%s", machine.vast_machine_id, prefix, msg)
        self.pass_decisions.append((machine.vast_machine_id, msg))
        now = datetime.fromtimestamp(self.clock(), timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        decisions = self.mstate(machine)["decisions"]
        if decisions and decisions[-1]["msg"] == msg:
            decisions[-1]["t"] = now
            decisions[-1]["count"] = decisions[-1].get("count", 1) + 1
        else:
            decisions.append({"t": now, "msg": msg})
            del decisions[:-MAX_DECISIONS]
        self._dirty = True

    def _save(self):
        if self.dry_run:
            return
        self.store.save(self.state)
        self._dirty = False

    def reload(self):
        """Re-read the state file. The service and a `mode` command run as
        separate processes on one file: without this the service kept the
        state it loaded at start and wrote it back over a mode change."""
        if self.dry_run:
            return                  # a dry run is short-lived; keep its view
        self.state = self.store.load([m.vast_machine_id for m in self.cfg.machines])
        self._dirty = False

    @contextmanager
    def locked(self):
        """One pass at a time across processes (`run` and `mode`), so the two
        never act on the same machine at once. A dry run takes no lock."""
        if self.dry_run:
            yield
            return
        path = self.store.path + ".lock"
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

    def set_mode(self, mode, machine_id=None):
        if mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        with self.locked():
            self.reload()
            for m in self.targets(machine_id):
                self.mstate(m)["mode"] = mode
                self._decide(m, f"mode set to {mode}")
            self._save()

    # -- one pass ------------------------------------------------------
    def read(self):
        """Read both sides once. Returns (vast machines, paralon nodes) or
        raises VastError / ParalonError."""
        vms = self.vast.read_machines()
        fresh = False
        # Once a minute while it matters (a machine recovering, or one of our
        # nodes rented); otherwise every paralon_min_get_interval_s (120 s) --
        # a once-a-minute read all day is ~1440 calls on its own.
        every = None
        if self.recovering or self.bursting:
            every = BURST_EVERY_S                 # a critical moment
        elif any(_needs_the_minute(self.paralon.cached_node(m.paralon_node_id))
                 for m in self.cfg.machines):
            every = FORCED_NODES_EVERY_S
        if every and self.clock() - self._last_forced_nodes >= every:
            fresh, self._last_forced_nodes = True, self.clock()
        nodes = {n.id: n for n in self.paralon.list_nodes(refresh=fresh)}
        return vms, nodes

    def poll(self, machine_id=None):
        with self.locked():
            self.reload()
            return self._poll(machine_id)

    def _poll(self, machine_id):
        targets = self.targets(machine_id)
        self.pass_decisions = []
        active = []
        for m in targets:
            if self.mstate(m)["mode"] in MODES:
                active.append(m)
            elif m.vast_machine_id not in self._told_no_mode:
                self._told_no_mode.add(m.vast_machine_id)   # say it once
                log.error("[%s] no mode set: run `mode vast|paralon|dual` "
                          "first; skipped", m.vast_machine_id)
        if not active:
            return False
        try:
            data = self.read()
        except (VastError, ParalonError) as exc:
            side = "Vast" if isinstance(exc, VastError) else "Paralon"
            for m in active:
                self._decide(m, f"hold: {side} unreadable ({exc})",
                             logging.WARNING)
            self._save()
            return False
        return self.apply(data, active)

    def apply(self, data, machines):
        vms, nodes = data
        ok = True
        for m in machines:
            ok = self._apply_one(m, vms.get(m.vast_machine_id),
                                 nodes.get(m.paralon_node_id)) and ok
        if self._dirty:
            self._save()
        return ok

    def _apply_one(self, m, vm, node):
        st = self.mstate(m)
        mode = st["mode"]
        if mode not in MODES:
            return False
        if vm is None or isinstance(vm, VastError):
            why = vm or "not found in your Vast account"
            self._decide(m, f"hold: Vast machine unreadable ({why})",
                         logging.WARNING)
            return False
        if node is None:
            if st["took_off"]["vast"] and not vm.listed:
                # The node is gone (deleted, or re-created with a new id), so
                # no Paralon rental can be holding the GPU: give Vast back
                # rather than leaving the machine unlisted with nobody owing it.
                self._decide(m, f"Paralon node {m.paralon_node_id} is not in "
                             "GET /nodes; relisting the Vast machine this tool "
                             "unlisted", logging.WARNING)
                return self._side(m, "vast", want_off=False, is_up=vm.listed,
                                  force_up=False, vm=vm)
            self._decide(m, f"hold: Paralon node {m.paralon_node_id} not "
                         "found in GET /nodes (deleted or re-created? fix the "
                         "config)", logging.WARNING)
            return False

        log.debug("[%s] vast listed=%s running=%s resident=%s | paralon "
                  "state=%s available=%s spot=%s", m.vast_machine_id,
                  vm.listed, vm.running, vm.resident, node.state,
                  node.available, node.spot)

        if mode == "dual" and not node.spot:
            self._warn_on_demand(m)

        idle = self.cfg.idle_job_images
        machine = self._read_machine(m) if m.paralon_agent else None
        on_machine = machine.vast_running(idle) if machine else []
        # Vast's count is a hint: a renter's container may still be pulling
        # its image (count 1, no container) or already running before Vast's
        # count moves (container, count 0). Either one means a renter.
        vast_renter = vm.running > 0 or bool(on_machine)
        # Paralon is rented when the API says so OR a Paralon RENTAL container
        # already runs on the machine: the API can lag it by a minute (v0.5.0).
        rentals = machine.paralon_rentals() if machine else []
        paralon_rented = node.rented or bool(rentals)
        if rentals and not node.rented:
            self._decide(m, f"Paralon rental container(s) running ({', '.join(rentals)}) "
                         "though the API shows no rental yet (starting, or tearing "
                         "down): counted as rented until it is gone")
        # An OFFLINE node cannot be rented, whatever `available` says (its agent
        # is stopped while Vast rents): no pause write for it. Our fleet looped
        # on that write all night once the API refused it (v0.4.3).
        paralon_up = node.available and not _offline(node)
        # A STORED Vast instance (stopped, can resume at any moment) keeps
        # Paralon paused, spot or on-demand (v0.5.0; on-demand only before).
        # Unlisting Vast cannot stop a resume, and a resume beside a SPOT
        # rental shares the GPU for the 2-minute reclaim notice.
        stored = max(0, vm.resident - vm.running)
        if stored and node.rented and not vast_renter:
            self._decide(
                m, f"Vast has {stored} stored instance(s) that can resume while "
                "Paralon is RENTED: the rental cannot be paused (no new one "
                "follows it); if an instance resumes, "
                + ("rule 1 reclaims the spot rental (2 minutes' notice)" if node.spot
                   else "this ON-DEMAND rental cannot be reclaimed and both share the GPU"),
                logging.WARNING)
        elif stored and paralon_up and not vast_renter and mode != "vast":
            self._decide(m, f"Vast has {stored} stored instance(s) that can resume: "
                         "pause Paralon (the node stays registered; it is made "
                         "available again once none is stored)")
        policy = self.cfg.on_paralon_rental

        paralon_off = vast_renter or mode == "vast" or stored > 0
        vast_off = mode == "paralon" or (
            mode == "dual" and paralon_rented and not vast_renter
            and policy == "unlist_vast")
        # After a Paralon rental, Vast is relisted only once its container is
        # gone and the GPU is free (v0.5.0); the host's idle job restarts on
        # the same condition, after the relist.
        handback_block = self._handback_block(m, machine, rentals)
        vast_block = (handback_block if self.mstate(m)["took_off"]["vast"]
                      and not node.rented else None)

        # Pause Paralon BEFORE reclaiming, so a reclaimed node is not open to
        # a new rental in the gap between the two calls.
        ok = self._side(m, "paralon", want_off=paralon_off,
                        is_up=paralon_up, force_up=(mode == "paralon"),
                        node=node, block=self._paralon_block(m, machine))
        # Rule 1 (every mode): a Vast renter wins the GPU.
        if vast_renter and node.rented:
            self._reclaim(m, node)
        elif not node.rented and st["reclaim_sent_at"] is not None:
            st["reclaim_sent_at"] = None
            self._dirty = True
        ok = self._side(m, "vast", want_off=vast_off, is_up=vm.listed,
                        force_up=(mode == "vast"), vm=vm, block=vast_block) and ok
        if m.paralon_agent:
            ok = self._agent(m, vast_renter, node, machine) and ok
            # AFTER the Vast relist above: a restarted idle job can never hold
            # the relist back (and gpu_free counts the idle job as free anyway).
            ok = self._idle_job(m, machine, paralon_rented, vast_renter,
                                handback_block) and ok
            self._pin_jupyter(m)
            self._refresh_jupyter(m, machine)
        vast_held = bool(vast_block) and not vast_off and not vm.listed
        self._track_recovery(m, vast_renter, node, vast_held)
        return ok

    # -- the machine ---------------------------------------------------
    def _read_machine(self, m):
        try:
            return self.docker(m.paralon_agent).machine_state()
        except AgentError as exc:
            self._decide(m, f"cannot read the machine ({exc}); nothing is made "
                         "available until it reads", logging.WARNING)
            return None

    def _paralon_block(self, m, machine):
        """Why Paralon must not be made available yet (None: it may). Only for
        a machine this tool can read; others keep the API-only rules."""
        if not m.paralon_agent:
            return None
        if machine is None:
            return "the machine is unreadable"
        on = machine.vast_running(self.cfg.idle_job_images)
        if on:
            return f"Vast container(s) running on the machine: {', '.join(on)}"
        agent = agent_of(machine.containers)
        if agent is None or not agent.running:
            return "the Paralon agent is not running yet"
        return None

    def _handback_block(self, m, machine, rentals):
        """Why the GPU is not yet free to hand back after a Paralon rental (to
        Vast's relist and the host's idle job), or None. Only for a machine
        this tool can read; others keep the API-only rule."""
        if not m.paralon_agent:
            return None
        if machine is None:
            return "the machine is unreadable"
        if rentals:
            return ("the Paralon rental's container is still running on the "
                    f"machine ({', '.join(rentals)})")
        if leftovers(machine.containers):
            # The agent's own benchmark / inference worker: it can hold the GPU
            # for as long as the agent runs, and rule 1 kills it the moment a
            # Vast renter arrives -- waiting on it could hold Vast off forever.
            return None
        if machine.gpu_free(self.cfg.gpu_free_mib, self.cfg.idle_job_images) is not True:
            return f"the Paralon rental has ended but the GPU is not free yet ({machine.gpu_text()})"
        return None

    def _track_recovery(self, m, vast_renter, node, vast_held=False):
        """A machine is recovering from a Vast rental while this tool owes it
        the agent's start or Paralon's relist, or from a Paralon rental while
        a Vast relist waits for the GPU: passes then run every fast_poll_s so
        the machine comes back within seconds, not a minute."""
        flags = self.mstate(m)["took_off"]
        owed = flags["agent"] or (flags["paralon"] and not node.available)
        if m.paralon_agent and (vast_held or (not vast_renter and owed)):
            self.recovering.add(m.vast_machine_id)
        else:
            self.recovering.discard(m.vast_machine_id)

    def container_started(self, target, name, image, paralon_type=""):
        """docker events on `target`: a container started. A non-Paralon one
        (not ours, not the host's idle job) pauses every available Paralon
        node on that machine NOW; the caller wakes the full pass, which
        stops the agent and checks Vast. A Paralon RENTAL container (label
        paralon.type=rental) unlists Vast NOW (dual + unlist_vast) and stops
        the host's idle job NOW (v0.5.0). Returns True if it acted."""
        img, nm = (image or "").lower(), (name or "").lstrip("/")
        ptype = (paralon_type or "").strip()
        if nm.startswith(PIN_PREFIX) or any(img.startswith(i.lower())
                                            for i in self.cfg.idle_job_images):
            return False
        if (ptype or img.startswith(PARALON_IMAGE_PREFIX)
                or nm.lower().startswith(PARALON_NAME_PREFIX)):
            if AGENT_IMAGE in img:
                return False          # the agent restarting is nobody's work
            # a Paralon rental/worker starting: confirm it on the API soon
            self.burst(f"Paralon container {nm} started on {target or 'this machine'}")
            if ptype == RENTAL:
                return self._paralon_rental_started(target, nm)
            return False              # a benchmark / inference worker / download
        self.burst(f"container {nm} started on {target or 'this machine'}")
        acted = False
        with self.locked():
            self.reload()
            for m in self.cfg.machines:
                if m.paralon_agent != target or self.mstate(m)["mode"] not in MODES:
                    continue
                node = self.paralon.cached_node(m.paralon_node_id)
                if node is None or not node.available or node.rented:
                    continue          # unknown, already paused, or rented: the pass decides
                self._decide(m, f"container {nm} started on the machine: pause Paralon now")
                self.mstate(m)["took_off"]["paralon"] = True
                self._save()          # the owed relist is on disk first
                acted = True
                if not self.dry_run:
                    try:
                        self.paralon.set_available(m.paralon_node_id, False)
                    except ParalonError as exc:
                        self._decide(m, f"paralon write failed, the pass retries: {exc}",
                                     logging.WARNING)
            if self._dirty:
                self._save()
        return acted

    def _paralon_rental_started(self, target, nm):
        """A Paralon rental's container started on `target`: before any pass
        sees node.rented (up to a minute later), unlist Vast in dual +
        unlist_vast -- through the pass's own _side, so the flag and listing
        terms are on disk first -- and stop the host's idle job. Nothing when
        a Vast renter already runs (rule 1). The next pass confirms on the
        API; a false alarm relists through the normal relist path."""
        acted = False
        with self.locked():
            self.reload()
            for m in self.cfg.machines:
                mode = self.mstate(m)["mode"]
                if m.paralon_agent != target or mode not in MODES:
                    continue
                unlist = mode == "dual" and self.cfg.on_paralon_rental == "unlist_vast"
                vm = None
                if unlist:
                    try:
                        vm = self.vast.read_machines().get(m.vast_machine_id)
                    except VastError as exc:
                        vm = exc
                    if vm is None or isinstance(vm, VastError):
                        self._decide(m, f"Paralon rental container {nm} started, but "
                                     f"Vast is unreadable ({vm or 'machine not found'}); "
                                     "the pass decides", logging.WARNING)
                        vm, unlist = None, False
                machine = self._read_machine(m)
                vast_renter = ((vm is not None and vm.running > 0)
                               or bool(machine and machine.vast_running(self.cfg.idle_job_images)))
                if vast_renter:
                    self._decide(m, f"Paralon rental container {nm} started while a "
                                 "Vast renter runs: rule 1 decides on the pass")
                    continue
                if unlist and vm.listed:
                    self._decide(m, f"Paralon rental container {nm} started on the "
                                 "machine: unlist Vast now")
                    self._side(m, "vast", want_off=True, is_up=True, force_up=False, vm=vm)
                    acted = True
                if machine is not None and self.cfg.idle_job_images \
                        and machine.idle_jobs(self.cfg.idle_job_images):
                    self._stop_idle_job(m, machine)
                    acted = True
            if self._dirty:
                self._save()
        return acted

    def _pin_jupyter(self, m):
        """Keep Paralon's Jupyter image from Vast's image cleanup. A failure
        is logged and retried next pass; it never holds anything else."""
        if self.dry_run:
            return
        try:
            lines = self.docker(m.paralon_agent).pin_jupyter()
        except AgentError as exc:
            self._decide(m, f"Jupyter pin failed, will retry: {exc}",
                         logging.WARNING)
            return
        changed = [ln for ln in lines if ln.startswith(("pinned", "removed"))]
        if changed:
            self._decide(m, "Paralon's Jupyter image: " + "; ".join(changed))

    def _refresh_jupyter(self, m, machine):
        """While the agent is stopped, pull Paralon's Jupyter image once every
        jupyter_refresh_s on a background thread (a changed image is ~9 GB;
        the pass never waits for it). The next pass's pin keeps it."""
        if self.dry_run or machine is None:
            return
        agent = agent_of(machine.containers)
        if agent is not None and agent.running:
            return                    # the agent pulls its own image
        last, thread = self._jupyter_refresh.get(m.vast_machine_id, (None, None))
        if thread is not None and thread.is_alive():
            return
        now = self.clock()
        if last is None:
            # first sight of a stopped agent: wait a few minutes so a Vast
            # hand-over settles before any pull
            self._jupyter_refresh[m.vast_machine_id] = (now - self.cfg.jupyter_refresh_s + 300, None)
            return
        if now - last < self.cfg.jupyter_refresh_s:
            return
        host = self.docker(m.paralon_agent)

        def pull():
            try:
                image_id = host.refresh_jupyter()
                log.info("machine %s: Paralon's Jupyter image checked (%s)",
                         m.vast_machine_id, image_id[:19])
            except AgentError as exc:
                log.warning("machine %s: Jupyter refresh failed, retried later: %s",
                            m.vast_machine_id, exc)

        t = threading.Thread(target=pull, name=f"jupyter-{m.vast_machine_id}", daemon=True)
        self._jupyter_refresh[m.vast_machine_id] = (now, t)
        t.start()

    def _agent(self, m, vast_renter, node, machine=None):
        """Rule 1, agent half: the Paralon agent is off while Vast rents."""
        st = self.mstate(m)
        flags = st["took_off"]
        if not vast_renter and not flags["agent"]:
            return True               # nothing to stop, no start owed
        if machine is not None:
            containers = machine.containers
        else:
            try:
                containers = self.docker(m.paralon_agent).containers()
            except AgentError as exc:
                self._decide(m, f"hold: cannot read Docker for the Paralon agent "
                             f"({exc})", logging.WARNING)
                return False
        agent = agent_of(containers)
        if agent is None:
            self._decide(m, f"no Paralon agent container on {m.paralon_agent}; "
                         "nothing to stop or start", logging.WARNING)
            return True
        if vast_renter:
            if node.rented:
                if agent.running:
                    self._decide(m, "Paralon agent stays up until the Paralon "
                                 "rental ends (it runs the reclaim)")
                return True
            extra = leftovers(containers)
            names = ", ".join(c.name for c in extra)
            if agent.running:
                self._decide(m, "Vast renter running: stop the Paralon agent"
                             + (f", then {names}" if extra else ""))
                if not flags["agent"]:
                    flags["agent"] = True
                    self._save()      # the owed start is on disk first
                steps = [("stop", [agent.id])] + ([("kill", [c.id for c in extra])]
                                                  if extra else [])
            elif extra:
                self._decide(m, f"Vast renter running: stop Paralon's "
                             f"containers ({names})")
                steps = [("kill", [c.id for c in extra])]
            else:
                return True
            # Agent first (2 s grace), so it is gone before the containers it
            # watches die; then the leftovers are KILLED at once -- the
            # inference worker ignores SIGTERM and holds ~20 GB of the
            # renter's GPU memory for as long as a stop would wait.
            return all(self._docker_write(m, verb, group) for verb, group in steps)
        if not flags["agent"]:
            return True               # someone else stopped it: leave it
        if agent.running:
            flags["agent"] = False    # confirmed back
            self._decide(m, "Paralon agent is running again; flag cleared")
            return True
        free = machine.gpu_free(self.cfg.gpu_free_mib, self.cfg.idle_job_images) \
            if machine is not None else None
        if free is not True:
            self._decide(m, "Vast renter gone; waiting for the GPU to be free "
                         f"before starting the Paralon agent ({machine.gpu_text() if machine else 'machine unread'})")
            return True
        self._decide(m, "no Vast renter and the GPU is free: start the Paralon agent")
        return self._docker_write(m, "start", agent.id)

    def _docker_write(self, m, verb, arg, what="Paralon agent"):
        if self.dry_run:
            return True
        try:
            host = self.docker(m.paralon_agent)
            {"stop": host.stop, "kill": host.kill, "start": host.start}[verb](arg)
        except AgentError as exc:
            self._decide(m, f"{what} {verb} failed, will retry: {exc}",
                         logging.WARNING)
            return False
        return True

    # -- the host's idle job (v0.5.0) ------------------------------------
    def _idle_job(self, m, machine, paralon_rented, vast_renter, handback_block):
        """The host's idle job never runs under a Paralon rental: stopped while
        one runs (and no Vast renter), started again -- only what this tool
        stopped -- once the rental is over and the GPU is free."""
        if not self.cfg.idle_job_images or machine is None:
            return True
        if paralon_rented and not vast_renter:
            return self._stop_idle_job(m, machine)
        if not self.mstate(m)["idle_job"] or paralon_rented or vast_renter:
            return True               # nothing owed, or the GPU is not ours to give
        if handback_block:
            self._decide(m, f"hold: not starting the host's idle job yet -- {handback_block}")
            return True
        return self._start_idle_job(m, machine)

    def _idle_instance(self, m, inst):
        """An instance on the host's account that is this machine's idle job:
        on this machine_id, image_uuid under an idle_job_images prefix."""
        try:
            on_machine = int(inst.get("machine_id") or 0) == m.vast_machine_id
        except (TypeError, ValueError):
            return False
        image = str(inst.get("image_uuid") or "").lower()
        return on_machine and any(image.startswith(i.lower()) for i in self.cfg.idle_job_images)

    def _owe_idle(self, m, kind, ident, name):
        owed = self.mstate(m)["idle_job"]
        if not any(j["kind"] == kind and j["id"] == ident for j in owed):
            owed.append({"kind": kind, "id": ident, "name": name})
        self._save()                  # the owed start is on disk before the stop

    def _stop_idle_job(self, m, machine):
        running = machine.idle_jobs(self.cfg.idle_job_images)
        ok = True
        if any(VAST_NAME_RE.match(c.name) for c in running):
            ok = self._stop_vast_idle_job(m, [c.name for c in running
                                              if VAST_NAME_RE.match(c.name)])
        for c in running:
            if VAST_NAME_RE.match(c.name):
                continue
            self._decide(m, f"Paralon rental running: stop the host's idle job "
                         f"(container {c.name})")
            self._owe_idle(m, "docker", c.id, c.name)
            ok = self._docker_write(m, "stop", [c.id], what="idle job") and ok
        return ok

    def _stop_vast_idle_job(self, m, names):
        """A Vast DEFAULT JOB: stopped through Vast. `docker stop` is not
        enough -- Vast's daemon restarts a job it believes should run."""
        now = self.clock()
        sent = self._idle_stop_sent.get(m.vast_machine_id)
        if sent is not None and now - sent < IDLE_STOP_WAIT_S:
            return True               # Vast is stopping it; give it a minute
        try:
            insts = self.vast.read_instances()
        except VastError as exc:
            self._decide(m, "cannot read Vast instances to stop the host's idle "
                         f"job ({', '.join(names)}): {exc}; retried next pass",
                         logging.WARNING)
            return False
        jobs = [i for i in insts if self._idle_instance(m, i)]
        if not jobs:
            self._decide(
                m, f"the host's idle job ({', '.join(names)}) is a Vast instance, but "
                "`vastai show instances` lists none on this machine with an "
                "idle_job_images image: cannot stop it, and the Paralon renter "
                "shares the GPU with it", logging.WARNING)
            return False
        to_stop = [i for i in jobs if i.get("intended_status") == "running"]
        if not to_stop:
            self._decide(m, "waiting for Vast to stop the host's idle job (instance "
                         + ", ".join(str(i["id"]) for i in jobs)
                         + f" is {jobs[0].get('intended_status')} on Vast, its "
                         "container still runs)")
            return True
        ok = True
        for inst in to_stop:
            iid = int(inst["id"])
            self._decide(m, f"Paralon rental running: stop the host's idle job "
                         f"(Vast instance {iid}) through Vast")
            self._owe_idle(m, "vast", iid, f"C.{iid}")
            if self.dry_run:
                continue
            try:
                self.vast.stop_instance(iid)
            except VastError as exc:
                self._decide(m, f"idle job stop failed, will retry: {exc}",
                             logging.WARNING)
                ok = False
                continue
            self._idle_stop_sent[m.vast_machine_id] = now
        return ok

    def _start_idle_job(self, m, machine):
        owed = self.mstate(m)["idle_job"]
        keep, ok = [], True
        if any(j["kind"] == "vast" for j in owed):
            try:
                insts = {int(i["id"]): i for i in self.vast.read_instances()}
            except (VastError, TypeError, ValueError) as exc:
                self._decide(m, "cannot read Vast instances to start the host's "
                             f"idle job: {exc}; retried next pass", logging.WARNING)
                return False
        for j in owed:
            if j["kind"] == "vast":
                inst = insts.get(int(j["id"]))
                status = None if inst is None else inst.get("intended_status")
                if inst is None:
                    self._decide(m, f"the host's idle job (Vast instance {j['id']}) "
                                 "no longer exists; nothing to start", logging.WARNING)
                elif status == "running":
                    self._decide(m, f"the host's idle job (Vast instance {j['id']}) "
                                 "is running again; flag cleared")
                else:
                    keep.append(j)
                    self._decide(m, "Paralon rental over and the GPU is free: start "
                                 f"the host's idle job (Vast instance {j['id']})")
                    if not self.dry_run:
                        try:
                            self.vast.start_instance(int(j["id"]))
                        except VastError as exc:
                            self._decide(m, f"idle job start failed, will retry: {exc}",
                                         logging.WARNING)
                            ok = False
                continue
            c = next((c for c in machine.containers if c.id == j["id"]), None)
            if c is None:
                self._decide(m, f"the host's idle job container {j.get('name') or j['id']} "
                             "no longer exists; nothing to start", logging.WARNING)
            elif c.running:
                self._decide(m, f"the host's idle job ({c.name}) is running again; "
                             "flag cleared")
            else:
                keep.append(j)
                self._decide(m, "Paralon rental over and the GPU is free: start "
                             f"the host's idle job (container {c.name})")
                ok = self._docker_write(m, "start", c.id, what="idle job") and ok
        if keep != owed:
            self.mstate(m)["idle_job"] = keep
            self._dirty = True
        return ok

    # -- helpers -------------------------------------------------------
    def _warn_on_demand(self, m):
        now = self.clock()
        last = self._last_on_demand_warn.get(m.vast_machine_id)
        if last is None or now - last >= ON_DEMAND_WARN_EVERY_S:
            self._last_on_demand_warn[m.vast_machine_id] = now
            log.warning(
                "!!! [%s] Paralon node %s is ON-DEMAND, not spot. In dual mode "
                "an on-demand rental CANNOT be reclaimed when a Vast renter "
                "arrives, so both renters would share the GPU. Switch the "
                "node to spot in the Paralon dashboard. !!!",
                m.vast_machine_id, m.paralon_node_id)

    def _reclaim(self, m, node):
        st = self.mstate(m)
        if not node.spot:
            self._decide(
                m, "Vast renter running while an ON-DEMAND Paralon rental is "
                "active: cannot reclaim; both share the GPU until one ends",
                logging.WARNING)
            return
        sent = st["reclaim_sent_at"]
        if sent is not None and self.clock() - sent < self.cfg.reclaim_retry_s:
            return  # already asked; the renter has 2 minutes' notice
        self._decide(m, "Vast renter running: reclaim Paralon spot rental")
        if self.dry_run:
            return
        try:
            self.paralon.reclaim(m.paralon_node_id)
        except ParalonError as exc:
            self._decide(m, f"reclaim failed, will retry: {exc}",
                         logging.WARNING)
            return
        st["reclaim_sent_at"] = self.clock()
        self._dirty = True
        self.burst("reclaim sent -- confirm it")

    def relist_listing(self, m):
        """Listing terms for a relist: config fallbacks overlaid with the
        terms saved at take-off. None if a required price is missing."""
        listing = dict(m.vast_listing)
        saved = dict(self.mstate(m)["saved_listing"] or {})
        end = saved.pop("end_date", None)
        rolling = saved.pop("duration", None)
        listing.update(saved)
        # ONE offer end, never two (the CLI refuses both). A machine listed
        # with a rolling `duration` must come back rolling: Vast reports it
        # as `duration` seconds, and relisting with the absolute end_date
        # instead would turn a "14 days, extended daily" offer into one that
        # expires on a fixed date (seen on the first live test).
        if isinstance(rolling, (int, float)) and rolling > 0:
            listing["duration"] = int(rolling)
            listing.pop("end_date", None)
        elif end is not None and end > self.clock():
            listing["end_date"] = end
            listing.pop("duration", None)
        if any(not isinstance(listing.get(k), (int, float))
               for k in REQUIRED_PRICES):
            return None
        return listing

    def _side(self, m, side, *, want_off, is_up, force_up, vm=None, node=None,
              block=None):
        st = self.mstate(m)
        flags = st["took_off"]
        if not want_off and not is_up and block and (flags[side] or force_up):
            what = "making Paralon available" if side == "paralon" else "relisting Vast"
            self._decide(m, f"hold: not {what} yet -- {block}")
            return True
        if (side == "paralon" and not want_off and not is_up and node is not None
                and node.raw.get("online") is False):
            # Paralon cannot rent an offline node whatever `available` says,
            # and PATCHing it every pass only spends the call budget.
            self._decide(m, "hold: Paralon node is OFFLINE (its agent is not "
                         "connected on the machine); not making it available",
                         logging.WARNING)
            return False
        if want_off:
            if is_up:
                msg = f"take {side} off"
                if side == "vast":
                    terms = vm.listing_terms
                    st["saved_listing"] = terms or None
                    msg += (f" (saved its listing for the relist: "
                            f"{describe_listing(terms)})" if terms else
                            " (Vast reported no listing terms to save; the "
                            "relist will use the config prices)")
                self._decide(m, msg)
                flags[side] = True
                self._save()          # flag + saved terms on disk first
                return self._write(m, side, up=False)
            return True               # already off (by us or by the host)
        if not is_up and (flags[side] or force_up):
            listing = None
            if side == "vast":
                listing = self.relist_listing(m)
                if listing is None:
                    self._decide(
                        m, "hold: cannot relist Vast: no saved price (this "
                        "tool did not unlist it) and no price_gpu + "
                        "price_min_bid in the config vast_listing. Add them "
                        "to the config, or list it by hand.", logging.WARNING)
                    return False
                self._decide(m, f"put vast back ({describe_listing(listing)})")
            else:
                self._decide(m, f"put {side} back")
            if not self._write(m, side, up=True, listing=listing):
                return False
            # The flag and saved terms stay until the NEXT read shows the
            # side back up: a write that returned cleanly is not proof (the
            # vastai CLI exits 0 on a refused list). A force_up relist of a
            # side the host had taken down is ours to confirm from here.
            if not flags[side]:
                flags[side] = True
                self._save()
            return True
        if is_up and flags[side]:
            flags[side] = False       # confirmed back (by us, or by the host)
            if side == "vast":
                st["saved_listing"] = None
            self._decide(m, f"{side} is back; flag cleared")
        return True

    def _write(self, m, side, *, up, listing=None):
        if self.dry_run:
            return True
        try:
            if side == "paralon":
                self.paralon.set_available(m.paralon_node_id, up)
            elif up:
                self.vast.list_machine(m.vast_machine_id, listing)
            else:
                self.vast.unlist_machine(m.vast_machine_id)
        except (ParalonError, VastError) as exc:
            self._decide(m, f"{side} write failed, will retry: {exc}",
                         logging.WARNING)
            return False
        self.burst(f"{side} {'listed' if up else 'taken off'} -- confirm it")
        return True
