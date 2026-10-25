"""Command line: run | mode vast|paralon|dual | status."""

import argparse
import logging
import signal
import sys
import threading
import time

from . import __version__
from .config import ConfigError, load_config
from .docker_events import start_watcher
from .engine import Controller
from .paralon import ParalonClient, ParalonError
from .state import MODES, StateStore
from .vast import VastCLI, VastError

log = logging.getLogger("vpdual")

MIN_EVENT_GAP_S = 15   # an event-triggered pass at most every 15 s


def build(cfg, dry_run):
    vast = VastCLI(cfg.vastai_bin)
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
    if cfg.docker_events:
        start_watcher(wake)
    if ctl.state["mode"] not in MODES:
        log.error("no mode set: run `mode vast|paralon|dual` once first")
        return 2
    log.info("watching Vast machine %s and Paralon node %s (mode %s, %s%s)",
             cfg.vast_machine_id, cfg.paralon_node_id, ctl.state["mode"],
             cfg.on_paralon_rental, ", dry-run" if ctl.dry_run else "")
    last = 0.0
    try:
        while not stop.is_set():
            last = time.monotonic()
            ctl.poll()
            woke = wake.wait(cfg.poll_interval_s)
            wake.clear()
            if woke and not stop.is_set():
                gap = MIN_EVENT_GAP_S - (time.monotonic() - last)
                if gap > 0:
                    stop.wait(gap)
    except KeyboardInterrupt:
        pass
    log.info("stopped")
    return 0


def cmd_mode(ctl, mode):
    ctl.set_mode(mode)
    if ctl.dry_run:
        log.info("dry-run: mode not saved; showing what one pass would do")
    ctl.poll()
    return 0


def cmd_status(ctl, cfg):
    st = ctl.state
    print(f"mode:               {st['mode'] or '(not set)'}")
    print(f"on_paralon_rental:  {cfg.on_paralon_rental}")
    print(f"took off (owed):    vast={st['took_off']['vast']} "
          f"paralon={st['took_off']['paralon']}")
    try:
        vm = ctl.vast.read_machine(cfg.vast_machine_id)
        print(f"Vast {vm.id}:  listed={vm.listed} running={vm.running} "
              f"resident={vm.resident}")
    except VastError as exc:
        print(f"Vast:     unreadable ({exc})")
    try:
        n = ctl.paralon.get_node(cfg.paralon_node_id)
        kind = "spot" if n.spot else "ON-DEMAND"
        print(f"Paralon {n.id}:  state={n.state} available={n.available} "
              f"{kind} price={n.price} rented={n.rented}")
        if st["mode"] == "dual" and not n.spot:
            print("WARNING: node is on-demand; dual mode needs spot to reclaim")
    except ParalonError as exc:
        print(f"Paralon:  unreadable ({exc})")
    print("last decisions:")
    for d in st["decisions"][-10:] or [{"t": "", "msg": "(none)"}]:
        count = f" (x{d['count']})" if d.get("count") else ""
        print(f"  {d['t']}  {d['msg']}{count}")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="vast_paralon_dual",
        description="List one GPU machine on Vast.ai and Paralon Cloud, "
                    "or switch fully between them.")
    p.add_argument("-c", "--config", default="/etc/vast-paralon-dual/config.json")
    p.add_argument("--dry-run", action="store_true",
                   help="log what would be done; write nothing")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="watch both sides and apply the rules")
    m = sub.add_parser("mode", help="switch: vast only, paralon only, or dual")
    m.add_argument("mode", choices=MODES)
    sub.add_parser("status", help="show both sides and the last decisions")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s")
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    try:
        ctl = build(cfg, args.dry_run)
    except (OSError, ValueError) as exc:
        print(f"error: cannot read state file {cfg.state_file}: {exc}",
              file=sys.stderr)
        return 2
    if args.cmd == "run":
        return cmd_run(ctl, cfg)
    if args.cmd == "mode":
        return cmd_mode(ctl, args.mode)
    return cmd_status(ctl, cfg)
