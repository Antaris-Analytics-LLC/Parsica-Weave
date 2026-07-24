#!/usr/bin/env python3
"""
Parsica Weave - conviction calibration ledger.

Two axes of confidence, kept separate on purpose:
  * CONVICTION-at-creation - the agent's first-person stance when it forms a decision or
    scar ("how strongly should this shape my future behavior?"). Captured at creation in
    the enrichment overlay, NEVER overwritten; it's unrecoverable later.
  * CALIBRATION - the deterministic, substrate-side trust the conviction has EARNED, from
    provenance: confirm/contradict events recorded against the decision over time.

This module owns the calibration half: an append-only event ledger and the (deliberately
boring) math that turns events into a reliability score and an effective weight. The
trajectory is the point - never collapse it to a single "confidence" number; that throws
away the good part (whether a conviction held up, or didn't).

Ledger: calibration/<date>.<pid>.jsonl, append-only per-pid shards (race-free, durable -
not pruned). Each event: {"target": "<decision-id>", "event": "confirmed|contradicted|
eroded", "weight": 1.0, "source": "...", "at": "<iso>"}.

CLI:
  python calibrate.py --cwd C --target <did> --event confirmed --source test_x
  python calibrate.py --cwd C --stats [--target <did>]
"""
import sys
import os
import re
import json
import math
import uuid
import datetime
import argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store
import scrub

_ALPHA = 2.0          # neutral Beta prior - a fresh conviction sits at reliability 0.5
_BETA = 2.0
_EVENTS = ("confirmed", "contradicted", "eroded")
# `target` must be an identifier (a decision's 12-char did), never free-form prose that
# could smuggle a secret into the ledger. The charset covers did + uuid + scar ids. Anchored with
# \Z, NOT $: `$` matches before a trailing "\n", so a value like "123.45.6789\n" would pass the
# id-shape gate AND then its stray "\n" would make the separator-stripped norm non-isdigit, skipping
# the PII length check below - a dotted SSN/phone lands RAW on the durable ledger. \Z admits no
# trailing newline, so the PII boundary (record() below) sees only clean, newline-free ids.
_VALID_TARGET = re.compile(r"^[A-Za-z0-9_.:\-]{1,96}\Z")
_MAX_WEIGHT = 5.0


def _safe_weight(x):
    """Event weights feed the reliability math - a NaN/Inf/negative weight would poison it
    (and NaN serializes to invalid JSON). Coerce to a finite, bounded, non-negative float."""
    try:
        w = float(x)
    except Exception:
        return 1.0
    if not math.isfinite(w):
        return 1.0
    return max(0.0, min(w, _MAX_WEIGHT))


def _now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _target_is_clean(target):
    """The ONE target-safety oracle: True only for an id-shaped value carrying no workspace refusal
    term and no PII/secret in raw OR separator-stripped form. SHARED by record() (the input
    boundary) and every ECHO of a raw --target (round-4 4b): emit_json's fence is refusal-ONLY, so
    a rejected PII/secret value would ride a receipt raw - and a separator-hidden phone (M3) is
    scrub-INVARIANT, so re-scrubbing the echo could not redact it either. An echo site therefore
    WITHHOLDS any target this refuses; one oracle, so the boundary and its display twins can never
    drift apart (validate at the effect)."""
    target = str(target or "")
    if not _VALID_TARGET.match(target):
        return False
    # a target is a decision/scar id, never a fenced term: if the workspace refusal fence would
    # ALTER it, it is a refusal term smuggled as an id (the reserved-key bypass GPT's review found),
    # so reject the whole event rather than write a redacted, meaningless ledger key.
    if scrub.apply_refusal(target, store.workspace_refusal_terms())[0] != target:
        return False
    # PII-as-id reject. The id charset admits digit runs AND separators (. _ : -), so a phone/SSN/card
    # could be smuggled as a `target` - and target, UNLIKE source, is a join key that is never
    # PII-scrubbed (scrubbing a did would corrupt the join), so a PII value would land on the ledger
    # RAW. The scrubber's regexes each accept only the separator class that shape is actually written
    # in - card takes ' -._:', SSN takes ' -.' , phone takes ' -.' - so PII written with a separator
    # outside its own shape's class (all legal here) can slip both the raw scrub AND a naive digit
    # check (M3). Defence in depth: this stays correct however those classes are tuned, because it
    # does not depend on them. So run the PII check on the SEPARATOR-STRIPPED digit stream, not the
    # raw string: strip the id separators to a bare stream, then reject if the scrubber alters it OR
    # it is a bare digit run of a standard PII length (9=SSN, 10=phone, 13-19=card). A machine-minted
    # id (a did/run_id is 12 hex chars) is NOT a standard PII shape, so an all-numeric 12-digit id
    # PASSES (the old blanket isdigit()>=9 wrongly dropped it - m6). This enforces the non-PII
    # property at the INPUT boundary rather than trusting the slug shape to prove it (a slug schema
    # is NOT proof a value is not PII).
    # ACCEPTED RESIDUAL: 11- and 12-digit runs are excluded from the denylist so a 12-digit numeric
    # did passes, which means an 11/12-digit international phone smuggled as a target is NOT caught
    # here - it is structurally indistinguishable from a did by length alone. This fence is real but
    # not total; the v1.1 close is a structured id-shape allowlist, not a wider denylist.
    norm = re.sub(r"[ ._:\-]", "", target)
    if (scrub.scrub(target, scrub_pii=True)[0] != target        # raw PII the shape's own class catches
            or scrub.scrub(norm, scrub_pii=True)[0] != norm      # PII hidden behind . _ : separators (M3)
            or (norm.isdigit() and (len(norm) in (9, 10) or 13 <= len(norm) <= 19))):
        return False
    return True


#: what an echo displays in place of a target the shared oracle refuses: the operator just typed
#: the real value, so nothing is lost - and a rejected target is by definition refusal/PII/secret
#: shaped, which must never transit stdout (it may be piped or logged).
_WITHHELD = "<target-withheld>"


def record(cwd, target, event, *, source="", weight=1.0):
    """Append a calibration event. Per-pid shard => race-free, no rewrite.
    The ledger is a disk surface like any other, so it obeys the same scrub-before-store
    law: `target` is validated as an id (never arbitrary text) through the shared
    _target_is_clean oracle, `source` is SCRUBBED before storage (it can be a test name /
    file / why - any of which could carry a pasted secret), and `weight` is clamped finite
    so a bad value can't poison reliability or break JSON."""
    target = str(target or "")
    if event not in _EVENTS or not _target_is_clean(target):
        return False
    shard = store.calibration_dir(cwd) / f"{_now()[:10]}.{os.getpid()}.jsonl"
    # source is pre-scrubbed at scrub_pii=True (calibration ALWAYS strips PII from the source,
    # regardless of the project's scrub_pii setting), then the write goes through the store
    # gateway, which re-scrubs (adds the refusal fence) and is the sole path to disk. This
    # handler holds no raw write of its own.
    src, _ = scrub.scrub(str(source or ""), scrub_pii=True)
    row = {"target": target, "event": event, "weight": _safe_weight(weight),
           "source": src, "at": _now()}
    try:
        store.append_record(shard, row, store.load_config(cwd))
        return True
    except Exception:
        return False


def load_stats(cwd=None, project=None):
    """target -> {'confirmed': w, 'contradicted': w, 'eroded': w} (sum of event weights)."""
    if project:
        if not store.validate_project_key(project):   # never let a traversal escape STORE_ROOT
            return {}
        cd = store.STORE_ROOT / "projects" / project / "calibration"
    else:
        cd = store.calibration_dir(cwd or os.getcwd())
    stats = {}
    if not cd.exists() or store.is_reparse_point(cd):
        return stats
    # _safe_iterdir (RC5L-H), not glob(): FOLLOWED a junctioned calibration/ (federated project=
    # checks only the <project> component, not this subdir; the default cwd branch had no check at
    # all) and enumerated its external shards into the calibration stats/trajectory.
    for f in sorted(p for p in store._safe_iterdir(cd, store.STORE_ROOT) if p.suffix == ".jsonl"):
        try:
            # no-follow read (RC5L-H), errors="replace" preserved: a single bad byte ANYWHERE in the
            # shard used to raise UnicodeDecodeError for the WHOLE read, and the broad except dropped
            # the entire FILE - every other row in it, hand-edited or not, silently vanished.
            # replace() confines the damage to the byte's own line (it becomes U+FFFD, which either
            # fails json.loads() below and is skipped as a bad ROW, or survives as a mangled target
            # the display oracle then withholds - either way the row-level polarity holds: junk
            # damages the row, never the file.
            lines = store._read_text_no_follow(f, store.STORE_ROOT, errors="replace").splitlines()
        except Exception:
            continue
        for ln in lines:
            ln = ln.strip()
            if not ln:
                continue
            try:
                o = json.loads(ln)
            except Exception:
                continue
            # target must be a STRING, not merely truthy: a shard is on disk and hand-editable, and
            # a JSON target of 123 became a numeric dict key that json.dumps renders as "123" -
            # colliding with a real "123" row into DUPLICATE keys - while a list target raised an
            # unhashable TypeError straight out of a read-only stats call. Both are shard-shaped
            # input reaching a structural decision; type-check at the parse seam.
            if not (isinstance(o, dict) and isinstance(o.get("target"), str) and o["target"]
                    and o.get("event") in _EVENTS):
                continue
            t = stats.setdefault(o["target"], {"confirmed": 0.0, "contradicted": 0.0, "eroded": 0.0})
            t[o["event"]] += _safe_weight(o.get("weight", 1.0))   # finite/bounded even if a shard was hand-edited
    return stats


def reliability(confirms, contradicts, eroded=0.0):
    """Beta-smoothed reliability in (0,1). Eroded events count as soft contradictions (1/2)."""
    pos = float(confirms)
    neg = float(contradicts) + 0.5 * float(eroded)
    return (_ALPHA + pos) / (_ALPHA + _BETA + pos + neg)


def trajectory(conviction_value, target, *, cwd=None, project=None, stats=None):
    """The full picture for one decision/scar - NOT collapsed to a single number."""
    if stats is None:
        stats = load_stats(cwd=cwd, project=project)
    s = stats.get(target, {"confirmed": 0.0, "contradicted": 0.0, "eroded": 0.0})
    rel = reliability(s["confirmed"], s["contradicted"], s.get("eroded", 0.0))
    try:
        conv = max(0.0, min(1.0, float(conviction_value)))
    except Exception:
        conv = 0.5
    return {
        "conviction_at_creation": round(conv, 3),
        "calibration": round(rel, 3),
        "confirmed": s["confirmed"], "contradicted": s["contradicted"], "eroded": s.get("eroded", 0.0),
        "effective_weight": round(conv * rel, 3),
    }


def effective_weight(conviction_value, target, *, cwd=None, project=None, stats=None):
    return trajectory(conviction_value, target, cwd=cwd, project=project, stats=stats)["effective_weight"]


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")   # Windows defaults cp1252; sources are UTF-8
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="Parsica Weave conviction calibration ledger.")
    ap.add_argument("--cwd", default=os.getcwd())
    ap.add_argument("--target", help="decision/scar id (did)")
    ap.add_argument("--event", choices=_EVENTS, help="record this event against --target")
    ap.add_argument("--source", default="", help="what produced the event (test name, file, ...)")
    ap.add_argument("--weight", type=float, default=1.0)
    ap.add_argument("--stats", action="store_true", help="print aggregated calibration stats")
    args = ap.parse_args()

    # recording an event is a durable write: an isolated (zero-footprint) session must refuse it.
    # --stats and every read path stay available (reads are always fine under isolation).
    if args.event and args.target and store.is_isolated():
        sys.stdout.write("parsica calibrate refused: PARSICA_ISOLATED=1 (isolated session, "
                         "zero footprint - nothing recorded)\n")
        sys.exit(3)

    if args.event and args.target:
        ok = record(args.cwd, args.target, args.event, source=args.source, weight=args.weight)
        # the receipt must never display what record() would refuse (round-4 4b): emit_json fences
        # the refusal union ONLY, and a rejected target is by definition refusal/PII/secret shaped -
        # including separator-hidden PII that is scrub-INVARIANT, which no emit-site re-scrub could
        # redact. Gate the echo on the SAME oracle the input boundary uses: an accepted target is
        # provably clean and echoes verbatim; a refused one is withheld. (Keyed on the oracle, not
        # on `ok`: a clean target whose append failed for an IO reason still echoes readably.)
        shown = args.target if _target_is_clean(args.target) else _WITHHELD
        store.emit_json({"recorded": ok, "target": shown, "event": args.event})
        return
    if args.stats:
        st = load_stats(args.cwd)
        if args.target:
            # Narrow to the asked-for target and let the SHARED loop below do the withholding.
            # The old special case substituted an all-zero row whenever the target was unclean,
            # on the reasoning that record() cannot write one - true for anything recorded TODAY,
            # but not for a shard written before the input boundary existed or hand-edited since.
            # For those the ledger really does hold rows, and the CLI answered "zero events" about
            # a target it could see: a fabricated fact, not a withheld one. Withholding hides the
            # NAME; it must never invent the COUNTS. One oracle, applied at the one emit seam.
            st = {args.target: st.get(args.target,
                                      {"confirmed": 0.0, "contradicted": 0.0, "eroded": 0.0})}
        # every emitted KEY passes the same oracle, including the keys that came off DISK rather
        # than off argv (convergence pass 1): a shard written before the M2/M3 input boundary
        # existed - or hand-edited - can hold a PII-shaped target, and emit_json's key fence is
        # refusal-only, so it would print verbatim. record() cannot produce such a target today,
        # so withholding it costs a legitimate row nothing. The disk-to-display twin of the two
        # argv echo sites; withheld rows are aggregated under one marker key rather than dropped,
        # so the operator still sees that something unshowable is on the ledger.
        out = {}
        for t, v in st.items():
            row = {**v, "reliability": round(reliability(v["confirmed"], v["contradicted"],
                                                         v.get("eroded", 0.0)), 3)}
            key = t if _target_is_clean(t) else _WITHHELD
            if key == _WITHHELD and key in out:
                for f in ("confirmed", "contradicted", "eroded"):
                    row[f] = out[key].get(f, 0.0) + row.get(f, 0.0)
                row["reliability"] = round(reliability(row["confirmed"], row["contradicted"],
                                                       row.get("eroded", 0.0)), 3)
            out[key] = row
        store.emit_json(out, indent=2)   # targets are keys; emit_json fences keys AND values
        return
    ap.print_help()


if __name__ == "__main__":
    main()
