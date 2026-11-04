"""`ui`: a small local web page -- each machine's Vast and Paralon side, what
the next pass would do, the last decisions, a mode switch per machine, and a
Settings tab that edits the config file (settings.py: checked by the config's
own validator, backed up, written atomically; never the key).

Standard library only. The page is inline HTML/CSS/JS served by this module
(no CDN, no web fonts), so it works on a LAN with no internet.

Reading: GET /api/status is `status_data()`, the data `status` prints: a dry
run that writes nothing and never forces a fresh GET /nodes (the service owns
those). One read serves every request for STATUS_MAX_AGE_S, so several open
pages cost one `vastai show machines` and no extra Paralon calls.

Switching: POST /api/mode {"machine": <Vast id>, "mode": "vast|paralon|dual"}
SAVES the mode and does nothing else -- `Controller.set_mode`: under the
state file's lock, re-read the file, set the mode, write it atomically. The
`run` service re-reads the state file at the start of every pass and acts on
it. The page never runs a pass itself: the lock would keep the two passes
apart, but the service's Paralon call budget, its error back-off and its
just-sent guards live in its own process, and a second process acting on
Vast/Paralon would not see them. One writer stays one writer.

Protection (there is no login; the page can switch modes and edit the
config):
  - binds 127.0.0.1 by default; any other address prints a warning;
  - the Host header must be an IP address, `localhost` or this machine's own
    name, so a DNS-rebinding web page cannot load the page or its token;
  - every POST (mode, config check, config save) needs the per-process
    random token embedded in the page (header
    X-VPDual-Token), an Origin equal to the page's own (or none, for curl;
    a cross-site Sec-Fetch-Site is refused) and Content-Type
    application/json;
  - CSP with per-response nonces, frame-ancestors 'none' (no clickjacking),
    Cache-Control: no-store.
The JSON never holds the Paralon API key; as a backstop, anything in it that
looks like a key (the configured key, `api_key=...`, `Bearer ...`) is
replaced with [redacted] before it is sent.
"""

import hmac
import ipaddress
import json
import logging
import os
import re
import secrets
import signal
import socket
import socketserver
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from . import __version__
from . import pairing, settings
from .agent import DockerHost
from .config import clean_name
from .engine import Controller
from .state import MODES, StateStore
from .status import status_data
from .vast import VastError

log = logging.getLogger("vpdual.ui")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787
STATUS_MAX_AGE_S = 10     # one status read serves every request this long
REFRESH_S = 15            # the page asks again this often
TOKEN_HEADER = "X-VPDual-Token"
MAX_BODY = 4096
DRAIN_MAX = 65536         # bytes read from a refused body at most
POST_LIMITS = {"/api/mode": MAX_BODY, "/api/config/check": settings.MAX_CONFIG_BODY,
               "/api/config": settings.MAX_CONFIG_BODY}
REDACTED = "[redacted]"
_SECRET_RE = re.compile(r"(?i)(api[_-]?key=|bearer\s+)[^\s&\"'\\]+")


def is_loopback(host):
    if host.strip().lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def bind_warning(host, port):
    """The warning for a non-loopback address, or None."""
    if is_loopback(host):
        return None
    return (f"WARNING: the web UI listens on {host}:{port}, not on loopback. "
            "Anyone who can reach that address on your network can open the "
            "page, SWITCH MODES (unlist Vast, pause Paralon) and EDIT THE "
            "CONFIG: there is no "
            "login. Prefer the default 127.0.0.1 and an SSH tunnel from your "
            f"other device: ssh -L {port}:127.0.0.1:{port} <you>@<this host>")


def _hostname(host_header):
    """The name part of a Host header ("[::1]:8787" -> "::1")."""
    h = (host_header or "").strip()
    if h.startswith("["):
        end = h.find("]")
        return h[1:end] if end > 0 else ""
    if h.count(":") == 1:
        h = h.rsplit(":", 1)[0]
    return h.rstrip(".").lower()


def allowed_names(bind_host):
    me = socket.gethostname().lower()
    names = {"localhost", me, me + ".local"}
    if bind_host and not _is_ip(bind_host):
        names.add(bind_host.rstrip(".").lower())
    return names


def _is_ip(name):
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        return False


def host_allowed(host_header, names):
    """An IP address or one of `names`: never a name an outside page
    controls (DNS rebinding)."""
    name = _hostname(host_header)
    return bool(name) and (_is_ip(name) or name in names)


class UIApp:
    """What the HTTP handler calls. One per `ui` process."""

    def __init__(self, cfg, vast, paralon, *, dry_run=False, docker=DockerHost,
                 clock=time.time, monotonic=time.monotonic, config_path=None,
                 rebuild=None, environ=None):
        self.cfg = cfg
        self.config_path = config_path    # the Settings tab edits this file
        self.rebuild = rebuild            # cfg -> (vast, paralon) after a save
        self.environ = environ            # where the key came from (tests)
        self._save_lock = threading.Lock()
        self.vast = vast
        self.paralon = paralon
        self.dry_run = dry_run
        self.docker = docker
        self.clock = clock
        self.monotonic = monotonic
        self.token = secrets.token_urlsafe(32)
        self._lock = threading.Lock()     # one status read at a time
        self._cache = None                # (monotonic, generation, data)
        self._gen = 0                     # bumped by every saved mode
        self._seen = None                 # (when, (vast machines, nodes)) last read

    def controller(self, dry_run):
        """A Controller on the state file as it is now (each loads it)."""
        return Controller(self.cfg, self.vast, self.paralon,
                          StateStore(self.cfg.state_file), dry_run=dry_run,
                          clock=self.clock, docker=self.docker)

    def redact_value(self, value):
        """A string as the page received it (inside the JSON)."""
        return json.loads(self.redact(json.dumps(value)))

    def redact(self, text):
        key = self.cfg.paralon_api_key
        if key and len(key) >= 8:         # a real key; never mangle the JSON
            text = text.replace(key, REDACTED)
        return _SECRET_RE.sub(lambda m: m.group(1) + REDACTED, text)

    def status(self):
        with self._lock:
            now, gen = self.monotonic(), self._gen
            if (self._cache and self._cache[1] == gen
                    and now - self._cache[0] < STATUS_MAX_AGE_S):
                return self._cache[2]
            ctl = self.controller(dry_run=True)
            data = status_data(ctl, self.cfg, fresh_ok=False)
            if ctl.last_read is not None:
                self._seen = (self.clock(), ctl.last_read)
            data.update(ok=True, version=__version__,
                        generated_at=int(self.clock()), refresh_s=REFRESH_S,
                        apply_within_s=self.cfg.poll_interval_s,
                        dry_run=self.dry_run, modes=list(MODES))
            self._cache = (now, gen, data)
            return data

    def discover(self):
        """GET /api/discover: the user's own Vast machines and Paralon nodes,
        from the last status read (no call of its own), each marked if the
        config already has it, and name-based pairing suggestions for the
        ones it has not (the same matcher as `init`)."""
        if self._seen is None:
            try:
                self.status()             # one ordinary, cached status read
            except Exception:             # the Machines tab shows why
                log.exception("ui: discovery read failed")
        if self._seen is None:
            return 200, {"ok": True, "read": False, "vast": [], "paralon": [],
                         "suggest": {}, "message": "Your Vast machines and "
                         "Paralon nodes could not be read yet (the Machines "
                         "tab says why); you can type the ids by hand."}
        at, (vms, nodes) = self._seen
        have_v = {m.vast_machine_id for m in self.cfg.machines}
        have_n = {m.paralon_node_id for m in self.cfg.machines}
        good = {vid: vm for vid, vm in vms.items()
                if not isinstance(vm, VastError)}
        vast = []
        for vid, vm in sorted(good.items()):
            raw = vm.raw or {}
            gpus = (f"{raw.get('num_gpus')}x {raw.get('gpu_name')}"
                    if raw.get("num_gpus") and raw.get("gpu_name") else "")
            vast.append({"id": vid, "hostname": clean_name(raw.get("hostname")),
                         "gpus": clean_name(gpus), "listed": vm.listed,
                         "configured": vid in have_v})
        paralon = [{"id": n.id, "name": clean_name((n.raw or {}).get("name")),
                    "label": clean_name(n.label), "spot": n.spot,
                    "configured": n.id in have_n}
                   for n in sorted(nodes.values(), key=lambda n: n.id)]
        guess = pairing.suggest(
            [(vid, pairing.vast_names(vm)) for vid, vm in good.items()
             if vid not in have_v],
            [(n.id, pairing.node_names(n)) for n in nodes.values()
             if n.id not in have_n])
        return 200, {"ok": True, "read": True, "read_at": int(at),
                     "vast": vast, "paralon": paralon,
                     "suggest": {str(vid): {"node": nid,
                                            "match": pairing.TIER_NAMES[tier]}
                                 for vid, (nid, tier) in guess.items()}}

    def set_mode(self, body):
        """POST /api/mode -> (HTTP code, reply). Saves the mode, nothing
        else: the running service applies it on its next pass."""
        if not isinstance(body, dict):
            return 400, _err("send a JSON object: {\"machine\": <Vast "
                             "machine id>, \"mode\": \"vast|paralon|dual\"}")
        mode, machine = body.get("mode"), body.get("machine")
        if not isinstance(mode, str) or mode not in MODES:
            return 400, _err("mode must be one of: " + ", ".join(MODES))
        if isinstance(machine, str) and machine.isdigit():
            machine = int(machine)
        if isinstance(machine, bool) or not isinstance(machine, int):
            return 400, _err("machine must be a Vast machine id (a number)")
        if self.cfg.machine(machine) is None:
            return 400, _err(f"machine {machine} is not in the config")
        if self.dry_run:
            return 200, {"ok": True, "saved": False, "machine": machine,
                         "mode": mode, "message": "Dry run (--dry-run): "
                         "nothing was saved."}
        try:
            self.controller(dry_run=False).set_mode(mode, machine)
        except (OSError, ValueError) as exc:
            log.error("ui: saving mode %s for %s failed: %s", mode, machine,
                      exc)
            return 500, _err(f"could not save the mode: {exc}")
        self._gen += 1
        log.info("ui: machine %s mode set to %s (saved; the service applies "
                 "it on its next pass)", machine, mode)
        within = self.cfg.poll_interval_s
        return 200, {"ok": True, "saved": True, "machine": machine,
                     "mode": mode, "apply_within_s": within,
                     "message": f"Saved: machine {machine} is now in {mode} "
                     f"mode. The running service applies it on its next "
                     f"pass, within about {within} s."}


    # -- settings (the config file) -------------------------------------
    def settings_get(self):
        if not self.config_path:
            return 404, _err("no config file to edit")
        try:
            raw, base = settings.read_raw(self.config_path)
        except (OSError, ValueError) as exc:
            return 500, _err(f"cannot read {self.config_path}: {exc}")
        state = settings._state_path(raw, self.cfg.state_file)
        return 200, {"ok": True, "path": self.config_path, "base": base,
                     "config": settings.normalized(raw),
                     "schema": settings.schema(),
                     "key": settings.key_status(self.config_path, self.environ),
                     "owed": settings.owed_machines(state),
                     "dry_run": self.dry_run,
                     "restart": settings.RESTART_COMMAND}

    def _check(self, body):
        """(raw, base, merged, errors, diff rows, Config|None)."""
        raw, base = settings.read_raw(self.config_path)
        merged, errors, rows, cfg = settings.check(
            raw, body.get("config"), key=self.cfg.paralon_api_key,
            fallback_state=self.cfg.state_file, redact=self.redact_value)
        return raw, base, merged, errors, rows, cfg

    @staticmethod
    def _errors(errors):
        return [{"field": f, "message": m} for f, m in errors]

    def settings_check(self, body):
        """POST /api/config/check: validate and diff; writes nothing."""
        if not self.config_path:
            return 404, _err("no config file to edit")
        if not isinstance(body, dict) or not isinstance(body.get("config"),
                                                        dict):
            return 400, _err('send {"config": {...}, "base": "..."}')
        try:
            _, base, _, errors, rows, _ = self._check(body)
        except (OSError, ValueError) as exc:
            return 500, _err(f"cannot read {self.config_path}: {exc}")
        return 200, {"ok": True, "valid": not errors, "base": base,
                     "errors": self._errors(errors), "diff": rows}

    def settings_save(self, body):
        """POST /api/config: check again, back up, write atomically."""
        if not self.config_path:
            return 404, _err("no config file to edit")
        if not isinstance(body, dict) or not isinstance(body.get("config"),
                                                        dict):
            return 400, _err('send {"config": {...}, "base": "..."}')
        with self._save_lock:
            try:
                _, base, merged, errors, rows, cfg = self._check(body)
            except (OSError, ValueError) as exc:
                return 500, _err(f"cannot read {self.config_path}: {exc}")
            if body.get("base") != base:
                return 409, _err("the config file changed on disk since this "
                                 "page loaded it: nothing was saved. Reload "
                                 "the settings and make the change again.")
            if errors:
                reply = _err(f"not saved: {len(errors)} problem(s)")
                reply["errors"] = self._errors(errors)
                return 400, reply
            if not rows:
                return 200, {"ok": True, "saved": False, "diff": [],
                             "message": "Nothing changed."}
            if self.dry_run:
                return 200, {"ok": True, "saved": False, "diff": rows,
                             "message": "Dry run (--dry-run): nothing was "
                             "saved. The changes are listed above."}
            try:
                dest = settings.save(self.config_path, merged)
            except OSError as exc:
                log.error("ui: saving %s failed: %s", self.config_path, exc)
                return 500, _err(f"could not save: {exc}")
            self.cfg = cfg
            if self.rebuild is not None:
                self.vast, self.paralon = self.rebuild(cfg)
            self._gen += 1
            log.info("ui: config saved (%d change(s)); backup %s", len(rows),
                     dest)
            return 200, {
                "ok": True, "saved": True, "diff": rows,
                "backup": os.path.basename(dest),
                "restart": settings.RESTART_COMMAND,
                "message": "Saved (old file kept as "
                           f"{os.path.basename(dest)}). The running service "
                           "reads the config only when it starts: restart "
                           f"it to apply the change ({settings.RESTART_COMMAND}). "
                           "This page uses the new config now."}


def _err(msg):
    return {"ok": False, "error": msg}


class _Handler(BaseHTTPRequestHandler):
    server_version = "vast-paralon-dual"
    sys_version = ""

    @property
    def app(self):
        return self.server.app

    def log_message(self, fmt, *args):       # not one stderr line a request
        log.debug("ui %s " + fmt, self.address_string(), *args)

    # -- responses -----------------------------------------------------
    def _send(self, code, body, ctype, headers=()):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        for name, value in headers:
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _json(self, code, obj):
        # <, > and & only occur inside JSON strings: escaped there, a name
        # like "<img onerror=...>" cannot read as markup even if sniffed.
        text = self.app.redact(json.dumps(obj, sort_keys=True))
        text = text.replace("<", "\\u003c").replace(">", "\\u003e") \
                   .replace("&", "\\u0026")
        self._send(code, text, "application/json; charset=utf-8")

    # -- checks --------------------------------------------------------
    def _host_ok(self):
        if host_allowed(self.headers.get("Host"), self.server.allowed_names):
            return True
        self._json(403, _err("unexpected Host header: open the page by IP "
                             "address or as localhost"))
        return False

    def _same_origin(self):
        site = self.headers.get("Sec-Fetch-Site")
        if site is not None and site not in ("same-origin", "none"):
            return False
        origin = self.headers.get("Origin")
        if origin is None:
            return True                       # not a browser (curl): token only
        return origin.strip().lower() == \
            "http://" + self.headers.get("Host", "").strip().lower()

    def _token_ok(self):
        given = self.headers.get(TOKEN_HEADER, "")
        return hmac.compare_digest(given.encode("utf-8", "replace"),
                                   self.app.token.encode("utf-8"))

    # -- routes --------------------------------------------------------
    def do_GET(self):
        if not self._host_ok():
            return
        path = urlsplit(self.path).path
        if path == "/":
            nonce = secrets.token_urlsafe(16)
            page = PAGE.replace("__TOKEN__", self.app.token) \
                       .replace("__NONCE__", nonce)
            csp = ("default-src 'none'; "
                   f"script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; "
                   "connect-src 'self'; img-src data:; base-uri 'none'; "
                   "form-action 'none'; frame-ancestors 'none'")
            self._send(200, page, "text/html; charset=utf-8",
                       [("Content-Security-Policy", csp)])
        elif path == "/api/status":
            try:
                self._json(200, self.app.status())
            except Exception as exc:          # the page shows it; keep serving
                log.exception("ui: status failed")
                self._json(500, _err(f"status failed: {exc}"))
        elif path == "/api/config":
            self._json(*self.app.settings_get())
        elif path == "/api/discover":
            self._json(*self.app.discover())
        else:
            self._json(404, _err("not found"))

    do_HEAD = do_GET

    def _read_body(self, limit):
        """The request body, or None when it is missing or over `limit`.
        Read (up to a bound) before any reply, so a refusal is not lost to a
        TCP reset over unread bytes."""
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            return None
        if length < 0:
            return None
        data = self.rfile.read(min(length, DRAIN_MAX))
        return data if length <= limit else None

    def do_POST(self):
        path = urlsplit(self.path).path
        limit = POST_LIMITS.get(path, MAX_BODY)
        body = self._read_body(limit)
        if not self._host_ok():
            return
        if path not in POST_LIMITS:
            return self._json(404, _err("not found"))
        if not self._same_origin():
            return self._json(403, _err("cross-origin request refused"))
        if not self._token_ok():
            return self._json(403, _err("missing or wrong token: reload the "
                                        "page (it changes when `ui` restarts)"))
        ctype = self.headers.get("Content-Type", "").split(";")[0]
        if ctype.strip().lower() != "application/json":
            return self._json(415, _err("send Content-Type: application/json"))
        if body is None:
            return self._json(413, _err(f"send a JSON body of at most "
                                        f"{limit} bytes"))
        try:
            parsed = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return self._json(400, _err("body is not JSON"))
        handler = {"/api/mode": self.app.set_mode,
                   "/api/config/check": self.app.settings_check,
                   "/api/config": self.app.settings_save}[path]
        self._json(*handler(parsed))


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self):
        # HTTPServer.server_bind does a reverse DNS lookup (getfqdn) that can
        # hang on a LAN with no DNS; the name is not needed.
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name, self.server_port = host, port


class _Server6(_Server):
    address_family = socket.AF_INET6


def make_server(app, host=DEFAULT_HOST, port=DEFAULT_PORT):
    cls = _Server6 if ":" in host else _Server
    server = cls((host.strip("[]"), port), _Handler)
    server.app = app
    server.allowed_names = allowed_names(host)
    return server


def serve(cfg, vast, paralon, *, host=DEFAULT_HOST, port=DEFAULT_PORT,
          dry_run=False, docker=DockerHost, config_path=None, rebuild=None):
    app = UIApp(cfg, vast, paralon, dry_run=dry_run, docker=docker,
                config_path=config_path, rebuild=rebuild)
    try:
        server = make_server(app, host, port)
    except OSError as exc:
        print(f"error: cannot listen on {host}:{port}: {exc}", file=sys.stderr)
        return 2
    warning = bind_warning(host, port)
    if warning:
        print(warning, file=sys.stderr, flush=True)
        log.warning("web UI on a non-loopback address %s:%s", host, port)
    shown = ("<this machine's address>"
             if host in ("0.0.0.0", "::", "") else
             f"[{host.strip('[]')}]" if ":" in host else host)
    print(f"web UI: http://{shown}:{server.server_address[1]}/ "
          f"(Ctrl+C to stop){'; dry run, modes are not saved' if dry_run else ''}",
          flush=True)

    def stop(*_):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    log.info("web UI stopped")
    return 0


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<meta name="vpdual-token" content="__TOKEN__">
<title>Vast + Paralon</title>
<link rel="icon" href="data:,">
<script nonce="__NONCE__">
/* the saved theme, before the first paint (no flash) */
try {
  var t = localStorage.getItem("vpdual.theme");
  if (t === "light" || t === "dark") document.documentElement.setAttribute("data-theme", t);
} catch (e) { /* storage blocked: follow the system */ }
</script>
<style nonce="__NONCE__">
/* Palette: raw r g b channels. Dark is the default; light comes from the
   system setting (Auto) or the header switch (data-theme on <html>).
   Base colours fill buttons, borders and focus rings; text uses the -ink
   ones, nudged where needed to hold WCAG AA (4.5:1) on their own tints:
   dark danger-ink +4% lighter; light fg-3 7% darker, inks 11-19% darker. */
:root {
  color-scheme: dark;
  --c-canvas: 13 15 19; --c-surface: 22 25 31;
  --c-line: 46 51 61; --c-line-strong: 72 79 92;
  --c-fg: 245 246 248; --c-fg-2: 198 202 210; --c-fg-3: 152 158 170;
  --c-brand: 46 230 166; --c-on-brand: 13 15 19;
  --c-ok: 61 214 140; --c-warn: 245 181 70; --c-danger: 242 85 90;
  --c-info: 110 168 254; --c-scrim: 4 6 9;
  --c-brand-ink: 46 230 166; --c-ok-ink: 61 214 140; --c-warn-ink: 245 181 70;
  --c-danger-ink: 243 92 97; --c-info-ink: 110 168 254;
  --card-shadow: none;
}
@media (prefers-color-scheme: light) {
  :root:not([data-theme="dark"]) {
    color-scheme: light;
    --c-canvas: 240 243 248; --c-surface: 255 255 255;
    --c-line: 229 233 239; --c-line-strong: 208 214 223;
    --c-fg: 10 37 64; --c-fg-2: 62 80 100; --c-fg-3: 97 110 126;
    --c-brand: 0 128 96; --c-on-brand: 255 255 255;
    --c-ok: 0 128 96; --c-warn: 176 105 8; --c-danger: 204 44 50;
    --c-info: 99 91 255; --c-scrim: 10 37 64;
    --c-brand-ink: 0 111 84; --c-ok-ink: 0 111 84; --c-warn-ink: 143 85 6;
    --c-danger-ink: 182 39 44; --c-info-ink: 84 77 217;
    --card-shadow: 0 1px 2px rgb(10 37 64 / .05),
                   0 12px 28px -16px rgb(10 37 64 / .18);
  }
}
:root[data-theme="light"] {
  color-scheme: light;
  --c-canvas: 240 243 248; --c-surface: 255 255 255;
  --c-line: 229 233 239; --c-line-strong: 208 214 223;
  --c-fg: 10 37 64; --c-fg-2: 62 80 100; --c-fg-3: 97 110 126;
  --c-brand: 0 128 96; --c-on-brand: 255 255 255;
  --c-ok: 0 128 96; --c-warn: 176 105 8; --c-danger: 204 44 50;
  --c-info: 99 91 255; --c-scrim: 10 37 64;
  --c-brand-ink: 0 111 84; --c-ok-ink: 0 111 84; --c-warn-ink: 143 85 6;
  --c-danger-ink: 182 39 44; --c-info-ink: 84 77 217;
  --card-shadow: 0 1px 2px rgb(10 37 64 / .05),
                 0 12px 28px -16px rgb(10 37 64 / .18);
}
/* what the page uses: every colour below comes from these */
:root {
  --bg: rgb(var(--c-canvas)); --card: rgb(var(--c-surface));
  --fg: rgb(var(--c-fg)); --fg-2: rgb(var(--c-fg-2)); --muted: rgb(var(--c-fg-3));
  --line: rgb(var(--c-line)); --line-strong: rgb(var(--c-line-strong));
  --accent: rgb(var(--c-brand)); --accent-fg: rgb(var(--c-on-brand));
  --accent-ink: rgb(var(--c-brand-ink));
  --ok: rgb(var(--c-ok-ink)); --ok-bg: rgb(var(--c-ok) / .14);
  --ok-line: rgb(var(--c-ok));
  --vast: rgb(var(--c-info-ink)); --vast-bg: rgb(var(--c-info) / .14);
  --para: rgb(var(--c-brand-ink)); --para-bg: rgb(var(--c-brand) / .14);
  --warn-fg: rgb(var(--c-warn-ink)); --warn-bg: rgb(var(--c-warn) / .14);
  --warn-line: rgb(var(--c-warn) / .55);
  --bad-fg: rgb(var(--c-danger-ink)); --bad-bg: rgb(var(--c-danger) / .14);
  --bad-line: rgb(var(--c-danger) / .55);
  --chip: rgb(var(--c-fg-3) / .12);
  --tip-bg: rgb(var(--c-fg)); --tip-fg: rgb(var(--c-canvas));
  --scrim: rgb(var(--c-scrim) / .6);
  --pop-shadow: 0 6px 22px rgb(var(--c-scrim) / .35);
  --font: Inter, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  --mono: "JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, Consolas,
          monospace;
}
* { box-sizing: border-box; }
[hidden] { display: none !important; }
body { margin: 0; background: var(--bg); color: var(--fg);
  font: 15px/1.4 var(--font); }
a { color: var(--accent-ink); }
:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
code, .ids, .when, td.name small, .diff td + td { font-family: var(--mono);
  font-size: .92em; }
.theme button { min-height: 30px; padding: 3px 9px; font-size: .82rem; }
main { max-width: 1920px; margin: 0 auto; padding: 10px 16px 40px; }
header.top { display: flex; flex-wrap: wrap; align-items: center;
  gap: 6px 12px; margin: 2px 0 10px; }
h1 { font-size: 1.2rem; margin: 0; }
.spacer { flex: 1; }
.muted { color: var(--muted); }
.small { font-size: .82rem; }
button { font: inherit; color: inherit; background: var(--card);
  border: 1px solid var(--line); border-radius: 8px; padding: 6px 12px;
  min-height: 36px; cursor: pointer; }
button:disabled { cursor: default; opacity: .6; }
button.primary { background: var(--accent); color: var(--accent-fg);
  border-color: var(--accent); }
.seg { display: inline-flex; border: 1px solid var(--line); border-radius: 8px;
  overflow: hidden; }
.seg button { border: 0; border-radius: 0; min-height: 34px; }
.seg button + button { border-left: 1px solid var(--line); }
.seg button[aria-pressed="true"], .modes button[aria-pressed="true"] {
  background: var(--accent); color: var(--accent-fg); opacity: 1; }
.banner { border: 1px solid; border-radius: 8px; padding: 8px 12px;
  margin: 0 0 10px; overflow-wrap: anywhere; }
.warn { background: var(--warn-bg); color: var(--warn-fg);
  border-color: var(--warn-line); }
.bad { background: var(--bad-bg); color: var(--bad-fg);
  border-color: var(--bad-line); }
.okmsg { background: var(--ok-bg); border-color: var(--ok-line);
  color: var(--fg); }
/* (i): small, quiet, a bigger invisible tap area */
button.info { position: relative; display: inline-flex; align-items: center;
  justify-content: center; width: 16px; height: 16px; min-height: 0;
  padding: 0; margin: 0 0 0 4px; border-radius: 50%; vertical-align: -2px;
  border: 1px solid var(--muted); background: transparent; color: var(--muted);
  font: italic 700 10px/1 Georgia, "Times New Roman", serif;
  text-transform: none; letter-spacing: 0; cursor: help; }
button.info::after { content: ""; position: absolute; inset: -9px; }
button.info:hover, button.info:focus-visible,
button.info[aria-expanded="true"] { color: var(--accent);
  border-color: var(--accent); }
/* one tooltip layer, position: fixed on <body> (inside an open modal
   <dialog> while one is shown, since that is the top layer): no card,
   table or form can clip it; placed by script against the visible area */
#tip { position: fixed; z-index: 1000; left: 8px; top: 8px; margin: 0;
  max-width: min(320px, calc(100vw - 16px)); padding: 8px 10px;
  border-radius: 8px; background: var(--tip-bg); color: var(--tip-fg);
  font-size: .85rem; line-height: 1.4; font-weight: 400; text-align: left;
  text-transform: none; letter-spacing: 0; white-space: normal;
  overflow-wrap: anywhere; box-shadow: var(--pop-shadow);
  pointer-events: none; }
#tip.capped { overflow-y: auto; overscroll-behavior: contain;
  pointer-events: auto; }
/* cards: as many columns as fit, one on a phone */
.grid { display: grid; gap: 12px; align-items: stretch;
  grid-template-columns: repeat(auto-fill, minmax(min(100%, 340px), 1fr)); }
.card { display: flex; flex-direction: column; gap: 8px; min-width: 0;
  background: var(--card); border: 1px solid var(--line);
  border-radius: 12px; padding: 12px; box-shadow: var(--card-shadow); }
.head { display: flex; align-items: center; gap: 6px; min-width: 0; }
.head h2 { flex: 1; min-width: 0; margin: 0; font-size: 1.05rem;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.nowrap { white-space: nowrap; }
.ids { margin-top: -6px; font-size: .76rem; color: var(--muted);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.chip { display: inline-block; padding: 1px 8px; border-radius: 999px;
  font-size: .78rem; font-weight: 600; white-space: nowrap; }
/* free = outlined (open to renters); rented = filled (earning) */
.chip.free { color: var(--ok); box-shadow: inset 0 0 0 1px var(--ok-line); }
.chip.vast { background: var(--vast-bg); color: var(--vast); }
.chip.para { background: var(--para-bg); color: var(--para); }
.chip.bad { background: var(--bad-bg); color: var(--bad-fg); }
.chip.idle { background: var(--chip); color: var(--fg-2); }
.modes { display: flex; gap: 4px; align-items: center; }
.modes button.info { flex: none; margin-left: 2px; min-height: 0;
  padding: 0; }
.modes button { flex: 1; padding: 4px 6px; min-height: 34px; }
.sides { display: grid; grid-template-columns: 1fr 1fr; gap: 0 12px; }
.side { min-width: 0; }
h3 { font-size: .72rem; text-transform: uppercase; letter-spacing: .05em;
  color: var(--muted); margin: 0 0 2px; }
dl { display: grid; grid-template-columns: max-content minmax(0, 1fr);
  gap: 1px 8px; margin: 0; font-size: .88rem; }
dt { color: var(--muted); white-space: nowrap; }
dd { margin: 0; overflow-wrap: anywhere; }
.yes { color: var(--ok); font-weight: 600; }
.owed { color: var(--warn-fg); background: var(--warn-bg); font-weight: 600;
  border-radius: 4px; padding: 0 5px; }
.line { font-size: .88rem; overflow-wrap: anywhere; }
.note { font-size: .85rem; border-radius: 6px; padding: 4px 8px; margin: 0;
  color: var(--warn-fg); background: var(--warn-bg); overflow-wrap: anywhere; }
.note button.info { border-color: var(--warn-fg); color: var(--warn-fg); }
.next { margin: 0; padding-left: 16px; font-size: .88rem; }
.next li { overflow-wrap: anywhere; }
details { margin-top: auto; border-top: 1px solid var(--line);
  padding-top: 4px; font-size: .86rem; }
summary { cursor: pointer; color: var(--muted); padding: 4px 0; }
details > div { display: grid; gap: 8px; padding: 4px 0 2px; }
ol.decisions { margin: 0; padding-left: 18px; }
ol.decisions li { overflow-wrap: anywhere; }
.when { color: var(--muted); font-variant-numeric: tabular-nums; }
/* table view */
.table-wrap { overflow-x: auto; background: var(--card);
  border: 1px solid var(--line); border-radius: 12px;
  box-shadow: var(--card-shadow); }
table { border-collapse: collapse; width: 100%; font-size: .88rem; }
th, td { text-align: left; padding: 6px 10px; vertical-align: top;
  border-bottom: 1px solid var(--line); }
tr:last-child td { border-bottom: 0; }
th { color: var(--muted); font-size: .72rem; text-transform: uppercase;
  letter-spacing: .05em; white-space: nowrap; }
td.name { font-weight: 600; white-space: nowrap; }
td.name small { display: block; font-weight: 400; color: var(--muted); }
td .modes { min-width: 210px; }
td .modes button { min-height: 30px; padding: 2px 6px; font-size: .85rem; }
td.next-cell { min-width: 220px; overflow-wrap: anywhere; }
dialog { border: 1px solid var(--line); border-radius: 12px;
  background: var(--card); color: var(--fg); padding: 18px;
  box-shadow: var(--pop-shadow);
  width: min(440px, calc(100vw - 32px)); }
dialog::backdrop { background: var(--scrim); }
dialog h2 { font-size: 1.1rem; margin: 0 0 8px; overflow-wrap: anywhere; }
dialog .actions { display: flex; gap: 8px; justify-content: flex-end;
  margin-top: 14px; }
footer { margin-top: 18px; }
/* tabs */
.tabs { display: inline-flex; gap: 4px; }
.tabs button { border-radius: 8px; min-height: 34px; }
.tabs button[aria-selected="true"] { background: var(--accent);
  color: var(--accent-fg); border-color: var(--accent); }
/* settings */
.s-grid { display: grid; gap: 12px; align-items: start; margin-bottom: 12px;
  grid-template-columns: repeat(auto-fill, minmax(min(100%, 430px), 1fr)); }
.s-card { background: var(--card); border: 1px solid var(--line);
  border-radius: 12px; padding: 12px; min-width: 0;
  box-shadow: var(--card-shadow); }
.s-card > h2 { font-size: 1rem; margin: 0 0 6px; }
.s-card h3 { margin-top: 10px; }
.field { display: grid; gap: 2px 10px; align-items: center; padding: 3px 0;
  grid-template-columns: minmax(0, 12rem) minmax(0, 1fr); }
.flabel { color: var(--muted); font-size: .88rem; }
.ctl { display: flex; gap: 6px; align-items: center; min-width: 0; }
.ctl input, .ctl select, .list-row input { flex: 1; min-width: 0;
  font: inherit; padding: 5px 8px; min-height: 34px; border-radius: 6px;
  border: 1px solid var(--line-strong); background: var(--bg);
  color: var(--fg); }
.unit { color: var(--muted); font-size: .8rem; white-space: nowrap; }
.hint { grid-column: 2; font-size: .76rem; color: var(--muted); }
.ferr { grid-column: 1 / -1; color: var(--bad-fg); font-size: .82rem;
  overflow-wrap: anywhere; }
.has-err input, .has-err select { border-color: var(--bad-fg); }
@media (max-width: 520px) {
  .field { grid-template-columns: minmax(0, 1fr); }
  .hint { grid-column: 1; }
}
.list-row { display: flex; gap: 6px; margin-bottom: 4px; }
.m-grid { display: grid; gap: 12px; margin: 6px 0 10px;
  grid-template-columns: repeat(auto-fill, minmax(min(100%, 400px), 1fr)); }
fieldset.m-edit { border: 1px solid var(--line); border-radius: 10px;
  padding: 6px 12px 12px; margin: 0; min-width: 0; }
fieldset.m-edit legend { font-weight: 600; padding: 0 4px;
  overflow-wrap: anywhere; }
fieldset.m-edit details { margin-top: 6px; }
.add-panel { margin-top: 12px; padding-top: 8px;
  border-top: 1px dashed var(--line-strong); }
.add-panel button { margin: 6px 6px 0 0; }
.add-panel .field { max-width: 760px; }
button.danger { color: var(--bad-fg); border-color: var(--bad-line);
  margin-top: 8px; }
.s-actions { position: sticky; bottom: 0; display: flex; flex-wrap: wrap;
  gap: 8px; align-items: center; padding: 10px 0;
  background: var(--bg); border-top: 1px solid var(--line); }
.diff { max-height: 55vh; overflow: auto; margin: 6px 0;
  border: 1px solid var(--line); border-radius: 8px; }
.diff td { overflow-wrap: anywhere; }
.diff .added { color: var(--ok); }
.diff .removed { color: var(--bad-fg); }
dialog.wide { width: min(720px, calc(100vw - 32px)); }
</style>
</head>
<body>
<main>
  <header class="top">
    <h1>Vast + Paralon</h1>
    <nav class="tabs" role="tablist" aria-label="Sections">
      <button type="button" role="tab" id="tab-machines"
        aria-controls="panel-machines" aria-selected="true">Machines</button>
      <button type="button" role="tab" id="tab-settings"
        aria-controls="panel-settings" aria-selected="false"
        tabindex="-1">Settings</button>
    </nav>
    <span class="muted small" id="policy-wrap">policy <b id="policy">-</b></span>
    <span class="muted small" id="ago">loading...</span>
    <span class="spacer"></span>
    <span class="seg" id="view-seg" role="group" aria-label="View">
      <button type="button" id="view-cards" aria-pressed="true">Cards</button>
      <button type="button" id="view-table" aria-pressed="false">Table</button>
    </span>
    <button id="refresh" type="button">Refresh</button>
    <span class="seg theme" role="group" aria-label="Colours">
      <button type="button" data-theme-choice="auto" aria-pressed="true">Auto</button>
      <button type="button" data-theme-choice="light" aria-pressed="false">Light</button>
      <button type="button" data-theme-choice="dark" aria-pressed="false">Dark</button>
    </span>
  </header>
  <noscript><p class="banner warn">This page needs JavaScript. `status` in a
    shell shows the same.</p></noscript>
  <p id="notice" class="banner okmsg" role="status" hidden></p>
  <p id="fetch-error" class="banner bad" role="alert" hidden></p>
  <p id="dry-run" class="banner warn" hidden></p>
  <section id="panel-machines" role="tabpanel" aria-labelledby="tab-machines">
    <p id="read-error" class="banner bad" hidden></p>
    <div id="removed"></div>
    <div id="machines" class="grid"></div>
    <div id="table" class="table-wrap" hidden></div>
    <footer class="muted small">
      A mode switch is <b>saved</b> to the state file; the running
      <code>run</code> service applies it on its next pass. This page itself
      never lists, unlists, pauses or reclaims anything.
      <span id="version"></span>
    </footer>
  </section>
  <section id="panel-settings" role="tabpanel" aria-labelledby="tab-settings"
    hidden>
    <p class="muted small" id="s-where">loading...</p>
    <p id="s-msg" class="banner" role="status" hidden></p>
    <div id="s-general-errors"></div>
    <div id="s-form"></div>
    <div class="s-actions">
      <button type="button" id="s-review" class="primary">Review changes</button>
      <button type="button" id="s-discard">Discard edits</button>
      <span id="s-dirty" class="muted small"></span>
    </div>
  </section>
</main>
<dialog id="s-confirm" class="wide" aria-labelledby="s-c-title">
  <h2 id="s-c-title">Save these changes?</h2>
  <div id="s-diff" class="diff"></div>
  <p id="s-after" class="muted small"></p>
  <p id="s-result" class="bad banner" role="alert" hidden></p>
  <div class="actions">
    <button id="s-cancel" type="button">Cancel</button>
    <button id="s-save" type="button" class="primary">Save</button>
  </div>
</dialog>
<div id="tip" role="tooltip" hidden></div>
<dialog id="confirm" aria-labelledby="c-title">
  <h2 id="c-title"></h2>
  <p id="c-what"></p>
  <p id="c-when" class="muted small"></p>
  <p id="c-result" class="bad banner" role="alert" hidden></p>
  <div class="actions">
    <button id="c-cancel" type="button">Cancel</button>
    <button id="c-ok" type="button" class="primary">Switch</button>
  </div>
</dialog>
<script nonce="__NONCE__">
"use strict";
const TOKEN = document.querySelector('meta[name="vpdual-token"]').content;
// Every (i) text, in ONE place. Shown with textContent (never as HTML).
const HELP = {
  policy: "on_paralon_rental: what dual mode does with Vast while Paralon " +
    "is rented. unlist_vast: Vast is unlisted, and relisted when the " +
    "rental ends. keep_vast_listed: Vast stays listed; a Vast booking " +
    "reclaims the Paralon spot rental.",
  mode: "What this tool does with this machine. dual: listed on Vast and " +
    "Paralon at once. vast: Vast listed, Paralon paused. paralon: Vast " +
    "unlisted, Paralon available. Switching saves the choice; the running " +
    "service applies it on its next pass.",
  status: "Both sides at a glance. Vast or Paralon rented: who has the GPU. " +
    "Free on both: open to renters on both sites. Off both: unlisted on " +
    "Vast and paused on Paralon. Rented on both!: two renters share the " +
    "GPU (the rules reclaim a spot rental). Unreadable: a side could not " +
    "be read, so the service holds and changes nothing.",
  vast_state: "rented (N): N Vast renters running (Vast's count, or Vast " +
    "containers seen on the machine). listed: open to new Vast bookings. " +
    "unlisted: no new Vast bookings; a running renter is never evicted.",
  stored: "Vast instances that are stopped but can resume at any moment " +
    "(Vast's resident count minus running). While any are stored, " +
    "Paralon stays paused, because a resume would share the GPU.",
  vast_price: "Your Vast on-demand GPU price per hour, as Vast reports it now.",
  min_bid: "The lowest interruptible bid Vast accepts on this machine, " +
    "per hour.",
  paralon_state: "rented: a Paralon renter has the node. available: open " +
    "to new Paralon rentals. paused: not open to new rentals; the node " +
    "stays registered.",
  paralon_type: "spot: a Paralon rental can be reclaimed (the renter gets " +
    "2 minutes' notice) when a Vast renter arrives. ON-DEMAND: it cannot " +
    "be reclaimed, so in dual mode both renters could share the GPU; " +
    "switch the node to spot in the Paralon dashboard.",
  paralon_price: "What a Paralon renter pays per hour for this node.",
  owes: "What this tool itself took off and still has to put back: a Vast " +
    "relist, a Paralon un-pause, the Paralon agent's start, or your idle " +
    "job's start. Kept in the state file, so a restart never forgets it. " +
    "It never puts back what it did not take off.",
  next_pass: "What the service would do on its next pass, worked out now " +
    "in a dry run: nothing is written. 'hold' means it waits and changes " +
    "nothing.",
  agent: "Paralon's agent container on the GPU machine. This tool stops it " +
    "while a Vast renter has the GPU and starts it after, so Paralon " +
    "never starts work beside a Vast renter.",
  containers: "Containers seen on the machine itself: Vast renters " +
    "(C.<id>) and Paralon rental containers. The machine's own view " +
    "counts before the APIs catch up.",
  gpu: "Compute processes and memory in use on the GPU. The GPU must be " +
    "free (no process, little memory) before the agent starts or Vast is " +
    "relisted after a rental.",
  saved_listing: "The machine's own Vast listing terms (prices, disk, " +
    "bandwidth, offer end), saved just before this tool unlisted it, so " +
    "the relist restores them.",
  relist: "The terms a Vast relist would send: the saved terms, else the " +
    "config's vast_listing fallbacks. NOTHING: no GPU price and min bid " +
    "are known, so a relist would hold until you add them.",
  decisions: "This machine's last 5 decisions, newest first. (xN): the same " +
    "decision N times in a row.",
  ids: "The Vast machine id and the Paralon node id. Commands, support " +
    "tickets and the mode switch use these; the name is only a label.",
  save: "Saving replaces the config file in one step (written to a " +
    "temporary file, then swapped in, mode 600). The old file is kept as " +
    "config.json.bak-<date>-<time>; the last 10 are kept. Your comments " +
    "and custom keys stay as they were. The running service reads the " +
    "config only when it starts, so restart it to apply the change.",
  // Settings: one per config key ("Default: ..." is added from the
  // config's own defaults when the settings load)
  "cfg.on_paralon_rental": "What dual mode does with Vast while Paralon " +
    "is rented. unlist_vast: Vast is unlisted, and relisted when the " +
    "rental ends (Vast cannot start a renter on a GPU Paralon has). " +
    "keep_vast_listed: Vast stays listed; a Vast booking reclaims the " +
    "Paralon spot rental.",
  "cfg.poll_interval_s": "Seconds between passes. Each pass reads both " +
    "sides and applies the rules.",
  "cfg.fast_poll_s": "Seconds between passes while a machine recovers " +
    "after a Vast rental, or waits for the GPU before a Vast relist.",
  "cfg.docker_events": "Follow docker events on each machine with a " +
    "Paralon agent: pause Paralon the moment a non-Paralon container " +
    "starts, and unlist Vast (unlist_vast) and stop your idle job the " +
    "moment a Paralon rental container starts. Off: only the passes act.",
  "cfg.gpu_free_mib": "The GPU counts as free with no compute process and " +
    "at most this much memory in use, in MiB. Checked before the agent " +
    "starts after a Vast rental and before Vast is relisted after a " +
    "Paralon rental.",
  "cfg.idle_job_images": "Image name prefixes of your own idle job (a " +
    "miner, say), e.g. me/miner. It is never counted as a renter, is " +
    "stopped while a Paralon rental runs, and is started again after.",
  "cfg.vastai_bin": "Path to the vastai command. Under systemd the PATH " +
    "is minimal, so give the full path, e.g. ~/.local/bin/vastai.",
  "cfg.vast_listing": "Fallback listing terms for a relist, used only " +
    "where nothing was saved from the machine before this tool unlisted " +
    "it (e.g. a machine you unlisted by hand). A relist needs at least a " +
    "GPU price and a min bid. Empty fields are left out.",
  "cfg.l.price_gpu": "On-demand price per GPU per hour, in $.",
  "cfg.l.price_min_bid": "Lowest interruptible bid accepted, per GPU per " +
    "hour, in $.",
  "cfg.l.price_disk": "Storage price, in $ per GB per month.",
  "cfg.l.price_inetu": "Upload bandwidth price, in $ per GB.",
  "cfg.l.price_inetd": "Download bandwidth price, in $ per GB.",
  "cfg.l.discount_rate": "Largest prepay discount, as a fraction from 0 " +
    "to 1.",
  "cfg.l.min_chunk": "Smallest number of GPUs a renter can take.",
  "cfg.l.end_date": "Offer end, as a unix timestamp (seconds). Set this " +
    "OR duration, not both.",
  "cfg.l.duration": "A rolling offer end that Vast renews daily, e.g. " +
    "14 days. Set this OR end_date, not both.",
  "cfg.l.vol_size": "Volume offer size in GB; 0 for none.",
  "cfg.l.vol_price": "Volume price, in $ per GB per month.",
  "cfg.paralon_base_url": "Paralon's API address. Change it only if " +
    "Paralon tells you to.",
  "cfg.paralon_min_get_interval_s": "Fewest seconds between reads of " +
    "your Paralon nodes while nothing is happening (they are read once a " +
    "minute while a machine is rented or recovering).",
  "cfg.paralon_max_calls_per_min": "Hard cap on Paralon API calls per " +
    "minute, reads and writes together.",
  "cfg.reclaim_retry_s": "Seconds before a Paralon reclaim is sent again " +
    "if the rental has not ended (the renter gets 2 minutes' notice).",
  "cfg.jupyter_refresh_s": "While the Paralon agent is stopped, pull " +
    "Paralon's Jupyter image this often, in seconds, so it is fresh when " +
    "the agent starts again.",
  "cfg.state_file": "Where this tool keeps each machine's mode and what " +
    "it owes it. It cannot change while anything is owed: the new file " +
    "would forget the debts.",
  "cfg.key": "The Paralon API key (prlc_..., with the My nodes " +
    "permission). This page never shows or sends it. To change it, run " +
    "init again or edit the env file next to the config " +
    "(PARALON_API_KEY=...), then restart the service.",
  "cfg.machines": "Each Vast machine paired with its Paralon node. A " +
    "machine this tool still owes something cannot be removed.",
  "cfg.m.name": "A label for people, shown on the cards and by status. " +
    "Optional: left empty, the hostname Vast reports is shown, else the " +
    "Vast id.",
  "cfg.m.vast_machine_id": "The machine's id on Vast (vastai show " +
    "machines). Required.",
  "cfg.m.paralon_node_id": "The node's id on Paralon. Required.",
  "cfg.m.paralon_agent": "How to run docker on that machine: local, or " +
    "user@host for ssh (key login; the user in the docker group). " +
    "Strongly recommended: without it the Paralon agent keeps starting " +
    "work beside Vast renters. Empty: not managed.",
  "cfg.add": "Pick one of your Vast machines and one of your Paralon " +
    "nodes that the config does not have yet. They come from the last read " +
    "of your accounts (the Machines tab's), so nothing extra is asked of " +
    "Vast or Paralon. Adding only changes this page's copy: review and save " +
    "to keep it.",
  "cfg.add.vast": "Machines in your Vast account that the config does not " +
    "have yet: id, the hostname Vast reports, GPUs.",
  "cfg.add.node": "Nodes in your Paralon account not paired yet. When a " +
    "node's name matches the machine's hostname (ignoring case, spaces and " +
    "dashes) it is chosen for you; check it before adding.",
  "cfg.m.vast_listing": "This machine's own fallback listing terms. Each " +
    "one left empty uses the shared Vast listing fallback."
};
const HELP_EXTRA = {};   // "Default: ..." per settings key, from the server
const MODE_HELP = {
  dual: "Listed on both. A Vast renter wins the GPU; a Paralon rental " +
        "unlists or keeps Vast per the policy, and Vast is relisted after.",
  vast: "Vast listed, Paralon paused (the node stays registered).",
  paralon: "Vast unlisted (a running Vast renter is never evicted), Paralon " +
           "available once no Vast renter is running."
};
const $ = (id) => document.getElementById(id);
let last = null, lastAt = 0, timer = null, loading = false, pending = null;
let view = "cards";
const opened = new Set();     // machines whose details the user opened
try {
  const v = localStorage.getItem("vpdual.view");
  if (v === "cards" || v === "table") view = v;
} catch (e) { /* storage blocked: default view */ }

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined && text !== null) e.textContent = String(text);
  return e;
}

// -- (i) tooltips: hover, keyboard focus, or tap (tap again or elsewhere
//    closes); one shared role=tooltip element ----------------------------
const tip = $("tip");
let tipFor = null, tipPinned = false;
function info(key, label) {
  const b = el("button", "info", "i");
  b.type = "button";
  b.setAttribute("aria-label", "What is " + label + "?");
  b.setAttribute("aria-expanded", "false");
  b.dataset.help = key;
  return b;
}
function labelled(text, key) {
  const s = el("span", "nowrap", text);
  if (key) s.append(info(key, text));
  return s;
}
const TIP_GAP = 6, TIP_PAD = 8, TIP_MIN_H = 60;
// The part of the page the user can see, in the same coordinates as
// getBoundingClientRect(): the visual viewport on a phone (pinch-zoom,
// on-screen keyboard), else the layout viewport without its scrollbars.
function visibleBox() {
  const vv = window.visualViewport;
  if (vv) return {left: vv.offsetLeft, top: vv.offsetTop,
                  right: vv.offsetLeft + vv.width, bottom: vv.offsetTop + vv.height};
  const de = document.documentElement;
  return {left: 0, top: 0, right: de.clientWidth, bottom: de.clientHeight};
}
function placeTip(b) {
  // the top layer while a modal dialog is open; <body> otherwise
  const host = b.closest("dialog[open]") || document.body;
  if (tip.parentNode !== host) host.append(tip);
  const vp = visibleBox();
  // measure at full width and natural height, then place
  tip.classList.remove("capped");
  tip.style.maxHeight = "";
  tip.style.maxWidth = Math.max(120, Math.min(320, vp.right - vp.left - 2 * TIP_PAD)) + "px";
  tip.style.left = (vp.left + TIP_PAD) + "px";
  tip.style.top = vp.top + "px";
  const w = tip.offsetWidth, h = tip.offsetHeight;
  const r = b.getBoundingClientRect();
  const below = vp.bottom - TIP_PAD - (r.bottom + TIP_GAP);
  const above = (r.top - TIP_GAP) - (vp.top + TIP_PAD);
  let top;
  if (h <= below) {
    top = r.bottom + TIP_GAP;                       // preferred: below
  } else if (h <= above) {
    top = r.top - TIP_GAP - h;                      // flipped: above
  } else {                                          // neither: the roomier
    const room = Math.max(TIP_MIN_H, Math.max(below, above));   // side, scrolling
    tip.classList.add("capped");
    tip.style.maxHeight = room + "px";
    top = below >= above ? r.bottom + TIP_GAP : r.top - TIP_GAP - Math.min(h, room);
  }
  const hNow = tip.offsetHeight;
  top = Math.min(Math.max(top, vp.top + TIP_PAD), vp.bottom - TIP_PAD - hNow);
  const left = Math.min(Math.max(r.left + r.width / 2 - w / 2, vp.left + TIP_PAD),
                        vp.right - TIP_PAD - w);
  tip.style.top = Math.max(vp.top + TIP_PAD, top) + "px";
  tip.style.left = Math.max(vp.left + TIP_PAD, left) + "px";
}
function showTip(b, pin) {
  if (tipFor && tipFor !== b) hideTip();
  const key = b.dataset.help;
  tip.textContent = (HELP[key] || "") +
                    (HELP_EXTRA[key] ? " " + HELP_EXTRA[key] : "");
  tip.hidden = false;
  tipFor = b;
  tipPinned = pin || tipPinned;
  b.setAttribute("aria-describedby", "tip");
  b.setAttribute("aria-expanded", "true");
  placeTip(b);
}
function hideTip() {
  if (tipFor) {
    tipFor.removeAttribute("aria-describedby");
    tipFor.setAttribute("aria-expanded", "false");
  }
  tip.hidden = true;
  tipFor = null;
  tipPinned = false;
}
function infoOf(e) {
  return e.target instanceof Element ? e.target.closest("button.info") : null;
}
function intoTip(node) { return node instanceof Node && tip.contains(node); }
document.addEventListener("mouseover", (e) => {
  const b = infoOf(e);
  if (b && !tipPinned) showTip(b, false);
});
document.addEventListener("mouseout", (e) => {
  const b = infoOf(e);
  // moving onto a tall (scrolling) tip keeps it open
  if (b && b === tipFor && !tipPinned && !intoTip(e.relatedTarget)) hideTip();
});
tip.addEventListener("mouseleave", (e) => {
  if (!tipPinned && e.relatedTarget !== tipFor) hideTip();
});
document.addEventListener("focusin", (e) => {
  const b = infoOf(e);
  if (b) showTip(b, false);
});
document.addEventListener("focusout", (e) => {
  const b = infoOf(e);
  if (b && b === tipFor && !tipPinned) hideTip();
});
document.addEventListener("click", (e) => {
  const b = infoOf(e);
  if (b) {
    if (b === tipFor && tipPinned) hideTip(); else showTip(b, true);
  } else if (tipFor && !intoTip(e.target)) {
    hideTip();
  }
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && tipFor) hideTip();
});
window.addEventListener("resize", hideTip);
window.addEventListener("scroll", (e) => {
  if (tipFor && e.target !== tip) hideTip();      // not the tip's own scroll
}, true);
if (window.visualViewport) {
  window.visualViewport.addEventListener("resize", () => { if (tipFor) hideTip(); });
  window.visualViewport.addEventListener("scroll", () => { if (tipFor) hideTip(); });
}

// -- helpers ----------------------------------------------------------
function yesno(v, cls) {
  return el("span", v ? (cls || "yes") : "", v ? "yes" : "no");
}
function dl(rows) {   // [label, value, help key?]
  const d = el("dl");
  for (const [k, v, key] of rows) {
    const dt = el("dt");
    dt.append(labelled(k, key));
    const dd = el("dd");
    if (v instanceof Node) dd.append(v); else dd.textContent = String(v);
    d.append(dt, dd);
  }
  return d;
}
function heading(text, key) {
  const h = el("h3", null, text);
  if (key) h.append(info(key, text));
  return h;
}
function banner(id, text) {
  const b = $(id);
  b.hidden = !text;
  b.textContent = text || "";
}
function ago(ms) {
  const s = Math.max(0, Math.round(ms / 1000));
  if (s < 90) return s + " s ago";
  if (s < 5400) return Math.round(s / 60) + " min ago";
  if (s < 129600) return Math.round(s / 3600) + " h ago";
  return Math.round(s / 86400) + " d ago";
}
function when(t) {
  const d = new Date(t);
  if (isNaN(d)) return el("span", "when", t);
  const s = el("span", "when", d.toLocaleString([], {month: "short",
    day: "numeric", hour: "2-digit", minute: "2-digit"}) + " (" +
    ago(Date.now() - d.getTime()) + ")");
  s.title = t;
  return s;
}
function busy() {
  return $("confirm").open || $("s-confirm").open || tipFor !== null;
}
function money(v) { return typeof v === "number" ? "$" + v : "-"; }

// -- what each side is doing ------------------------------------------
function vastRenters(m) {
  const onMachine = m.machine && m.machine.error === null ?
    m.machine.vast_running.length : 0;
  return m.vast && m.vast.error === null ?
    Math.max(m.vast.running, onMachine) : onMachine;
}
function paralonRented(m) {
  return Boolean((m.paralon && m.paralon.error === null && m.paralon.rented) ||
    (m.machine && m.machine.error === null && m.machine.paralon_rentals.length));
}
function chip(m) {
  const v = m.vast, p = m.paralon;
  if (v === null || p === null) return ["Unreadable", "bad"];
  const vr = vastRenters(m) > 0, pr = paralonRented(m);
  if (vr && pr) return ["Rented on both!", "bad"];
  if (vr) return ["Vast rented", "vast"];
  if (pr) return ["Paralon rented", "para"];
  if (v.error !== null || p.error !== null) return ["Unreadable", "bad"];
  if (v.listed && p.available) return ["Free on both", "free"];
  if (v.listed) return ["Free on Vast", "free"];
  if (p.available) return ["Free on Paralon", "free"];
  return ["Off both", "idle"];
}
function chipEl(m) {
  const [text, cls] = chip(m);
  const s = el("span", "nowrap");
  s.append(el("span", "chip " + cls, text), info("status", "this status"));
  return s;
}
function vastShort(m) {
  const v = m.vast;
  if (v === null) return "not read";
  if (v.error !== null) return "unreadable";
  const r = vastRenters(m);
  return r ? "rented (" + r + ")" : v.listed ? "listed" : "unlisted";
}
function paralonShort(m) {
  const p = m.paralon;
  if (p === null) return "not read";
  if (p.error !== null) return "not found";
  return paralonRented(m) ? "rented" : p.available ? "available" : "paused";
}
function owedText(m) {
  const t = m.took_off, out = [];
  if (t.vast) out.push("Vast relist");
  if (t.paralon) out.push("Paralon un-pause");
  if (t.agent) out.push("agent start");
  if (m.idle_job_stopped.length) out.push("idle job " + m.idle_job_stopped.join(", "));
  return out.join(", ");
}
function nextFor(m, s) {
  if (s.next_pass === null) return null;
  return s.next_pass.filter((d) => d.machine === m.vast_machine_id).map((d) => d.msg);
}
function nextText(m, next) {
  return next === null ? "unknown (cannot read both sides)" :
    !m.mode ? "skipped (no mode)" : next.length ? null : "nothing to do";
}
function modeButtons(m, s, withInfo) {
  const modes = el("div", "modes");
  modes.setAttribute("role", "group");
  modes.setAttribute("aria-label", "Mode for " + m.name);
  for (const mode of s.modes) {
    const b = el("button", null, mode);
    b.type = "button";
    const on = m.mode === mode;
    b.setAttribute("aria-pressed", on ? "true" : "false");
    b.disabled = on;
    b.addEventListener("click", () => ask(m, mode, s));
    modes.append(b);
  }
  if (withInfo) modes.append(info("mode", "the mode"));
  return modes;
}

// -- a card -----------------------------------------------------------
function card(m, s) {
  const c = el("section", "card");
  const head = el("div", "head");
  const h = el("h2", null, m.name);
  h.title = m.name;
  head.append(h, chipEl(m));
  const ids = el("div", "ids", "Vast " + m.vast_machine_id + " · Paralon " +
                 m.paralon_node_id);
  ids.title = ids.textContent;
  c.append(head, ids, modeButtons(m, s, true));

  if (!m.mode) c.append(el("p", "note", "No mode set: the service skips " +
                                        "this machine until one is chosen."));
  for (const w of m.warnings) {
    const p = el("p", "note", "Warning: " + w);
    if (w.indexOf("on-demand") >= 0) p.append(info("paralon_type", "on-demand"));
    c.append(p);
  }

  const sides = el("div", "sides");
  const vs = el("div", "side"), ps = el("div", "side");
  vs.append(heading("Vast"));
  if (m.vast === null) vs.append(el("div", "line muted", "not read"));
  else if (m.vast.error !== null) vs.append(el("div", "line", "unreadable"));
  else {
    const stored = Math.max(0, m.vast.resident - m.vast.running);
    const rows = [["State", vastShort(m), "vast_state"],
                  ["GPU", money(m.vast.listing.price_gpu), "vast_price"],
                  ["Min bid", money(m.vast.listing.price_min_bid), "min_bid"]];
    if (stored) rows.splice(1, 0, ["Stored", stored, "stored"]);
    vs.append(dl(rows));
  }
  ps.append(heading("Paralon"));
  if (m.paralon === null) ps.append(el("div", "line muted", "not read"));
  else if (m.paralon.error !== null) ps.append(el("div", "line", "not found"));
  else ps.append(dl([["State", paralonShort(m), "paralon_state"],
                     ["Type", m.paralon.spot ? "spot" : "ON-DEMAND", "paralon_type"],
                     ["Price", m.paralon.price, "paralon_price"]]));
  sides.append(vs, ps);
  c.append(sides);

  const owed = owedText(m);
  if (owed) {
    const p = el("p", "note", "Owes: " + owed);
    p.append(info("owes", "owes"));
    c.append(p);
  }

  const next = nextFor(m, s);
  const nx = el("div");
  nx.append(heading("Next pass", "next_pass"));
  const nt = nextText(m, next);
  if (nt !== null) nx.append(el("div", "line muted", nt));
  else {
    const ul = el("ul", "next");
    for (const msg of next) ul.append(el("li", null, msg));
    nx.append(ul);
  }
  c.append(nx);
  c.append(details(m));
  return c;
}

function details(m) {
  const d = el("details");
  d.open = opened.has(m.vast_machine_id);
  d.addEventListener("toggle", () => {
    if (d.open) opened.add(m.vast_machine_id);
    else opened.delete(m.vast_machine_id);
  });
  d.append(el("summary", null, "Details"));
  const body = el("div");
  const rows = [["Ids", "Vast " + m.vast_machine_id + ", Paralon " +
                 m.paralon_node_id, "ids"]];
  if (m.vast !== null) rows.push(["Vast", m.vast.error !== null ?
    "unreadable (" + m.vast.error + ")" :
    "listed " + (m.vast.listed ? "yes" : "no") + ", running " + m.vast.running +
    ", resident " + m.vast.resident + "; now " + m.vast.listing_text, "vast_state"]);
  if (m.paralon !== null) rows.push(["Paralon", m.paralon.error !== null ?
    m.paralon.error : "state " + m.paralon.state + ", available " +
    (m.paralon.available ? "yes" : "no") + ", rented " +
    (m.paralon.rented ? "yes" : "no"), "paralon_state"]);
  if (m.paralon_agent) {
    const a = m.agent, ms = m.machine;
    rows.push(["Agent", "via " + m.paralon_agent + ": " + (a.state === "unreadable" ?
      "unreadable (" + a.error + ")" : a.state), "agent"]);
    if (ms.error !== null) rows.push(["Machine", "unreadable (" + ms.error + ")", "containers"]);
    else rows.push(["Vast containers", ms.vast_running.join(", ") || "none", "containers"],
                   ["Paralon rentals", ms.paralon_rentals.join(", ") || "none", "containers"],
                   ["GPU", ms.gpu, "gpu"]);
  } else {
    rows.push(["Agent", "not managed (no paralon_agent in the config)", "agent"]);
  }
  const t = m.took_off, took = el("span");
  took.append("Vast ", yesno(t.vast, "owed"), " · Paralon ",
              yesno(t.paralon, "owed"), " · agent ", yesno(t.agent, "owed"));
  rows.push(["Took off", took, "owes"]);
  if (m.idle_job_stopped.length)
    rows.push(["Idle job stopped", m.idle_job_stopped.join(", "), "owes"]);
  if (m.saved_listing_text !== null)
    rows.push(["Saved listing", m.saved_listing_text, "saved_listing"]);
  rows.push(["Relist", m.relist_text !== null ? m.relist_text :
    "NOTHING (no saved price, no config price: a relist would hold)", "relist"]);
  body.append(dl(rows));
  const dec = el("div");
  dec.append(heading("Last decisions", "decisions"));
  if (!m.decisions.length) dec.append(el("div", "line muted", "none yet"));
  else {
    const ol = el("ol", "decisions");
    for (const x of m.decisions.slice().reverse()) {
      const li = el("li");
      li.append(when(x.t), " " + x.msg + (x.count ? " (x" + x.count + ")" : ""));
      ol.append(li);
    }
    dec.append(ol);
  }
  body.append(dec);
  d.append(body);
  return d;
}

// -- the table --------------------------------------------------------
function table(s) {
  const t = el("table");
  const head = el("tr");
  for (const [h, key] of [["Machine"], ["Mode", "mode"], ["Status", "status"],
                          ["Vast", "vast_state"], ["Paralon", "paralon_state"],
                          ["Next pass", "next_pass"]]) {
    const th = el("th", null, h);
    if (key) th.append(info(key, h));
    head.append(th);
  }
  const thead = el("thead");
  thead.append(head);
  const tbody = el("tbody");
  for (const m of s.machines) {
    const tr = el("tr");
    const name = el("td", "name", m.name);
    name.title = "Vast " + m.vast_machine_id + ", Paralon " + m.paralon_node_id;
    name.append(el("small", null, "Vast " + m.vast_machine_id));
    const st = el("td");
    st.append(el("span", "chip " + chip(m)[1], chip(m)[0]));
    const md = el("td");
    md.append(modeButtons(m, s));
    let vast = vastShort(m);
    if (m.vast && m.vast.error === null && typeof m.vast.listing.price_gpu === "number")
      vast += " · " + money(m.vast.listing.price_gpu);
    let par = paralonShort(m);
    if (m.paralon && m.paralon.error === null)
      par += " · " + (m.paralon.spot ? "spot" : "ON-DEMAND") + " · " +
             m.paralon.price;
    const next = nextFor(m, s);
    const nt = nextText(m, next);
    const nx = el("td", "next-cell", nt !== null ? nt : next.join("; "));
    const owed = owedText(m);
    if (owed) nx.append(el("div", "small muted", "owes: " + owed));
    tr.append(name, md, st, el("td", null, vast), el("td", null, par), nx);
    tbody.append(tr);
  }
  t.append(thead, tbody);
  return t;
}

function setView(v) {
  view = v;
  try { localStorage.setItem("vpdual.view", v); } catch (e) { /* ignore */ }
  $("view-cards").setAttribute("aria-pressed", v === "cards" ? "true" : "false");
  $("view-table").setAttribute("aria-pressed", v === "table" ? "true" : "false");
  $("machines").hidden = v !== "cards";
  $("table").hidden = v !== "table";
  if (last) render(last);
}

function render(s) {
  hideTip();
  $("policy").textContent = s.policy;
  $("version").textContent = "vast-paralon-dual " + s.version;
  banner("dry-run", s.dry_run ? "Dry run (--dry-run): mode switches are " +
         "not saved." : null);
  banner("read-error", s.read_error !== null ? "Cannot read both sides, " +
         "the watcher would hold (change nothing) this pass: " +
         s.read_error : null);
  $("removed").replaceChildren(...s.removed_owing.map((o) => el("p", "banner warn",
    "Warning: machine " + o.vast_machine_id + " is no longer in the config " +
    "but the tool still owes it a relist: " +
    Object.keys(o.took_off).filter((k) => o.took_off[k]).join(", "))));
  if (!s.machines.length) {
    // nothing assumed: the machines come from the user's own config
    const empty = el("p", "banner warn", "No machines configured: run init " +
      "to pair your Vast machines with your Paralon nodes, or add them in " +
      "Settings.");
    $("machines").replaceChildren(empty);
    $("table").replaceChildren(empty.cloneNode(true));
    return;
  }
  if (view === "cards") $("machines").replaceChildren(...s.machines.map((m) => card(m, s)));
  else $("table").replaceChildren(table(s));
}

function tick() {
  if (!lastAt) return;
  $("ago").textContent = "updated " + ago(Date.now() - lastAt) +
    (busy() ? " (paused)" : "");
}
function schedule() {
  clearTimeout(timer);
  timer = setTimeout(load, ((last && last.refresh_s) || 15) * 1000);
}
async function load() {
  if (loading || busy() || document.hidden || tab !== "machines") {
    schedule();
    return;
  }
  loading = true;
  try {
    const r = await fetch("/api/status", {cache: "no-store",
                                          headers: {"Accept": "application/json"}});
    const j = await r.json().catch(() => ({}));
    if (!r.ok || !j.ok) throw new Error(j.error || "HTTP " + r.status);
    last = j; lastAt = Date.now();
    render(j);
    banner("fetch-error", null);
  } catch (e) {
    banner("fetch-error", "Update failed: " + e.message +
           (last ? " (showing the last data that loaded)" : ""));
  } finally {
    loading = false;
    tick();
    schedule();
  }
}

function ask(m, mode, s) {
  hideTip();
  pending = {machine: m.vast_machine_id, mode: mode};
  $("c-title").textContent = "Switch " + m.name + " to " + mode + "?";
  $("c-title").append(info("mode", "the modes"));
  $("c-what").textContent = "Vast " + m.vast_machine_id + ", Paralon " +
    m.paralon_node_id + ". Now: " + (m.mode || "no mode") + ". " +
    (MODE_HELP[mode] || "");
  $("c-when").textContent = s.dry_run ? "Dry run: nothing will be saved." :
    "The mode is saved now; the running service applies it on its next " +
    "pass, within about " + s.apply_within_s + " s. If the service is not " +
    "running, nothing changes until it starts.";
  banner("c-result", null);
  $("c-ok").disabled = false;
  $("confirm").showModal();
  tick();
}
async function confirmSwitch() {
  if (!pending) return;
  $("c-ok").disabled = true;
  try {
    const r = await fetch("/api/mode", {method: "POST",
      headers: {"Content-Type": "application/json", "X-VPDual-Token": TOKEN},
      body: JSON.stringify(pending)});
    const j = await r.json().catch(() => ({}));
    if (!r.ok || !j.ok) throw new Error(j.error || "HTTP " + r.status);
    $("confirm").close();
    const at = new Date().toLocaleTimeString([], {hour: "2-digit",
                                                   minute: "2-digit"});
    banner("notice", at + " — " + j.message);
    load();
  } catch (e) {
    banner("c-result", "Not switched: " + e.message);
    $("c-ok").disabled = false;
  }
}
$("policy-wrap").append(info("policy", "the policy"));
$("c-ok").addEventListener("click", confirmSwitch);
$("c-cancel").addEventListener("click", () => $("confirm").close());
$("confirm").addEventListener("close", () => {
  pending = null;
  hideTip();
  tick();
  schedule();
});
$("s-confirm").addEventListener("close", hideTip);
$("refresh").addEventListener("click", load);
$("view-cards").addEventListener("click", () => setView("cards"));
$("view-table").addEventListener("click", () => setView("table"));
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) load();
});
setInterval(tick, 1000);

// -- tabs -------------------------------------------------------------
const TABS = ["machines", "settings"];
function setTab(t) {
  tab = t;
  try { localStorage.setItem("vpdual.tab", t); } catch (e) { /* ignore */ }
  for (const name of TABS) {
    const on = name === t, btn = $("tab-" + name);
    btn.setAttribute("aria-selected", on ? "true" : "false");
    btn.tabIndex = on ? 0 : -1;
    $("panel-" + name).hidden = !on;
  }
  for (const id of ["policy-wrap", "ago", "view-seg", "refresh"])
    $(id).hidden = t !== "machines";
  hideTip();
  if (t === "settings" && !S.data) loadSettings();
  if (t === "machines") load();
}
for (const name of TABS) {
  $("tab-" + name).addEventListener("click", () => setTab(name));
  $("tab-" + name).addEventListener("keydown", (e) => {
    if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
    const next = TABS[(TABS.indexOf(name) + (e.key === "ArrowRight" ? 1 : -1)
                       + TABS.length) % TABS.length];
    setTab(next);
    $("tab-" + next).focus();
  });
}

// -- settings ---------------------------------------------------------
const S = {data: null, cfg: null, dirty: false, disc: null};
let fid = 0;
function clone(x) { return JSON.parse(JSON.stringify(x)); }
function sMsg(kind, text) {
  const b = $("s-msg");
  b.className = "banner " + (kind === "bad" ? "bad" : kind === "warn" ? "warn" : "okmsg");
  b.hidden = !text;
  b.textContent = text || "";
}
function setDirty(v) {
  S.dirty = v;
  $("s-dirty").textContent = v ? "Unsaved edits" : "";
}
async function postJSON(url, body) {
  const r = await fetch(url, {method: "POST",
    headers: {"Content-Type": "application/json", "X-VPDual-Token": TOKEN},
    body: JSON.stringify(body)});
  const j = await r.json().catch(() => ({}));
  return {r, j};
}
async function loadSettings() {
  try {
    const r = await fetch("/api/config", {cache: "no-store",
                                          headers: {"Accept": "application/json"}});
    const j = await r.json().catch(() => ({}));
    if (!r.ok || !j.ok) throw new Error(j.error || "HTTP " + r.status);
    S.data = j;
    S.cfg = clone(j.config);
    setDirty(false);
    for (const g of j.schema.groups)
      for (const f of g.fields) HELP_EXTRA["cfg." + f.key] =
        "Default: " + fmtDefault(f.default, f.type) + (f.unit ? " " + f.unit : "") + ".";
    renderSettings();
    loadDiscover();
  } catch (e) {
    sMsg("bad", "Cannot load the settings: " + e.message);
  }
}
function fmtDefault(v, type) {
  if (type === "bool") return v ? "on" : "off";
  if (Array.isArray(v)) return v.length ? v.join(", ") : "none";
  return v === "" || v === null || v === undefined ? "none" : String(v);
}
function fmtValue(v) {
  if (v === null || v === undefined) return "(not set)";
  return typeof v === "string" ? v : JSON.stringify(v);
}

function fieldRow(label, helpKey, control, unit, hint, path) {
  const row = el("div", "field");
  row.dataset.field = path;
  const id = "f" + (++fid);
  const lw = el("div", "flabel");
  const lab = el("label", null, label);
  lab.htmlFor = id;
  lw.append(lab);
  if (helpKey) lw.append(info(helpKey, label));
  control.id = id;
  const ctl = el("div", "ctl");
  ctl.append(control);
  if (unit) ctl.append(el("span", "unit", unit));
  row.append(lw, ctl);
  if (hint) row.append(el("div", "hint", hint));
  const err = el("div", "ferr");
  err.dataset.err = path;
  err.hidden = true;
  row.append(err);
  return row;
}
// One typed control bound to obj[key]. Empty = key left out (the default),
// except for required keys. Numbers are sent as numbers when they parse;
// anything else is sent as typed, and the server says what is wrong.
function valueInput(obj, key, type, opts) {
  opts = opts || {};
  if (type === "select" || type === "bool") {
    const c = el("select");
    if (!opts.required) c.append(new Option("default (" +
      fmtDefault(opts.defaultValue, type) + ")", ""));
    const choices = type === "bool" ? [["true", "on"], ["false", "off"]] :
      opts.options.map((o) => [o, o]);
    for (const [v, t] of choices) c.append(new Option(t, v));
    c.value = obj[key] === undefined ? "" : String(obj[key]);
    c.addEventListener("change", () => {
      if (c.value === "") delete obj[key];
      else obj[key] = type === "bool" ? c.value === "true" : c.value;
      setDirty(true);
    });
    return c;
  }
  const c = el("input");
  c.type = "text";
  if (type === "int") c.inputMode = "numeric";
  if (type === "number") c.inputMode = "decimal";
  c.autocomplete = "off";
  c.spellcheck = false;
  c.value = obj[key] === undefined || obj[key] === null ? "" : String(obj[key]);
  if (opts.defaultValue !== undefined && opts.defaultValue !== "" &&
      !Array.isArray(opts.defaultValue))
    c.placeholder = "default " + fmtDefault(opts.defaultValue, type);
  else if (opts.placeholder) c.placeholder = opts.placeholder;
  c.addEventListener("input", () => {
    const v = c.value.trim();
    if (v === "") { if (opts.required) obj[key] = ""; else delete obj[key]; }
    else if (type === "int" && /^-?\d+$/.test(v)) obj[key] = parseInt(v, 10);
    else if (type === "number" && isFinite(Number(v))) obj[key] = Number(v);
    else obj[key] = c.value;
    setDirty(true);
  });
  return c;
}
function listEditor(obj, key, path) {
  const box = el("div");
  const hadKey = S.data.config[key] !== undefined;
  function draw(focusLast) {
    box.replaceChildren();
    const arr = Array.isArray(obj[key]) ? obj[key] : [];
    arr.forEach((v, i) => {
      const row = el("div", "list-row");
      const inp = el("input");
      inp.type = "text";
      inp.value = String(v);
      inp.spellcheck = false;
      inp.setAttribute("aria-label", key + " " + (i + 1));
      inp.addEventListener("input", () => { obj[key][i] = inp.value; setDirty(true); });
      const rm = el("button", null, "Remove");
      rm.type = "button";
      rm.addEventListener("click", () => {
        obj[key].splice(i, 1);
        if (!obj[key].length && !hadKey) delete obj[key];
        setDirty(true);
        draw(false);
      });
      row.append(inp, rm);
      box.append(row);
    });
    const add = el("button", null, "Add an image prefix");
    add.type = "button";
    add.addEventListener("click", () => {
      if (!Array.isArray(obj[key])) obj[key] = [];
      obj[key].push("");
      setDirty(true);
      draw(true);
    });
    box.append(add);
    if (focusLast) {
      const all = box.querySelectorAll("input");
      if (all.length) all[all.length - 1].focus();
    }
  }
  draw(false);
  const row = el("div", "field");
  row.dataset.field = path;
  const lw = el("div", "flabel");
  lw.append(el("span", null, "Idle job images"), info("cfg." + key, "idle job images"));
  const err = el("div", "ferr");
  err.dataset.err = path;
  err.hidden = true;
  row.append(lw, box, err);
  return row;
}
// vast_listing of `owner` (the config, or a machine): created on the first
// value typed, dropped again when emptied (if the file did not have it).
function listingEditor(owner, path, hintEmpty) {
  const had = owner.vast_listing !== undefined;
  const lst = owner.vast_listing && typeof owner.vast_listing === "object" &&
              !Array.isArray(owner.vast_listing) ? owner.vast_listing : {};
  const wrap = el("div");
  const err = el("div", "ferr");
  err.dataset.err = path;
  err.hidden = true;
  wrap.append(err);
  for (const f of S.data.schema.listing) {
    wrap.append(fieldRow(f.key, "cfg.l." + f.key,
      valueInput(lst, f.key, f.type, {placeholder: hintEmpty}), f.unit, "",
      path + "." + f.key));
  }
  wrap.addEventListener("input", () => {
    if (Object.keys(lst).length) owner.vast_listing = lst;
    else if (!had) delete owner.vast_listing;
  });
  return wrap;
}
function card2(title, helpKey) {
  const c = el("section", "s-card");
  const h = el("h2", null, title);
  if (helpKey) h.append(info(helpKey, title));
  c.append(h);
  return c;
}

function renderSettings() {
  hideTip();
  const d = S.data, cfg = S.cfg;
  $("s-where").textContent = "Editing " + d.path +
    (d.dry_run ? " (dry run: nothing will be saved)" : "");
  const form = el("div");
  const grid = el("div", "s-grid");
  for (const g of d.schema.groups) {
    const c = card2(g.title);
    for (const f of g.fields) {
      if (f.type === "list") { c.append(listEditor(cfg, f.key, f.key)); continue; }
      c.append(fieldRow(f.key, "cfg." + f.key,
        valueInput(cfg, f.key, f.type, {defaultValue: f.default, options: f.options}),
        f.unit, "", f.key));
    }
    if (g.title === "Vast") {
      const h = el("h3", null, "Listing fallbacks (vast_listing)");
      h.append(info("cfg.vast_listing", "listing fallbacks"));
      c.append(h, listingEditor(cfg, "vast_listing", ""));
    }
    if (g.title === "Paralon") {
      const k = d.key;
      c.append(fieldRow("PARALON_API_KEY", "cfg.key",
        el("span", null, k.set ? "set (read from " + k.source + ")" :
          "NOT SET"), "", "Not editable here: run init again or edit " +
          k.env_file + ", then restart the service.", "PARALON_API_KEY"));
    }
    grid.append(c);
  }
  form.append(grid);

  const mc = card2("Machines", "cfg.machines");
  const merr = el("div", "ferr");
  merr.dataset.err = "machines";
  merr.hidden = true;
  mc.append(merr);
  const mg = el("div", "m-grid");
  const machines = Array.isArray(cfg.machines) ? cfg.machines : [];
  const onDisk = new Set((Array.isArray(d.config.machines) ? d.config.machines : [])
    .map((m) => String(m && m.vast_machine_id)));
  machines.forEach((m, i) => {
    if (!m || typeof m !== "object") return;
    const p = "machines[" + i + "]";
    const box = el("fieldset", "m-edit");
    box.append(el("legend", null, (m.name || "Machine " + (i + 1)) +
      (m.vast_machine_id !== "" && m.vast_machine_id !== undefined ?
       " · Vast " + m.vast_machine_id : "")));
    const err = el("div", "ferr");
    err.dataset.err = p;
    err.hidden = true;
    box.append(err);
    box.append(
      fieldRow("Name", "cfg.m.name", valueInput(m, "name", "text",
        {placeholder: "e.g. GPU box 1"}), "", "", p + ".name"),
      fieldRow("Vast machine id", "cfg.m.vast_machine_id",
        valueInput(m, "vast_machine_id", "int", {required: true}), "", "",
        p + ".vast_machine_id"),
      fieldRow("Paralon node id", "cfg.m.paralon_node_id",
        valueInput(m, "paralon_node_id", "text", {required: true}), "", "",
        p + ".paralon_node_id"),
      fieldRow("Paralon agent", "cfg.m.paralon_agent",
        valueInput(m, "paralon_agent", "text", {placeholder: "local or user@host"}),
        "", "", p + ".paralon_agent"));
    const det = el("details");
    const sum = el("summary", null, "Listing overrides");
    det.append(sum, listingEditor(m, p + ".vast_listing", "shared value"));
    box.append(det);
    const owes = d.owed[String(m.vast_machine_id)];
    const rm = el("button", "danger", "Remove this machine");
    rm.type = "button";
    if (owes && onDisk.has(String(m.vast_machine_id))) {
      rm.disabled = true;
      const note = el("p", "note", "Cannot be removed: this tool still owes " +
                      "it " + owes.what + ".");
      note.append(info("owes", "owes"));
      box.append(note);
    }
    rm.addEventListener("click", () => {
      cfg.machines.splice(i, 1);
      setDirty(true);
      renderSettings();
    });
    box.append(rm);
    mg.append(box);
  });
  mc.append(mg, addPanel(cfg));
  form.append(mc);
  $("s-form").replaceChildren(form);
}

// "Add a machine": the user's own Vast machines and Paralon nodes (from the
// last status read, GET /api/discover) that the edited config does not have
// yet, with the node pre-selected where its name matches the machine's
// hostname. Adding only edits the page's copy: Review -> diff -> Save, as
// for any other change.
async function loadDiscover() {
  try {
    const r = await fetch("/api/discover", {cache: "no-store",
                                            headers: {"Accept": "application/json"}});
    const j = await r.json().catch(() => ({}));
    if (!r.ok || !j.ok) throw new Error(j.error || "HTTP " + r.status);
    S.disc = j;
  } catch (e) {
    S.disc = {read: false, vast: [], paralon: [], suggest: {},
              message: "Your machines could not be read (" + e.message +
                       "); you can type the ids by hand."};
  }
  const old = $("add-panel");
  if (old && S.cfg) old.replaceWith(addPanel(S.cfg));
}
function addManual(cfg) {
  if (!Array.isArray(cfg.machines)) cfg.machines = [];
  cfg.machines.push({vast_machine_id: "", paralon_node_id: ""});
  setDirty(true);
  renderSettings();
  const sets = $("s-form").querySelectorAll("fieldset.m-edit");
  if (sets.length) sets[sets.length - 1].querySelector("input").focus();
}
function addPanel(cfg) {
  const box = el("div", "add-panel");
  box.id = "add-panel";
  const h = el("h3", null, "Add a machine");
  h.append(info("cfg.add", "adding a machine"));
  box.append(h);
  const D = S.disc;
  const machines = Array.isArray(cfg.machines) ? cfg.machines : [];
  const takenV = new Set(machines.map((m) => String(m && m.vast_machine_id)));
  const takenN = new Set(machines.map((m) => String(m && m.paralon_node_id)));
  const vs = D ? D.vast.filter((v) => !takenV.has(String(v.id))) : [];
  const ns = D ? D.paralon.filter((n) => !takenN.has(String(n.id))) : [];
  if (!D) box.append(el("p", "muted small", "Looking for your machines..."));
  else if (!D.read) box.append(el("p", "note", D.message));
  else if (!vs.length) box.append(el("p", "muted small", "Every Vast machine " +
    "in your account is already in the config."));
  else if (!ns.length) box.append(el("p", "muted small", "Every Paralon node " +
    "in your account is already paired."));
  if (vs.length && ns.length) {
    const vsel = el("select");
    vsel.append(new Option("Choose a Vast machine", ""));
    for (const v of vs) vsel.append(new Option(String(v.id) +
      (v.hostname ? " · " + v.hostname : "") +
      (v.gpus ? " · " + v.gpus : "") + (v.listed ? "" : " · unlisted"),
      String(v.id)));
    const nsel = el("select");
    nsel.append(new Option("Choose a Paralon node", ""));
    for (const n of ns) nsel.append(new Option((n.label || n.name || n.id) +
      " · " + n.id + (n.spot ? "" : " · ON-DEMAND"), n.id));
    const hint = el("p", "muted small");
    const addBtn = el("button", "primary", "Add this pair");
    addBtn.type = "button";
    const sync = () => { addBtn.disabled = !(vsel.value && nsel.value); };
    const pickNode = () => {
      const g = D.suggest[vsel.value];
      if (g && ns.some((n) => n.id === g.node)) {
        nsel.value = g.node;
        hint.textContent = "Node chosen by name (" + g.match + " match): " +
          "check it before adding.";
      } else {
        nsel.value = "";
        hint.textContent = vsel.value ? "No node name matches this machine: " +
          "choose its node." : "";
      }
      sync();
    };
    vsel.addEventListener("change", pickNode);
    nsel.addEventListener("change", () => { hint.textContent = ""; sync(); });
    // start on the first machine that has a free name match, if any
    const first = vs.find((v) => D.suggest[String(v.id)] &&
                                 ns.some((n) => n.id === D.suggest[String(v.id)].node));
    if (first) { vsel.value = String(first.id); pickNode(); } else sync();
    addBtn.addEventListener("click", () => {
      if (!Array.isArray(cfg.machines)) cfg.machines = [];
      cfg.machines.push({vast_machine_id: Number(vsel.value),
                         paralon_node_id: nsel.value});
      setDirty(true);
      renderSettings();
      sMsg("ok", "Added Vast " + vsel.value + " with node " + nsel.value +
           ". Review changes and save to keep it.");
    });
    box.append(
      fieldRow("Vast machine", "cfg.add.vast", vsel, "", "", "add.vast"),
      fieldRow("Paralon node", "cfg.add.node", nsel, "", "", "add.node"),
      hint, addBtn);
  }
  const manual = el("button", null, "Type the ids instead");
  manual.type = "button";
  manual.addEventListener("click", () => addManual(cfg));
  box.append(manual);
  return box;
}

function clearErrors() {
  for (const e of $("s-form").querySelectorAll(".ferr")) {
    e.hidden = true;
    e.textContent = "";
  }
  for (const e of $("s-form").querySelectorAll(".has-err")) e.classList.remove("has-err");
  $("s-general-errors").replaceChildren();
}
function showErrors(errors) {
  clearErrors();
  let first = null;
  for (const e of errors) {
    const spot = e.field ? $("s-form").querySelector(
      '.ferr[data-err="' + CSS.escape(e.field) + '"]') : null;
    if (spot) {
      spot.textContent = (spot.textContent ? spot.textContent + " " : "") + e.message;
      spot.hidden = false;
      const row = spot.closest(".field");
      if (row) row.classList.add("has-err");
      first = first || spot;
    } else {
      $("s-general-errors").append(el("p", "banner bad", e.message));
    }
  }
  if (first) first.scrollIntoView({block: "center"});
}
function drawDiff(rows) {
  const t = el("table");
  const hr = el("tr");
  for (const h of ["Setting", "Now", "After saving"]) hr.append(el("th", null, h));
  const thead = el("thead");
  thead.append(hr);
  const tb = el("tbody");
  for (const r of rows) {
    const tr = el("tr", r.kind);
    tr.append(el("td", null, r.path), el("td", null, fmtValue(r.old)),
              el("td", null, fmtValue(r.new)));
    tb.append(tr);
  }
  t.append(thead, tb);
  $("s-diff").replaceChildren(t);
}
async function review() {
  sMsg(null, null);
  try {
    const {r, j} = await postJSON("/api/config/check", {config: S.cfg});
    if (!r.ok || !j.ok) throw new Error(j.error || "HTTP " + r.status);
    if (!j.valid) {
      showErrors(j.errors);
      sMsg("bad", "Cannot save yet: " + j.errors.length + " problem(s), " +
           "marked beside the settings.");
      return;
    }
    clearErrors();
    if (!j.diff.length) { sMsg("ok", "No changes to save."); return; }
    drawDiff(j.diff);
    $("s-after").textContent = S.data.dry_run ?
      "Dry run (--dry-run): nothing will be saved." :
      "The old file is kept as a backup. The running service reads the " +
      "config only when it starts: after saving, restart it (" +
      S.data.restart + ").";
    $("s-after").append(info("save", "saving"));
    banner("s-result", null);
    $("s-save").disabled = false;
    $("s-save").textContent = S.data.dry_run ? "Check only" : "Save";
    $("s-confirm").showModal();
  } catch (e) {
    sMsg("bad", "Could not check the settings: " + e.message);
  }
}
async function saveSettings() {
  $("s-save").disabled = true;
  try {
    const {r, j} = await postJSON("/api/config", {config: S.cfg, base: S.data.base});
    if (r.status === 400 && j.errors) {
      $("s-confirm").close();
      showErrors(j.errors);
      sMsg("bad", j.error);
      return;
    }
    if (!r.ok || !j.ok) throw new Error(j.error || "HTTP " + r.status);
    $("s-confirm").close();
    if (j.saved) {
      await loadSettings();
      last = null;
    }
    sMsg(j.saved ? "ok" : "warn", j.message);
  } catch (e) {
    banner("s-result", "Not saved: " + e.message);
    $("s-save").disabled = false;
  }
}
$("s-review").addEventListener("click", review);
$("s-discard").addEventListener("click", () => {
  if (!S.data) return;
  S.cfg = clone(S.data.config);
  setDirty(false);
  clearErrors();
  sMsg(null, null);
  renderSettings();
});
$("s-save").addEventListener("click", saveSettings);
$("s-cancel").addEventListener("click", () => $("s-confirm").close());
window.addEventListener("beforeunload", (e) => {
  if (S.dirty) { e.preventDefault(); e.returnValue = ""; }
});

// -- colours: Auto (the system's), Light or Dark -----------------------
function setTheme(choice) {
  const root = document.documentElement;
  if (choice === "light" || choice === "dark") root.setAttribute("data-theme", choice);
  else root.removeAttribute("data-theme");
  try {
    if (choice === "light" || choice === "dark") localStorage.setItem("vpdual.theme", choice);
    else localStorage.removeItem("vpdual.theme");
  } catch (e) { /* not remembered */ }
  for (const b of document.querySelectorAll("[data-theme-choice]"))
    b.setAttribute("aria-pressed", b.dataset.themeChoice === (choice || "auto") ?
                   "true" : "false");
}
for (const b of document.querySelectorAll("[data-theme-choice]"))
  b.addEventListener("click", () => { hideTip(); setTheme(b.dataset.themeChoice); });
setTheme(document.documentElement.getAttribute("data-theme") || "auto");

let tab = "machines";
try {
  const t = localStorage.getItem("vpdual.tab");
  if (TABS.indexOf(t) >= 0) tab = t;
} catch (e) { /* storage blocked: Machines */ }
setView(view);
setTab(tab);
</script>
</body>
</html>
"""
