#!/usr/bin/env python3
"""Tests for the Parsica Weave enrichment layer. Run: python test_enrich.py

Covers the scrub-on-output guard (incl. noun-proximity prose-secret probe), the
append-only sharded overlay (capture never rewritten), the overlay join + ranking,
versioning, and pruning. Includes adversarial regression cases."""
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
    import store, scrub, enrich, query
    fails = []

    def check(c, m):
        print(("ok  " if c else "FAIL ") + m)
        if not c:
            fails.append(m)

    def red(s):
        return enrich.build_enrich({"summary": s}).get("summary", "")

    # scrub-on-output: noun-proximity prose secrets redacted; prefixed tokens caught
    check("k7m2x9q4z8w3n6p1r5t0y2u8" not in red("the staging db password is k7m2x9q4z8w3n6p1r5t0y2u8"),
          "base36 secret near 'password' redacted")
    check("X9f2mQ8vReWp4" not in red("rotated the password X9f2mQ8vReWp4 today"),
          "short (13-char) secret near 'password' redacted")
    # the inverted rule - near a secret noun, redact unless code/hash evidence
    check("aB1cD-eF2gH-iJ3kL" not in red("the staging password is aB1cD-eF2gH-iJ3kL-mN4oP-qR5sT-uV6wX"),
          "hyphenated mixed-case secret near 'password' redacted")
    check("abc.def.ghi1234567890" not in red("the staging token is abc.def.ghi1234567890"),
          "dotted secret near 'token' redacted")
    check("deadbeefcafebabe0123456789abcdef" not in red("the password is deadbeefcafebabe0123456789abcdef"),
          "pure-hex secret near 'password' redacted")
    # a PUNCTUATION-bearing prose secret near a noun is redacted WHOLE - no
    # alnum prefix left behind once the suffix is masked (was: "a8F)" + "[REDACTED]").
    for s in ("the password is a8F)kQ2zR7wL1pM4", "the password is a8F!kQ2zR7wL1pM4Z",
              "the secret is a8F$kQ2zR7wL1pM4Z"):
        r = red(s)
        check("a8F" not in r and "kQ2zR7wL1pM4" not in r,
              "punctuation-bearing prose secret redacted whole (" + s.split(" is ")[0] + ")")

    # --- the opaque probe no longer keys on nearby WORDS ---
    # an incidental code word (commit/version/tag) near a real secret must NOT spare it (was a leak)
    check("Kp9mXq2Lw8Rt5Zv7Bn4" not in red("rotated the api_key to Kp9mXq2Lw8Rt5Zv7Bn4 after the commit"),
          "secret near incidental 'commit' word is STILL redacted (no window-word spare)")
    check("Zx8WqLm3Np6Rt9Bv2" not in red("the deploy secret is now Zx8WqLm3Np6Rt9Bv2 for this version"),
          "secret near incidental 'version' word redacted")
    check("Hk4Pj7Wn2Qm5Lx8aQ" not in red("set the auth token Hk4Pj7Wn2Qm5Lx8aQ on the release tag"),
          "secret near incidental 'tag' word redacted")
    # a 40-char git SHA / file path / uppercase hash digest near a secret noun MUST survive (priority #2)
    SHA = "3f7a9c2e8b1d4056a7f9c2e8b1d4056a7f9c2e8b"
    check(SHA in red("rotated the token; deploy is " + SHA), "40-char git SHA near 'token' kept")
    check(SHA in red("bumped the api_key handler; HEAD now at " + SHA), "git SHA near 'api_key' kept")
    check("/etc/app/secrets/store.enc" in red("the password material moved to /etc/app/secrets/store.enc"),
          "unix path near 'password' kept")
    check("C:/Users/me/AppData/creds.dat" in red("the token cache is C:/Users/me/AppData/creds.dat"),
          "windows path near 'token' kept")
    check("SHA256:abc123DEF456ghi789JKLmnoPQRst" in red("the private key fingerprint is SHA256:abc123DEF456ghi789JKLmnoPQRst"),
          "UPPERCASE SHA256: fingerprint kept (case-insensitive hash prefix)")
    # --- an opaque secret glued beside an in-place redaction marker is caught ---
    check("Qz4Xy7Wm2Lp9Rk5Tn8" not in red("the token https://h/x?sig=ABCDEF0123456789ABCD&u=Qz4Xy7Wm2Lp9Rk5Tn8 is set"),
          "opaque tail glued beside a [REDACTED:signature] marker is still redacted")
    check("[REDACTED:secret]" in red("rotated the api_key to Kp9mXq2Lw8Rt5Zv7Bn4 after the commit"),
          "a marker-free opaque secret near a noun still redacts normally")
    # --- marker branch judges the WHOLE residue (no per-/-segment shatter) ---
    check("Zk8Qm2Zx" not in red("password sig=[REDACTED]&u=Zk8Qm2Zx/7Lp4Rt8/Wn3Vb6Yc"),
          "a delimiter-bearing secret glued beside a marker is NOT shattered (still redacted)")
    check("~/Projects/app/secrets/keystore.json" in red("secret [REDACTED:secret]~/Projects/app/secrets/keystore.json"),
          "a file path glued beside a marker SURVIVES (not gutted by a '/' split)")
    # --- a live secret sandwiched BETWEEN two markers (a [..]-bracketed token that
    # would otherwise read as a placeholder) is redacted, and a slash-wrapped long secret is not a path ---
    check("LIVExy9kq2zr7wl1pm" not in red("password [REDACTED:anthropic_key]/LIVExy9kq2zr7wl1pm/[REDACTED:anthropic_key]"),
          "live secret between two markers redacted (double-marker token not read as placeholder)")
    check("/etc/app/secrets/store.enc" in red("the password material lives at /etc/app/secrets/store.enc"),
          "a real segment-validated path still survives near a noun (path check not over-tightened)")
    check("d383dc0e1f2a3b4c5d6e7f8091a2b3c4d5e6f708" in red("reverted to commit d383dc0e1f2a3b4c5d6e7f8091a2b3c4d5e6f708"),
          "SHA with 'commit' context word KEPT")
    check("sk-ant-api03-AAAA" not in red("set key sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAA"),
          "prefixed token caught by scrub")
    check("AKIAIOSFODNN7EXAMPLE" not in json.dumps(enrich.build_enrich({"facts": {"AKIAIOSFODNN7EXAMPLE": "x"}})),
          "secret in a dict KEY scrubbed")
    check("[CARD]" in json.dumps(enrich.build_enrich({"facts": [4111111111111111]})),
          "credit-card-as-int scrubbed")

    # precision: legit content kept (no secret noun, or clean identifier/hash shape)
    for tok in ("overlay_aware_ranking_v2_final_build_123",
                "sha512-Xk9fL2mQ8vReWpZaBcDeFgHiJ123abcDEF456",
                "feature/JIRA-1234-overlay-aware-ranking-fix"):
        check(tok in red("refactored " + tok + " cleanly"), "legit identifier/hash kept: " + tok[:24])
    check("d383dc0e1f2a3b4c5d6e7f8091a2b3c4d5e6f708" in red("reverted to commit d383dc0e1f2a3b4c5d6e7f8091a2b3c4d5e6f708"),
          "git SHA in prose kept")
    check("overlay_aware_ranking_v2" in red("fixed the token validation in overlay_aware_ranking_v2 path"),
          "identifier next to a secret noun KEPT (not gutted)")
    check("abc123def456ghi789" in red("the cache key abc123def456ghi789 lookup"),
          "'cache key' (not a secret noun) kept")
    check(all(isinstance(x, str) for x in enrich.build_enrich({"topic": [{"d": 1}, 42]}).get("topic", [])),
          "list fields coerced to list[str]")

    # append-only sharded overlay: capture untouched, Stop append safe
    cwd = tmp + "/proj"
    store.ensure_workspace(cwd)
    md, ed = store.mirror_dir(cwd), store.enrich_dir(cwd)
    day = md / "2026-06-27.jsonl"
    day.write_text(json.dumps({"id": "A", "ts": "2026-06-27T10:00:00Z", "sess": "s",
                               "user": "auth tokens", "asst": "jwt exp", "tools": ["Bash"],
                               "skills": ["security-review"]}) + "\n", encoding="utf-8")
    before = day.read_text(encoding="utf-8")
    enrich.apply_enrichments(cwd, {"A": {"topic": ["authentication", "jwt"],
                                         "summary": "checked token expiry",
                                         "search_queries": ["why jwt.py checks exp"]}})
    check(day.read_text(encoding="utf-8") == before, "capture mirror file UNTOUCHED by enrich (race-free)")
    shards = list(ed.glob("*.jsonl"))
    check(len(shards) == 1 and "." in shards[0].stem, "overlay written to a per-pid shard")
    with day.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"id": "B", "ts": "2026-06-27T10:05:00Z", "sess": "s",
                             "user": "later", "asst": "x"}) + "\n")
    check([json.loads(l)["id"] for l in day.read_text(encoding="utf-8").splitlines() if l.strip()] == ["A", "B"],
          "concurrent Stop append preserved alongside enrich")

    # query join + ranking + categories
    A = [c for c in query._load_chunks(cwd=cwd) if c["id"] == "A"][0]
    check(A.get("enrich", {}).get("topic") == ["authentication", "jwt"], "query joins overlay onto chunk")
    check("security" in A.get("enrich", {}).get("categories", []), "categorize uses tools/skills")
    chunks = query._load_chunks(cwd=cwd)
    sc = sorted([(query._score(c, _ns(grep="jwt"))[1], c["id"]) for c in chunks
                 if query._score(c, _ns(grep="jwt"))[0]], reverse=True)
    check(sc and sc[0][1] == "A" and sc[0][0] > 1, "overlay hit outranks raw-text")

    # versioning: pending via overlay; newer-spec not re-offered
    check([p["id"] for p in enrich.list_pending(cwd)] == ["B"], "list_pending via overlay (un-enriched only)")

    # multi-shard dates: list_pending reads ALL of a date's shards before honoring the limit
    # (truncation at a DATE boundary), and enrich-then-relist converges on the full set
    cwd_lp = tmp + "/proj_lp"
    store.ensure_workspace(cwd_lp)
    md_lp = store.mirror_dir(cwd_lp)
    for pid, cid in ((100, "lp-a"), (9000, "lp-b"), (50, "lp-c")):
        (md_lp / f"2026-06-27.{pid}.jsonl").write_text(
            json.dumps({"id": cid, "ts": "2026-06-27T10:00:00Z", "sess": "s", "user": cid}) + "\n",
            encoding="utf-8")
    first = [p["id"] for p in enrich.list_pending(cwd_lp, limit=2)]
    check(len(first) == 2, "list_pending honors the limit over a multi-shard date")
    enrich.apply_enrichments(cwd_lp, {cid: {"summary": cid} for cid in first})
    second = [p["id"] for p in enrich.list_pending(cwd_lp, limit=2)]
    check(set(first) | set(second) == {"lp-a", "lp-b", "lp-c"},
          "list_pending converges across a date's shards (no chunk starved)")

    # tie-break by (spec, at): newer wins across shards
    for p in ed.glob("*.jsonl"):
        p.unlink()
    (ed / "2026-06-27.111.jsonl").write_text(json.dumps({"id": "A", "enrich": {"spec": "ctx-enrich/v1", "at": "2026-06-27T09:00:00Z", "topic": ["older"]}}) + "\n", encoding="utf-8")
    (ed / "2026-06-27.222.jsonl").write_text(json.dumps({"id": "A", "enrich": {"spec": "ctx-enrich/v1", "at": "2026-06-27T12:00:00Z", "topic": ["newer"]}}) + "\n", encoding="utf-8")
    check(enrich.load_overlay(cwd)["A"].get("topic") == ["newer"], "tie-break: newer `at` wins")

    # coercion: malformed overlay shape never crashes query
    (ed / "2026-06-27.333.jsonl").write_text(json.dumps({"id": "A", "enrich": {"spec": "ctx-enrich/v2", "at": "2026-06-27T13:00:00Z", "summary": ["x", "y"], "topic": "single"}}) + "\n", encoding="utf-8")
    ov = enrich.load_overlay(cwd)
    check(isinstance(ov["A"].get("summary"), str) and isinstance(ov["A"].get("topic"), list),
          "load_overlay shape-coerces an odd overlay")
    try:
        for c in query._load_chunks(cwd=cwd):
            query._score(c, _ns(grep="x"))
            query._fmt(c, True)
        check(True, "query never crashes on coerced/odd overlay")
    except Exception as ex:
        check(False, "query crashed: " + repr(ex))

    # rotation sweeps an orphan overlay shard for an out-of-window date - MOVED to
    # archive/, never unlinked (retention = rotation; the file count is conserved)
    (ed / "2020-01-01.999.jsonl").write_text(json.dumps({"id": "OLD", "enrich": {"spec": "ctx-enrich/v1"}}) + "\n", encoding="utf-8")
    from pathlib import Path as _P
    n_before = sum(1 for p in _P(tmp).rglob("*") if p.is_file())
    store.rotate_mirror(cwd, 14)
    check(not (ed / "2020-01-01.999.jsonl").exists(), "rotation sweeps orphan overlay shard out of the active set")
    check((ed / "archive" / "2020-01-01.999.jsonl").exists(), "rotated overlay shard MOVED to enrich/archive/ (never unlinked)")
    check(sum(1 for p in _P(tmp).rglob("*") if p.is_file()) == n_before,
          "rotation conserves the store's file count (nothing deleted)")

    shutil.rmtree(tmp, ignore_errors=True)
    if fails:
        print(f"\nFAIL - {len(fails)} check(s) failed")
        return 1
    print("\nOK - all enrichment checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(run())
