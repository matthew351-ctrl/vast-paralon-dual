"""Suggest which Paralon node goes with which Vast machine, by name.

Used by `init` (as the default the host confirms with Enter) and by the web
UI's Settings tab (to pre-select a node when adding a machine). Nothing is
ever paired by this module alone: it only proposes.

Names are compared after normalising case, spaces, dashes, underscores and
dots ("GPU-Box 1" == "gpubox1"). A match is exact, else a prefix (one name
starts with the other, e.g. "gpubox1" / "gpubox1paralon"), else fuzzy
(difflib ratio >= FUZZY_MIN). The best matches are taken first and each
machine and node is used at most once.
"""

import difflib
import re

FUZZY_MIN = 0.8
EXACT, PREFIX, FUZZY = 3, 2, 1
TIER_NAMES = {EXACT: "exact", PREFIX: "prefix", FUZZY: "similar"}
_DROP = re.compile(r"[\s\-_.]+")


def norm(name):
    return _DROP.sub("", str(name or "")).lower()


def score(a, b):
    """(tier, ratio) for two names; tier 0 = no match."""
    a, b = norm(a), norm(b)
    if not a or not b:
        return 0, 0.0
    if a == b:
        return EXACT, 1.0
    ratio = difflib.SequenceMatcher(None, a, b).ratio()
    if a.startswith(b) or b.startswith(a):
        return PREFIX, ratio
    if ratio >= FUZZY_MIN:
        return FUZZY, ratio
    return 0, ratio


def _best(names_a, names_b):
    best = (0, 0.0)
    for a in names_a:
        for b in names_b:
            best = max(best, score(a, b))
    return best


def suggest(machines, nodes):
    """machines: [(vast id, [names])], nodes: [(node id, [names])].
    Returns {vast id: (node id, tier)}, best matches first, each side used
    once; ties go to the lower Vast id, then the lower node id."""
    cands = []
    for vid, vnames in machines:
        for nid, nnames in nodes:
            tier, ratio = _best(vnames, nnames)
            if tier:
                cands.append((-tier, -ratio, vid, str(nid), nid))
    cands.sort(key=lambda c: c[:4])
    out, used = {}, set()
    for neg_tier, _, vid, _, nid in cands:
        if vid in out or nid in used:
            continue
        out[vid] = (nid, -neg_tier)
        used.add(nid)
    return out


def vast_names(vm):
    """The names a Vast machine is known by (its hostname)."""
    raw = getattr(vm, "raw", None) or {}
    return [raw.get("hostname")] if raw.get("hostname") else []


def node_names(node):
    raw = getattr(node, "raw", None) or {}
    return [raw.get("name")] if raw.get("name") else []
