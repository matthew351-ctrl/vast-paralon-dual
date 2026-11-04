"""What `status` shows, built ONCE as plain data.

`status_data()` reads both sides (and each managed machine), works out what
one pass would do in a dry run, and returns a JSON-ready dict. The `status`
command prints it with `status_lines()`; the web UI serves the same dict as
GET /api/status. Nothing here writes: the controller is switched to dry-run.

The dict holds only what `status` prints -- never the Paralon API key, the
config, or the raw API records.
"""

import logging

from .agent import AgentError, agent_of
from .config import clean_name
from .engine import describe_listing
from .paralon import ParalonError
from .state import MODES, owes_something
from .vast import VastError

log = logging.getLogger("vpdual")

NOT_FOUND_NODE = "node not found in GET /nodes"


def _agent(ctl, cfg, m):
    """(agent, machine) for a machine with `paralon_agent` set."""
    try:
        found = agent_of(ctl.docker(m.paralon_agent).containers())
        agent = {"state": ("not found" if found is None else
                           "running" if found.running else "stopped"),
                 "error": None}
    except (AgentError, ValueError) as exc:
        agent = {"state": "unreadable", "error": str(exc)}
    try:
        ms = ctl.docker(m.paralon_agent).machine_state()
        machine = {"vast_running": list(ms.vast_running(cfg.idle_job_images)),
                   "paralon_rentals": list(ms.paralon_rentals()),
                   "gpu": ms.gpu_text(), "error": None}
    except (AgentError, ValueError) as exc:
        machine = {"error": str(exc)}
    return agent, machine


def display_name(m, vm=None):
    """(name, where from): the config's `name`, else the hostname Vast
    reports for the machine (from the read `status` already makes), else
    the Vast machine id."""
    if m.name:
        return m.name, "config"
    host = clean_name((getattr(vm, "raw", None) or {}).get("hostname"))
    if host:
        return host, "vast"
    return str(m.vast_machine_id), "id"


def _vast(vm):
    if vm is None or isinstance(vm, VastError):
        return {"error": str(vm) if vm is not None else "not in your account"}
    terms = vm.listing_terms
    return {"listed": vm.listed, "running": vm.running,
            "resident": vm.resident, "listing": terms,
            "listing_text": describe_listing(terms), "error": None}


def _paralon(n):
    if n is None:
        return {"error": NOT_FOUND_NODE}
    return {"state": n.state, "available": n.available, "spot": n.spot,
            "price": n.price_text, "rented": n.rented, "error": None}


def status_data(ctl, cfg, machine_id=None, *, fresh_ok=True):
    """Both sides, owed relists, and what one pass would do. Writes nothing.

    `fresh_ok=False` never forces a fresh GET /nodes past the client's cache
    (the web UI asks every few seconds; the service owns the fresh reads)."""
    ctl.dry_run = True
    targets = ctl.targets(machine_id)
    out = {"policy": cfg.on_paralon_rental, "read_error": None,
           "machines": [], "next_pass": None, "removed_owing": []}
    try:
        data = ctl.read(fresh_ok=fresh_ok)
    except (VastError, ParalonError) as exc:
        data = None
        out["read_error"] = str(exc)
    for m in targets:
        st = ctl.mstate(m)
        relist = ctl.relist_listing(m)
        saved = st["saved_listing"]
        vm = data[0].get(m.vast_machine_id) if data is not None else None
        name, name_from = display_name(m, vm)
        row = {
            "name": name, "name_from": name_from,
            "vast_machine_id": m.vast_machine_id,
            "paralon_node_id": m.paralon_node_id,
            "mode": st["mode"],
            "took_off": dict(st["took_off"]),
            "idle_job_stopped": [str(j.get("name") or j["id"])
                                 for j in st.get("idle_job") or []],
            "paralon_agent": m.paralon_agent,
            "agent": None, "machine": None,
            "saved_listing": dict(saved) if saved else None,
            "saved_listing_text": describe_listing(saved) if saved else None,
            "relist": relist,
            "relist_text": describe_listing(relist) if relist else None,
            "vast": None, "paralon": None, "warnings": [],
            "decisions": [dict(d) for d in st["decisions"][-5:]],
        }
        if m.paralon_agent:
            row["agent"], row["machine"] = _agent(ctl, cfg, m)
        if data is not None:
            row["vast"] = _vast(vm)
            n = data[1].get(m.paralon_node_id)
            row["paralon"] = _paralon(n)
            if n is not None and st["mode"] == "dual" and not n.spot:
                row["warnings"].append("node is on-demand; dual mode needs "
                                       "spot to reclaim")
        out["machines"].append(row)
    for mid, st in ctl.state["machines"].items():
        if cfg.machine(mid) is None and owes_something(st):
            out["removed_owing"].append({"vast_machine_id": mid,
                                         "took_off": dict(st["took_off"])})
    if data is not None:
        level = log.level
        log.setLevel(logging.CRITICAL)   # returned below instead
        try:
            ctl.apply(data, [m for m in targets
                             if ctl.mstate(m)["mode"] in MODES])
        finally:
            log.setLevel(level)
        names = {r["vast_machine_id"]: r["name"] for r in out["machines"]}
        out["next_pass"] = [{"machine": mid, "name": names.get(mid, str(mid)),
                             "msg": msg} for mid, msg in ctl.pass_decisions]
    return out


def status_lines(s):
    """The `status` text, line by line (a line may start with "\\n")."""
    lines = [f"policy: {s['policy']}"]
    if s["read_error"] is not None:
        lines.append("cannot read both sides, the watcher would hold: "
                     f"{s['read_error']}")
    for r in s["machines"]:
        took = r["took_off"]
        vast = (f"Vast {r['vast_machine_id']}" if r["name_from"] == "id" else
                f"{r['name']} (Vast {r['vast_machine_id']})")
        lines.append(f"\n{vast} <-> Paralon {r['paralon_node_id']}")
        lines.append(f"  mode: {r['mode'] or '(not set: run `mode dual`)'}")
        lines.append(f"  took off (owed): vast={took['vast']} "
                     f"paralon={took['paralon']} agent stopped={took['agent']}"
                     + (f" idle job stopped={', '.join(r['idle_job_stopped'])}"
                        if r["idle_job_stopped"] else ""))
        if r["paralon_agent"]:
            a = r["agent"]
            seen = (f"unreadable ({a['error']})" if a["state"] == "unreadable"
                    else a["state"])
            lines.append(f"  Paralon agent via {r['paralon_agent']}: {seen}")
            ms = r["machine"]
            if ms.get("error") is not None:
                lines.append(f"  machine: unreadable ({ms['error']})")
            else:
                lines.append(
                    "  machine: Vast containers running: "
                    + (", ".join(ms["vast_running"]) if ms["vast_running"]
                       else "none")
                    + "; Paralon rental containers: "
                    + (", ".join(ms["paralon_rentals"])
                       if ms["paralon_rentals"] else "none")
                    + f"; {ms['gpu']}")
        else:
            lines.append("  Paralon agent: not managed (no paralon_agent in "
                         "the config)")
        if r["saved_listing_text"] is not None:
            lines.append(f"  saved listing: {r['saved_listing_text']}")
        lines.append("  relist would use: " + (
            r["relist_text"] if r["relist_text"] is not None else
            "NOTHING (no saved price, no config price: a relist would hold)"))
        v, n = r["vast"], r["paralon"]
        if v is not None:
            if v["error"] is not None:
                lines.append(f"  Vast: unreadable ({v['error']})")
            else:
                lines.append(f"  Vast: listed={v['listed']} "
                             f"running={v['running']} "
                             f"resident={v['resident']} "
                             f"now {v['listing_text']}")
        if n is not None:
            if n["error"] is not None:
                lines.append(f"  Paralon: {n['error']}")
            else:
                kind = "spot" if n["spot"] else "ON-DEMAND"
                lines.append(f"  Paralon: state={n['state']} "
                             f"available={n['available']} {kind} "
                             f"price={n['price']} rented={n['rented']}")
        lines += [f"  WARNING: {w}" for w in r["warnings"]]
    if s["next_pass"] is not None:
        lines.append("\nnext pass would:")
        lines += ([f"  [{d['name']}] {d['msg']}" if d["machine"]
                   else f"  {d['msg']}" for d in s["next_pass"]]
                  or ["  nothing to do"])
    for o in s["removed_owing"]:
        lines.append(f"\nWARNING: machine {o['vast_machine_id']} is no longer "
                     "in the config but the tool still owes it a relist: "
                     f"{o['took_off']}")
    lines.append("\nlast decisions:")
    for r in s["machines"]:
        for d in r["decisions"]:
            count = f" (x{d['count']})" if d.get("count") else ""
            lines.append(f"  {d['t']}  [{r['name']}] {d['msg']}"
                         f"{count}")
    return lines
