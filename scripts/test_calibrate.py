#!/usr/bin/env python3
"""Tests for the conviction + calibration layer. Run: python test_calibrate.py

Covers: conviction-at-creation capture (clamp/structure/did), the NEVER-overwrite property
across re-enrichment, decision-text scrubbing, the calibration ledger math, and the
earned-conviction retrieval weighting + trajectory display."""
import io
import os
import sys
import json
import tempfile
import shutil
import argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _ns(**kw):
    d = dict(grep=None, file=None, cmd=None, tool=None, category=None)
    d.update(kw)
    return argparse.Namespace(**d)


def run():
    tmp = tempfile.mkdtemp()
    os.environ["PARSICA_WEAVE_ROOT"] = tmp
    import store, enrich, calibrate, query
    fails = []

    def ck(c, m):
        print(("ok  " if c else "FAIL ") + m)
        if not c:
            fails.append(m)

    # capture: conviction-at-creation (clamp, structure, did)
    e = enrich.build_enrich({"decisions": [{"text": "prefer --apply-stdin over --apply-file",
        "conviction": {"value": 1.7, "domain": "security", "why": "no pre-scrub temp file",
                       "would_weaken_if": "apply-file made secure"}}]})
    d = e["decisions"][0]
    ck(len(d.get("did", "")) == 12, "decision stamped with a stable did")
    ck(d["conviction"]["value"] == 1.0, "conviction value clamped to [0,1]")
    ck(d["conviction"]["kind"] == "conviction_at_creation" and d["conviction"]["domain"] == ["security"]
       and "at" in d["conviction"], "conviction structured + stamped at creation")

    # decision text passes the scrub-on-output guard
    e2 = enrich.build_enrich({"decisions": [{"text": "rotated the staging password to k7m2x9q4z8w3n6p1r5t0y2u8",
                                             "conviction": {"value": 0.8}}]})
    ck("k7m2x9q4z8w3n6p1r5t0y2u8" not in json.dumps(e2), "secret in a decision text scrubbed")

    # conviction is NEVER overwritten on re-enrichment
    cwd = tmp + "/proj"
    store.ensure_workspace(cwd)
    md = store.mirror_dir(cwd)
    (md / "2026-06-27.jsonl").write_text(json.dumps({"id": "c1", "ts": "2026-06-27T10:00:00Z",
        "sess": "s", "user": "x", "asst": "y"}) + "\n", encoding="utf-8")
    enrich.apply_enrichments(cwd, {"c1": {"decisions": [{"text": "use stdin", "conviction": {"value": 0.88}}]}})
    enrich.apply_enrichments(cwd, {"c1": {"decisions": [{"text": "use stdin", "conviction": {"value": 0.10}}]}})
    dec = enrich.load_overlay(cwd)["c1"]["decisions"][0]
    ck(dec["conviction"]["value"] == 0.88, "conviction preserved (never overwritten) across re-enrich")
    did = dec["did"]

    # calibration ledger math (Beta-smoothed, neutral prior)
    for _ in range(5):
        calibrate.record(cwd, did, "confirmed", source="test")
    calibrate.record(cwd, did, "contradicted", source="test")
    st = calibrate.load_stats(cwd)
    ck(st[did]["confirmed"] == 5 and st[did]["contradicted"] == 1, "ledger aggregates events")
    ck(abs(calibrate.reliability(5, 1) - 0.7) < 1e-6, "reliability = (a+c)/(a+b+c+x) = 0.7")
    tr = calibrate.trajectory(0.88, did, cwd=cwd)
    ck(abs(tr["effective_weight"] - 0.88 * 0.7) < 1e-3 and tr["conviction_at_creation"] == 0.88,
       "trajectory = conviction x calibration, kept as a trajectory")

    # earned-conviction ranking: confirmed decision outranks contradicted; both retrievable
    (md / "2026-06-27.jsonl").write_text(
        json.dumps({"id": "cHi", "ts": "2026-06-27T10:00:00Z", "sess": "s", "user": "a", "asst": "b"}) + "\n"
        + json.dumps({"id": "cLo", "ts": "2026-06-27T11:00:00Z", "sess": "s", "user": "a", "asst": "b"}) + "\n",
        encoding="utf-8")
    enrich.apply_enrichments(cwd, {
        "cHi": {"decisions": [{"text": "HI adopt overlay separation", "conviction": {"value": 0.9}}]},
        "cLo": {"decisions": [{"text": "LO adopt overlay separation", "conviction": {"value": 0.9}}]}})
    ov = enrich.load_overlay(cwd)
    for _ in range(8):
        calibrate.record(cwd, ov["cHi"]["decisions"][0]["did"], "confirmed")
    for _ in range(8):
        calibrate.record(cwd, ov["cLo"]["decisions"][0]["did"], "contradicted")
    cs = calibrate.load_stats(cwd)
    chunks = query._load_chunks(cwd=cwd)
    scored = {c["id"]: query._score(c, _ns(grep="adopt overlay separation"), cs) for c in chunks}
    ck(scored["cHi"][0] and scored["cLo"][0], "both decisions retrievable")
    ck(scored["cHi"][1] > scored["cLo"][1], "confirmed decision outranks contradicted (earned conviction)")
    out = query._fmt([c for c in chunks if c["id"] == "cHi"][0], True, cs)
    ck(all(k in out for k in ("conviction", "calibration", "effective")),
       "query --full shows the trajectory, not a collapsed number")

    # --- the ledger obeys scrub-before-store ---
    calibrate.record(cwd, "didsrctest", "confirmed", source="proved by password=hunter2Pass99")
    cd = store.calibration_dir(cwd)
    ledger = "".join(f.read_text(encoding="utf-8") for f in cd.glob("*.jsonl"))
    ck("hunter2Pass99" not in ledger, "calibration `source` is scrubbed before storage")
    ck(calibrate.record(cwd, "bad target has spaces", "confirmed") is False,
       "non-identifier target rejected (no free-form text smuggled onto the ledger)")
    # PII smuggled as a `target` id must be refused: target is a never-scrubbed join key, so a
    # phone/SSN/card would otherwise land on the ledger RAW. (F5) M3: each scrubber regex accepts
    # only the separator class its own shape is written in, so PII written with a separator outside
    # that class (all legal in the id charset) must ALSO be caught - via the separator-stripped
    # digit-stream check, which holds however those classes are later tuned. Both directions.
    _PII = ("123-45-6789", "4111111111111111", "5551234567", "1234567890123456",
            "4111.1111.1111.1111", "4111_1111_1111_1111", "4111:1111:1111:1111",   # M3: separators
            "123_45_6789", "123.45.6789", "555-1234567")
    for pii in _PII:
        ck(calibrate.record(cwd, pii, "confirmed") is False,
           "PII-shaped target refused, never written raw: %r" % pii)
    ledger2 = "".join(f.read_text(encoding="utf-8") for f in store.calibration_dir(cwd).glob("*.jsonl"))
    for pii in _PII:
        ck(pii not in ledger2, "no PII target reached the ledger: %r" % pii)
    # convergence-Eye MAJOR (M2 newline bypass): a trailing newline once slipped the id-shape gate
    # ($ matched before a final \n), and the stray \n then made norm non-isdigit and skipped the PII
    # length check, so a dotted/bare SSN/phone landed RAW. \Z admits no trailing newline; record()
    # returns False at the id-shape gate, BEFORE any write, so nothing lands.
    for pii in ("123.45.6789\n", "123456789\n", "5551234567\n", "4111.1111.1111.1111\n"):
        ck(calibrate.record(cwd, pii, "confirmed") is False,
           "a newline-terminated PII target is refused at the id-shape gate: %r" % pii)
    ledger_nl = "".join(f.read_text(encoding="utf-8") for f in store.calibration_dir(cwd).glob("*.jsonl"))
    ck("123.45.6789" not in ledger_nl and "4111.1111.1111.1111" not in ledger_nl,
       "no newline-smuggled PII reached the ledger")
    # FALSE-POSITIVE companion: legitimate machine ids must still record - the PII reject is not
    # over-broad. m6: a legit all-NUMERIC 12-char did (a ~0.4% outcome of a hex hash) is NOT a
    # standard PII length, so the old blanket isdigit()>=9 wrongly dropped it; it must PASS now.
    for good in ("a1b2c3d4e5f6", "didsrctest", "scar-2026-07-17", "123456789012", "12345678"):
        ck(calibrate.record(cwd, good, "confirmed") is True,
           "a legit id still records (PII reject is not over-broad): %r" % good)
    ck(calibrate.record(cwd, "didnan", "confirmed", weight=float("nan")) is True, "nan-weight event still records")
    snan = calibrate.load_stats(cwd).get("didnan", {})
    import math as _m
    ck(_m.isfinite(snan.get("confirmed", 0.0)), "nan weight coerced finite in stats")
    calibrate.record(cwd, "didbig", "confirmed", weight=999.0)
    ck(calibrate.load_stats(cwd).get("didbig", {}).get("confirmed") == 5.0, "over-range weight clamped to 5.0")

    # --- round-4 4b: the ECHO twins of the reject gate (the storage-vs-echo sibling) ---------
    # emit_json's fence is refusal-ONLY, so a rejected PII/secret --target would ride the receipt
    # raw - and a separator-hidden phone (M3) is scrub-INVARIANT, so re-scrubbing the echo could
    # not redact it either; withholding is the only safe display. Both echo sites (the record
    # receipt and the --stats key) gate on the SAME _target_is_clean oracle record() uses:
    # accepted -> verbatim, refused -> withheld. Both directions checked.
    import contextlib as _ctx
    import io as _io

    def _cli(*argv):
        buf = _io.StringIO()
        old_argv = sys.argv
        sys.argv = ["calibrate.py"] + list(argv)
        try:
            with _ctx.redirect_stdout(buf):
                calibrate.main()
        finally:
            sys.argv = old_argv
        return buf.getvalue()

    for pii in ("555.123.4567", "123.45.6789", "AKIAIOSFODNN7EXAMPLE", "123-45-6789"):
        out_r = _cli("--cwd", cwd, "--target", pii, "--event", "confirmed", "--source", "unit")
        ck(pii not in out_r and '"recorded": false' in out_r,
           "a rejected target never echoes raw in the receipt: %r" % pii)
        ck("<target-withheld>" in out_r, "the rejected receipt shows the withheld marker: %r" % pii)
        out_s = _cli("--cwd", cwd, "--target", pii, "--stats")
        ck(pii not in out_s and "<target-withheld>" in out_s,
           "a rejected target never becomes a raw --stats KEY: %r" % pii)
    # FALSE-POSITIVE companion: an accepted target echoes verbatim in BOTH sites (the oracle gates
    # the display, so the fix is provably a no-op for everything record() accepts).
    out_ok = _cli("--cwd", cwd, "--target", "a1b2c3d4e5f6", "--event", "confirmed", "--source", "unit")
    ck('"a1b2c3d4e5f6"' in out_ok and "withheld" not in out_ok,
       "an accepted target echoes verbatim in the receipt")
    out_oks = _cli("--cwd", cwd, "--target", "a1b2c3d4e5f6", "--stats")
    ck('"a1b2c3d4e5f6"' in out_oks and "withheld" not in out_oks,
       "an accepted target keys the --stats echo verbatim")

    # --- did is derived from SCRUBBED decision text, never a secret artifact ---
    import hashlib as _h
    canon = enrich._canonical_text("rotated the staging password to k7m2x9q4z8w3n6p1r5t0y2u8")
    ck("k7m2x9q4z8w3n6p1r5t0y2u8" not in canon, "decision text canonical-scrub removes the secret")
    e3 = enrich.build_enrich({"decisions": [{"text": "rotated the staging password to k7m2x9q4z8w3n6p1r5t0y2u8",
                                             "conviction": {"value": 0.7}}]})
    ck(e3["decisions"][0]["did"] == _h.sha1(canon.encode("utf-8")).hexdigest()[:12],
       "did = sha1(scrubbed text), not the raw secret-bearing sentence")

    # --- a missing conviction is flagged defaulted, not silently a real 0.5 ---
    dmiss = enrich.build_enrich({"decisions": [{"text": "a stance with no conviction supplied"}]})["decisions"][0]
    ck(dmiss["conviction"].get("defaulted") is True and dmiss["conviction"]["value"] == 0.5,
       "missing conviction -> defaulted flag (0.5 is a fallback, not a captured stance)")
    dgiven = enrich.build_enrich({"decisions": [{"text": "a stance with real conviction",
                                                 "conviction": {"value": 0.9}}]})["decisions"][0]
    ck("defaulted" not in dgiven["conviction"], "supplied conviction is NOT flagged defaulted")

    # --- NaN/Inf conviction is MISSING (defaulted 0.5), never max 1.0 ---
    dnan = enrich.build_enrich(json.loads('{"decisions":[{"text":"d nan","conviction":{"value":NaN}}]}'))["decisions"][0]
    ck(dnan["conviction"]["value"] == 0.5 and dnan["conviction"].get("defaulted") is True,
       "NaN conviction -> defaulted 0.5 (not coerced to max 1.0)")
    dinf = enrich.build_enrich(json.loads('{"decisions":[{"text":"d inf","conviction":{"value":Infinity}}]}'))["decisions"][0]
    ck(dinf["conviction"]["value"] == 0.5 and dinf["conviction"].get("defaulted") is True,
       "Infinity conviction -> defaulted 0.5")

    # --- a defaulted placeholder must NOT shadow a later REAL conviction ---
    cwd2 = tmp + "/proj2"
    store.ensure_workspace(cwd2)
    (store.mirror_dir(cwd2) / "2026-06-27.jsonl").write_text(
        json.dumps({"id": "d1", "ts": "2026-06-27T10:00:00Z", "sess": "s", "user": "x", "asst": "y"}) + "\n", encoding="utf-8")
    enrich.apply_enrichments(cwd2, {"d1": {"decisions": [{"text": "adopt the new flow"}]}})           # no conviction -> defaulted
    enrich.apply_enrichments(cwd2, {"d1": {"decisions": [{"text": "adopt the new flow",
                                                          "conviction": {"value": 0.95, "why": "load-bearing"}}]}})
    c = enrich.load_overlay(cwd2)["d1"]["decisions"][0]["conviction"]
    ck(c["value"] == 0.95 and not c.get("defaulted"), "defaulted placeholder REPLACED by a later real conviction")
    enrich.apply_enrichments(cwd2, {"d1": {"decisions": [{"text": "adopt the new flow", "conviction": {"value": 0.10}}]}})
    ck(enrich.load_overlay(cwd2)["d1"]["decisions"][0]["conviction"]["value"] == 0.95,
       "a REAL conviction is still never overwritten")

    # --- red counter reflects a redaction performed in decision TEXT ---
    e_red = enrich.build_enrich({"decisions": [{"text": "set api_key=sk-ant-AAAA1111BBBB2222CCCC3333DDDD in config"}]})
    ck(e_red.get("red", 0) >= 1 and "sk-ant-AAAA" not in json.dumps(e_red),
       "decision-text redaction counted in `red` and the secret is gone")

    # --- a concurrent defaulted shard must NOT shadow a real conviction at the JOIN ---
    cwd3 = tmp + "/proj3"
    store.ensure_workspace(cwd3)
    ed3 = store.enrich_dir(cwd3)
    DID = "abc123def456"
    # both shards: same (spec, at); the DEFAULTED one (run_id ffff...) wins the run_id tiebreak
    ed3.joinpath("2026-06-27.111.jsonl").write_text(json.dumps({"id": "k1", "enrich": {
        "spec": "ctx-enrich/v2", "at": "2026-06-27T12:00:00Z", "run_id": "000000000000",
        "decisions": [{"did": DID, "text": "ship", "conviction": {"kind": "conviction_at_creation",
                       "value": 0.9, "at": "2026-06-27T12:00:00Z"}}]}}) + "\n", encoding="utf-8")
    ed3.joinpath("2026-06-27.222.jsonl").write_text(json.dumps({"id": "k1", "enrich": {
        "spec": "ctx-enrich/v2", "at": "2026-06-27T12:00:00Z", "run_id": "ffffffffffff",
        "decisions": [{"did": DID, "text": "ship", "conviction": {"kind": "conviction_at_creation",
                       "value": 0.5, "defaulted": True, "at": "2026-06-27T12:00:00Z"}}]}}) + "\n", encoding="utf-8")
    conv3 = enrich.load_overlay(cwd3)["k1"]["decisions"][0]["conviction"]
    ck(conv3["value"] == 0.9 and not conv3.get("defaulted"),
       "concurrent defaulted shard does NOT shadow a real conviction (load_overlay back-fill)")

    # --- the back-fill is PER-CHUNK - a real conviction must NOT bleed across
    # two distinct chunks that share a decision-text hash (did) ---
    cwd4 = tmp + "/proj4"
    store.ensure_workspace(cwd4)
    (store.mirror_dir(cwd4) / "2026-06-27.jsonl").write_text(
        json.dumps({"id": "A", "ts": "2026-06-27T10:00:00Z", "sess": "s", "user": "a", "asst": "b"}) + "\n"
        + json.dumps({"id": "B", "ts": "2026-06-27T10:01:00Z", "sess": "s", "user": "c", "asst": "d"}) + "\n",
        encoding="utf-8")
    enrich.apply_enrichments(cwd4, {"A": {"decisions": [{"text": "ship it", "conviction": {"value": 0.97, "why": "A was sure"}}]}})
    enrich.apply_enrichments(cwd4, {"B": {"decisions": [{"text": "ship it"}]}})   # B supplies NONE -> defaulted
    bconv = enrich.load_overlay(cwd4)["B"]["decisions"][0]["conviction"]
    ck(bconv["value"] == 0.5 and bconv.get("defaulted") is True,
       "chunk A's real conviction does NOT bleed onto chunk B sharing the same did")

    # --- a malformed (non-dict) conviction must not crash retrieval ---
    bad = {"id": "bad", "ts": "2026-06-27T10:00:00Z", "sess": "s",
           "enrich": {"decisions": [{"text": "use stdin", "did": "d", "conviction": "bad"}]}}
    try:
        query._score(bad, _ns(grep="stdin"), {})
        query._fmt(bad, True, {})
        ck(True, "malformed (non-dict) conviction does not crash query._score / _fmt")
    except Exception as ex:
        ck(False, "malformed conviction crashed query: " + repr(ex))


    # --- convergence pass 2: a hand-edited shard must not shape the stats structure ------------
    # A calibration shard is a plain file on disk. A non-STRING target got past the truthiness
    # check: a number became a dict key that json.dumps renders as "123" (colliding with a real
    # "123" row into duplicate JSON keys), and a list raised an unhashable TypeError out of a
    # read-only stats call. Both directions are checked: the junk rows are dropped, the good row
    # in the SAME shard still counts.
    cwd5 = os.path.join(tmp, "calshard")
    os.makedirs(cwd5, exist_ok=True)
    store.ensure_workspace(cwd5)
    cdir = store.calibration_dir(cwd5)
    shard = cdir / "handedited.jsonl"
    rows = [
        {"target": 123, "event": "confirmed", "weight": 1.0},              # numeric key
        {"target": ["a", "b"], "event": "confirmed", "weight": 1.0},       # unhashable
        {"target": {"k": "v"}, "event": "confirmed", "weight": 1.0},       # unhashable
        {"target": None, "event": "confirmed", "weight": 1.0},
        {"target": "abc123def456", "event": "confirmed", "weight": 1.0},   # the one GOOD row
    ]
    shard.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    try:
        st5 = calibrate.load_stats(cwd5)
        ck(True, "a shard with non-string targets does not crash load_stats")
    except Exception as ex:
        st5 = {}
        ck(False, "non-string target crashed load_stats: " + repr(ex))
    ck(all(isinstance(k, str) for k in st5), "every stats key is a string (no numeric dict keys)")
    ck(st5.get("abc123def456", {}).get("confirmed") == 1.0,
       "the well-formed row in the same shard still counts (junk is dropped, not the file)")

    # --- convergence pass 2: --stats must never FABRICATE counts for a withheld target ---------
    # The withheld path substituted an all-zero row on the reasoning that record() cannot write an
    # unclean target - true today, but a pre-boundary or hand-edited shard can hold one, and the
    # CLI then reported "zero events" about rows it could see. Withholding hides the NAME; the
    # COUNTS must stay true. Both directions: the real count surfaces under the marker, and the
    # unclean name itself never does.
    dirty = "5551234567"                       # phone-shaped: the shared oracle refuses it
    shard2 = cdir / "preboundary.jsonl"
    shard2.write_text(json.dumps({"target": dirty, "event": "confirmed", "weight": 3.0}) + "\n",
                      encoding="utf-8")
    ck(not calibrate._target_is_clean(dirty), "the fixture target really is refused by the oracle")
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        sys.argv = ["calibrate.py", "--cwd", cwd5, "--stats", "--target", dirty]
        calibrate.main()
    finally:
        sys.stdout = old
    shown = buf.getvalue()
    ck(dirty not in shown, "a refused target name is never echoed by --stats")
    parsed = json.loads(shown)
    ck(calibrate._WITHHELD in parsed, "the row is reported under the withheld marker, not dropped")
    ck(parsed.get(calibrate._WITHHELD, {}).get("confirmed") == 3.0,
       "the REAL on-disk count is reported - the zero-row fabrication is gone")

    # false-positive companion: a clean target still reports under its own name, unmolested
    buf = io.StringIO()
    sys.stdout = buf
    try:
        sys.argv = ["calibrate.py", "--cwd", cwd5, "--stats", "--target", "abc123def456"]
        calibrate.main()
    finally:
        sys.stdout = old
    parsed = json.loads(buf.getvalue())
    ck(parsed.get("abc123def456", {}).get("confirmed") == 1.0,
       "a clean target still reports under its own name with its true count")

    # --- convergence pass 3 Q4: withheld AGGREGATION across MORE THAN ONE unclean target ---------
    # The single-dirty-target case above proves the marker surfaces a real count. It does not prove
    # the accumulator at calibrate.py's --stats loop (the `if key == _WITHHELD and key in out` sum)
    # is correct once a SECOND and THIRD unclean target land in the same shard set - the finder's own
    # mutation (deleting that accumulator block) left every OTHER suite green, so this is the one
    # test that would have caught it. Three distinct dirty targets (all 10-digit, phone-shaped - each
    # rejected independently so they exercise three separate merge steps, not one) plus two clean
    # targets in the SAME cwd; the withheld bucket must sum to the exact total across all three dirty
    # rows and every clean row must still surface individually, unmolested.
    cwd6 = os.path.join(tmp, "calshard_multi")
    os.makedirs(cwd6, exist_ok=True)
    store.ensure_workspace(cwd6)
    cdir6 = store.calibration_dir(cwd6)
    dirty_targets = ["5551230001", "5551230002", "5551230003"]
    dirty_rows = [
        {"target": dirty_targets[0], "event": "confirmed", "weight": 2.0},
        {"target": dirty_targets[0], "event": "contradicted", "weight": 1.0},
        {"target": dirty_targets[1], "event": "confirmed", "weight": 3.0},
        {"target": dirty_targets[2], "event": "eroded", "weight": 0.5},
        {"target": dirty_targets[2], "event": "confirmed", "weight": 1.0},
    ]
    clean_rows = [
        {"target": "abc111111111", "event": "confirmed", "weight": 4.0},
        {"target": "abc222222222", "event": "contradicted", "weight": 2.0},
    ]
    (cdir6 / "multi.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in dirty_rows + clean_rows), encoding="utf-8")
    ck(all(not calibrate._target_is_clean(t) for t in dirty_targets),
       "all three fixture targets really are refused by the oracle")

    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        sys.argv = ["calibrate.py", "--cwd", cwd6, "--stats"]
        calibrate.main()
    finally:
        sys.stdout = old
    shown = buf.getvalue()
    for t in dirty_targets:
        ck(t not in shown, f"dirty target {t!r} is never echoed in the aggregated --stats output")
    parsed = json.loads(shown)

    expected_confirmed = 2.0 + 3.0 + 1.0     # rows 1, 3, 5 above
    expected_contradicted = 1.0              # row 2
    expected_eroded = 0.5                    # row 4
    withheld = parsed.get(calibrate._WITHHELD, {})
    ck(withheld.get("confirmed") == expected_confirmed,
       f"withheld bucket sums 'confirmed' across all 3 dirty targets exactly (got {withheld.get('confirmed')!r})")
    ck(withheld.get("contradicted") == expected_contradicted,
       "withheld bucket sums 'contradicted' across all 3 dirty targets exactly")
    ck(withheld.get("eroded") == expected_eroded,
       "withheld bucket sums 'eroded' across all 3 dirty targets exactly")

    ck(parsed.get("abc111111111", {}).get("confirmed") == 4.0,
       "a clean target's own row survives untouched alongside a withheld aggregate")
    ck(parsed.get("abc222222222", {}).get("contradicted") == 2.0,
       "the SECOND clean target's own row also survives untouched (no clean row lost)")
    ck(len(parsed) == 3, "exactly 3 keys reported: 2 clean targets + 1 withheld aggregate, no row lost")

    # --- convergence pass 3 Q5: a bad UTF-8 byte damages its own LINE, not the whole shard file ----
    # load_stats() read the shard with read_text(encoding='utf-8') inside a broad except-continue -
    # ANY decode error dropped the entire file, not just the malformed row, silently discarding every
    # other well-formed line in the SAME shard. The row-level polarity (junk dropped, file kept) was
    # already hardened above (the numeric/unhashable-target fixture); this proves the FILE level now
    # matches it, using a genuinely invalid UTF-8 byte (0xFF, which is invalid in every UTF-8
    # position) spliced into the middle of one line, sandwiched between two well-formed rows.
    cwd7 = os.path.join(tmp, "calshard_badbyte")
    os.makedirs(cwd7, exist_ok=True)
    store.ensure_workspace(cwd7)
    cdir7 = store.calibration_dir(cwd7)
    good_before = json.dumps({"target": "beforebadbyte01", "event": "confirmed", "weight": 1.0})
    good_after = json.dumps({"target": "afterbadbyte01", "event": "confirmed", "weight": 1.0})
    bad_line = b'{"target": "corrupt\xffline", "event": "confirmed", "weight": 1.0}'
    (cdir7 / "badbyte.jsonl").write_bytes(
        good_before.encode("utf-8") + b"\n" + bad_line + b"\n" + good_after.encode("utf-8") + b"\n")
    try:
        st7 = calibrate.load_stats(cwd7)
        ck(True, "a shard with one invalid UTF-8 byte does not crash load_stats")
    except Exception as ex:
        st7 = {}
        ck(False, "invalid UTF-8 byte crashed load_stats: " + repr(ex))
    ck(st7.get("beforebadbyte01", {}).get("confirmed") == 1.0,
       "the well-formed row BEFORE the bad byte still counts (file-level drop is gone)")
    ck(st7.get("afterbadbyte01", {}).get("confirmed") == 1.0,
       "the well-formed row AFTER the bad byte still counts (only its own line was damaged)")
    # The replaced byte (U+FFFD) does not break JSON syntax, so the corrupted row still PARSES - it
    # is not dropped by load_stats itself, only mangled. Its target string is exactly the shape the
    # display oracle exists to catch: U+FFFD falls outside _VALID_TARGET's charset, so it is
    # WITHHELD (not echoed raw) the same way the pre-existing dirty-target case is, and its weight
    # still folds into the withheld aggregate rather than vanishing - the row-level polarity (junk
    # damages the row, never the file, and a display-unsafe row is withheld, never dropped or leaked).
    mangled = [t for t in st7 if "corrupt" in t]
    ck(len(mangled) == 1, "the corrupted row survives parsing as its own (mangled) target")
    ck(mangled and not calibrate._target_is_clean(mangled[0]),
       "the mangled target (containing the U+FFFD replacement char) is refused by the display oracle")

    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        sys.argv = ["calibrate.py", "--cwd", cwd7, "--stats"]
        calibrate.main()
    finally:
        sys.stdout = old
    shown7 = buf.getvalue()
    ck("corrupt" not in shown7, "the mangled target is never echoed raw by --stats")
    parsed7 = json.loads(shown7)
    ck(parsed7.get(calibrate._WITHHELD, {}).get("confirmed") == 1.0,
       "the mangled row's weight is folded into the withheld aggregate, not silently lost")
    ck(parsed7.get("beforebadbyte01", {}).get("confirmed") == 1.0
       and parsed7.get("afterbadbyte01", {}).get("confirmed") == 1.0,
       "both well-formed neighbor rows still surface individually in the SAME --stats output")

    shutil.rmtree(tmp, ignore_errors=True)
    if fails:
        print(f"\nFAIL - {len(fails)} check(s) failed")
        return 1
    print("\nOK - all conviction/calibration checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(run())
