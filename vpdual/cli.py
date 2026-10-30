"""Command line: init | status | mode | run | service."""

import argparse
import logging
import os
import shutil
import signal
import sys
import threading
import time

from . import __version__
from .agent import AgentError, agent_of
from .config import ConfigError, config_path, load_config
from .docker_events import start_watcher
from .engine import Controller, describe_listing
from .install import add_init_args, render_service, run_init
from .paralon import ParalonClient, ParalonError
from .state import MODES, StateStore, owes_something
from .vast import VastCLI, VastError

log = logging.getLogger("vpdual")

MIN_EVENT_GAP_S = 2    # an event-triggered pass at most every 2 s


def build(cfg, dry_run):
    binary = shutil.which(cfg.vastai_bin) or cfg.vastai_bin
    if binary == cfg.vastai_bin and not os.path.isabs(binary):
        log.warning("`%s` is not on this process's PATH (under systemd the PATH "
                    "is minimal); set \"vastai_bin\" in the config to its full "
                    "path, e.g. ~/.local/bin/vastai", cfg.vastai_bin)
    vast = VastCLI(binary)
    paralon = ParalonClient(
        cfg.paralon_base_url, cfg.paralon_api_key,
        min_get_interval_s=cfg.paralon_min_get_interval_s,
        max_calls_per_min=cfg.paralon_max_calls_per_min)
    return Controller(cfg, vast, paralon, StateStore(cfg.state_file),
                      dry_run=dry_run)


def cmd_run(ctl, cfg):
    stop = threading.Event()
    wake = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: (stop.set(), wake.set()))
    moded = [m for m in cfg.machines if ctl.mstate(m)["mode"] in MODES]
    if not moded:
        # Keep running: every pass re-reads the state file, so a `mode ...`
        # run later is picked up. Exiting would make systemd restart us
        # every 30 s with the same error.
        log.warning("no mode set yet for any machine: nothing will be done "
                    "until you run `mode vast|paralon|dual`")
    if cfg.docker_events:
        targets = sorted({m.paralon_agent for m in cfg.machines if m.paralon_agent})
        for t in targets:
            start_watcher(wake, target=t,
                          on_start=lambda name, image, ptype="", t=t:
                          ctl.container_started(t, name, image, ptype))
        if not targets:
            start_watcher(wake)      # no machine reachable: wake-only, local
    for m in cfg.machines:
        log.info("watching Vast machine %s + Paralon node %s (mode %s)",
                 m.vast_machine_id, m.paralon_node_id,
                 ctl.mstate(m)["mode"] or "NOT SET - skipped")
    log.info("policy %s, every %ss%s", cfg.on_paralon_rental,
             cfg.poll_interval_s, ", dry-run" if ctl.dry_run else "")
    last = 0.0
    try:
        while not stop.is_set():
            last = time.monotonic()
            ctl.poll()
            # A machine recovering after a Vast rental is checked every
            # fast_poll_s until its GPU is free, its agent up and its node
            # back on Paralon.
            interval = (cfg.fast_poll_s if ctl.recovering or ctl.bursting
                        else cfg.poll_interval_s)
            woke = wake.wait(interval)
            wake.clear()
            if woke and not stop.is_set():
                gap = MIN_EVENT_GAP_S - (time.monotonic() - last)
                if gap > 0:
                    stop.wait(gap)
    except KeyboardInterrupt:
        pass
    log.info("stopped")
    return 0


def cmd_mode(ctl, mode, machine_id):
    ctl.set_mode(mode, machine_id)
    if ctl.dry_run:
        log.info("dry-run: mode not saved; showing what one pass would do")
    ctl.poll(machine_id)
    return 0


def cmd_status(ctl, cfg, machine_id, out=print):
    """Both sides, owed relists, and what one pass would do. Writes nothing."""
    ctl.dry_run = True
    targets = ctl.targets(machine_id)
    history = {m.vast_machine_id: list(ctl.mstate(m)["decisions"][-5:])
               for m in targets}
    out(f"policy: {cfg.on_paralon_rental}")
    try:
        data = ctl.read()
    except (VastError, ParalonError) as exc:
        data = None
        out(f"cannot read both sides, the watcher would hold: {exc}")
    for m in targets:
        st = ctl.mstate(m)
        out(f"\nVast {m.vast_machine_id} <-> Paralon {m.paralon_node_id}")
        out(f"  mode: {st['mode'] or '(not set: run `mode dual`)'}")
        out(f"  took off (owed): vast={st['took_off']['vast']} "
            f"paralon={st['took_off']['paralon']} "
            f"agent stopped={st['took_off']['agent']}"
            + (f" idle job stopped={', '.join(str(j.get('name') or j['id']) for j in st['idle_job'])}"
               if st.get("idle_job") else ""))
        if m.paralon_agent:
            try:
                agent = agent_of(ctl.docker(m.paralon_agent).containers())
                seen = ("not found" if agent is None else
                        "running" if agent.running else "stopped")
            except (AgentError, ValueError) as exc:
                seen = f"unreadable ({exc})"
            out(f"  Paralon agent via {m.paralon_agent}: {seen}")
            try:
                ms = ctl.docker(m.paralon_agent).machine_state()
                vast_on = ms.vast_running(cfg.idle_job_images)
                rentals = ms.paralon_rentals()
                out("  machine: Vast containers running: "
                    + (", ".join(vast_on) if vast_on else "none")
                    + "; Paralon rental containers: "
                    + (", ".join(rentals) if rentals else "none")
                    + f"; {ms.gpu_text()}")
            except (AgentError, ValueError) as exc:
                out(f"  machine: unreadable ({exc})")
        else:
            out("  Paralon agent: not managed (no paralon_agent in the config)")
        relist = ctl.relist_listing(m)
        if st["saved_listing"]:
            out(f"  saved listing: {describe_listing(st['saved_listing'])}")
        out("  relist would use: " + (describe_listing(relist) if relist else
            "NOTHING (no saved price, no config price: a relist would hold)"))
        if data is None:
            continue
        vm = data[0].get(m.vast_machine_id)
        if vm is None or isinstance(vm, VastError):
            out(f"  Vast: unreadable ({vm or 'not in your account'})")
        else:
            out(f"  Vast: listed={vm.listed} running={vm.running} "
                f"resident={vm.resident} "
                f"now {describe_listing(vm.listing_terms)}")
        n = data[1].get(m.paralon_node_id)
        if n is None:
            out("  Paralon: node not found in GET /nodes")
        else:
            kind = "spot" if n.spot else "ON-DEMAND"
            out(f"  Paralon: state={n.state} available={n.available} {kind} "
                f"price={n.price_text} rented={n.rented}")
            if st["mode"] == "dual" and not n.spot:
                out("  WARNING: node is on-demand; dual mode needs spot to "
                    "reclaim")
    if data is not None:
        level = log.level
        log.setLevel(logging.CRITICAL)   # print the decisions below instead
        try:
            ctl.apply(data, [m for m in targets
                             if ctl.mstate(m)["mode"] in MODES])
        finally:
            log.setLevel(level)
        out("\nnext pass would:")
        for mid, msg in ctl.pass_decisions or [("", "nothing to do")]:
            out(f"  [{mid}] {msg}" if mid else f"  {msg}")
    for mid, st in ctl.state["machines"].items():
        if cfg.machine(mid) is None and owes_something(st):
            out(f"\nWARNING: machine {mid} is no longer in the config but the "
                f"tool still owes it a relist: {st['took_off']}")
    out("\nlast decisions:")
    for m in targets:
        for d in history[m.vast_machine_id]:
            count = f" (x{d['count']})" if d.get("count") else ""
            out(f"  {d['t']}  [{m.vast_machine_id}] {d['msg']}{count}")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="vast_paralon_dual",
        description="List GPU machines on Vast.ai and Paralon Cloud at once, "
                    "or switch fully between them.")
    p.add_argument("-c", "--config", default=None,
                   help="config file (default "
                        "~/.config/vast-paralon-dual/config.json)")
    p.add_argument("--dry-run", action="store_true",
                   help="log what would be done; write nothing")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="debug logging (each pass's readings)")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)
    add_init_args(sub.add_parser(
        "init", help="set up: pair Vast machines with Paralon nodes"))
    s = sub.add_parser("status", help="both sides and what a pass would do "
                                      "(writes nothing)")
    s.add_argument("machine", nargs="?", type=int, help="one Vast machine id")
    m = sub.add_parser("mode", help="switch: vast only, paralon only, or dual")
    m.add_argument("mode", choices=MODES)
    m.add_argument("machine", nargs="?", type=int,
                   help="one Vast machine id (default: all)")
    sub.add_parser("run", help="watch both sides and apply the rules")
    sub.add_parser("service", help="print the systemd unit for this account")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s")
    cfg_path = config_path(args.config)
    if args.cmd == "init":
        args.config = cfg_path
        return run_init(args)
    if args.cmd == "service":
        sys.stdout.write(render_service(cfg_path))
        return 0
    try:
        cfg = load_config(cfg_path)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    try:
        ctl = build(cfg, args.dry_run)
    except (OSError, ValueError) as exc:
        print(f"error: cannot read state file {cfg.state_file}: {exc}",
              file=sys.stderr)
        return 2
    try:
        if args.cmd == "run":
            return cmd_run(ctl, cfg)
        if args.cmd == "mode":
            return cmd_mode(ctl, args.mode, args.machine)
        return cmd_status(ctl, cfg, args.machine)
    except ValueError as exc:      # unknown machine id
        print(f"error: {exc}", file=sys.stderr)
        return 2
