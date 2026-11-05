"""Command line: init | doctor | arm | status | mode | run | ui | service."""

import argparse
import logging
import os
import shutil
import signal
import sys
import threading
import time

from . import __version__, doctor
from . import settings
from .config import ConfigError, config_path, load_config
from .docker_events import start_watcher
from .engine import Controller
from .install import add_init_args, render_service, run_init
from .paralon import ParalonClient
from .state import MODES, StateStore
from .status import publish_snapshot, status_data, status_lines
from .vast import VastCLI
from .webui import DEFAULT_HOST, DEFAULT_PORT, PASSWORD_ENV, serve

log = logging.getLogger("vpdual")

MIN_EVENT_GAP_S = 2    # an event-triggered pass at most every 2 s


def clients(cfg):
    """(vastai CLI, Paralon client) for this config."""
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
    return vast, paralon


def build(cfg, dry_run):
    vast, paralon = clients(cfg)
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
    if not ctl.armed:
        log.warning("NOT ARMED: every decision is logged as \"NOT ARMED: "
                    "would ...\" and nothing is changed on Vast, Paralon or "
                    "Docker. Arm it with `arm` (or Settings in the web UI) "
                    "and restart.")
    last = 0.0
    try:
        while not stop.is_set():
            last = time.monotonic()
            ok = ctl.poll()
            publish_status(ctl, cfg, ok)
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


def publish_status(ctl, cfg, ok):
    """v0.7.0: after every pass (acted or held), the status snapshot the web
    UI serves (status.publish_snapshot), so an open page costs no read of its
    own. A dry run writes nothing. A failure here never stops the service."""
    if ctl.dry_run:
        return
    try:
        publish_snapshot(ctl, cfg, ok, ctl.clock)
    except Exception as exc:          # the snapshot is a convenience
        log.warning("could not write the status snapshot for the web UI: %s",
                    exc)


def cmd_doctor(cfg_path, out=print, **kw):
    """v0.8.0: every check as a PASS/WARN/FAIL row; exit 1 on any FAIL."""
    rows = doctor.run_checks(cfg_path, **kw)
    for line in doctor.lines(rows):
        out(line)
    return doctor.exit_code(rows)


def startup_checks(cfg_path, **kw):
    """v0.8.0: `run` makes doctor's checks first and refuses to start on a
    FAIL in doctor.BLOCKING (exit 2; --skip-doctor bypasses). Under systemd
    the refusal repeats each restart, which is one GET /nodes per 30 s."""
    rows = doctor.run_checks(cfg_path, **kw)
    for line in doctor.lines(rows):
        log.info("doctor: %s", line)
    bad = doctor.blocking(rows)
    for r in bad:
        log.error("not starting: %s / %s: %s (fix: %s)", r.group, r.name,
                  r.detail, r.hint)
    if bad:
        log.error("fix the above, or start with --skip-doctor")
    return not bad


def cmd_arm(cfg_path, skip_doctor=False, out=print, **kw):
    """v0.8.0: set "armed": true in the config (backed up first, as a
    Settings save). Doctor runs first; a blocking FAIL refuses (exit 2) --
    `run` would refuse to start anyway. The service reads the config at
    start, so it is armed from its next restart."""
    try:
        raw, _ = settings.read_raw(cfg_path)
    except (OSError, ValueError) as exc:
        out(f"error: cannot read {cfg_path}: {exc}")
        return 2
    if raw.get("armed", True) is True:          # absent = armed (v0.8.0)
        out("already armed; nothing changed")
        return 0
    if not skip_doctor:
        rows = doctor.run_checks(cfg_path, **kw)
        for line in doctor.lines(rows):
            out(line)
        if doctor.blocking(rows):
            out("not armed: fix the FAIL rows above first (or --skip-doctor)")
            return 2
    raw["armed"] = True
    try:
        dest = settings.save(cfg_path, raw)
    except OSError as exc:
        out(f"error: could not save {cfg_path}: {exc}")
        return 2
    out(f"armed (old config kept as {os.path.basename(dest)}). Restart the "
        f"service to apply: {settings.RESTART_COMMAND}")
    return 0


def cmd_mode(ctl, mode, machine_id):
    ctl.set_mode(mode, machine_id)
    if ctl.dry_run:
        log.info("dry-run: mode not saved; showing what one pass would do")
    ctl.poll(machine_id)
    return 0


def cmd_status(ctl, cfg, machine_id, out=print):
    """Both sides, owed relists, and what one pass would do. Writes nothing.
    The same data the web UI serves as GET /api/status."""
    for line in status_lines(status_data(ctl, cfg, machine_id)):
        out(line)
    return 0


def cmd_ui(ctl, cfg, host, port, cfg_path=None, no_login=False):
    return serve(cfg, ctl.vast, ctl.paralon, host=host, port=port,
                 dry_run=ctl.dry_run, docker=ctl.docker, config_path=cfg_path,
                 rebuild=clients, no_login=no_login,
                 password=os.environ.get(PASSWORD_ENV) or None)


def build_parser():
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
    r = sub.add_parser("run", help="watch both sides and apply the rules")
    r.add_argument("--skip-doctor", action="store_true",
                   help="start even if a start-up check fails (v0.8.0)")
    sub.add_parser("doctor", help="check the install: vastai, the key, the "
                                  "config, ids, Docker, state (read-only)")
    a = sub.add_parser("arm", help="let `run` make changes (the config's "
                                   "\"armed\": true); runs doctor first")
    a.add_argument("--skip-doctor", action="store_true",
                   help="arm even if a check fails")
    u = sub.add_parser("ui", help="a local web page: status, and a mode "
                                  "switch per machine")
    u.add_argument("--host", default=DEFAULT_HOST,
                   help=f"address to listen on (default {DEFAULT_HOST}: "
                        "this machine only)")
    u.add_argument("--port", type=int, default=DEFAULT_PORT,
                   help=f"port (default {DEFAULT_PORT})")
    u.add_argument("--no-login", action="store_true",
                   help=f"allow a non-loopback --host without {PASSWORD_ENV} "
                        "(anyone who can reach it can switch modes)")
    sub.add_parser("service", help="print the systemd unit for this account")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)

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
    if args.cmd == "doctor":
        return cmd_doctor(cfg_path)
    if args.cmd == "arm":
        return cmd_arm(cfg_path, args.skip_doctor)
    try:
        cfg = load_config(cfg_path)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.cmd == "run" and not args.skip_doctor \
            and not startup_checks(cfg_path):
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
        if args.cmd == "ui":
            return cmd_ui(ctl, cfg, args.host, args.port, cfg_path,
                          no_login=args.no_login)
        return cmd_status(ctl, cfg, args.machine)
    except ValueError as exc:      # unknown machine id
        print(f"error: {exc}", file=sys.stderr)
        return 2
