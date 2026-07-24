#!/usr/bin/env python3
"""Native-memory at the Weave surface: display-time collapse + provenance + the boot import.

  * COLLAPSE (presentation only, never a score): the same content never prints twice in
    one result list - the promoted twin of a warm chunk (id-level), a durable hit
    repeating another durable hit, and a durable hit (nearly) contained in a warm chunk
    all fold to one copy, with the warm copy winning a contested slot ([both] tag).
  * PROVENANCE: a native-memory hit identifies itself - claude-native/<scope> + heading
    trail - and a hit from an older Spirit (no provenance keys) renders exactly as before.
  * BOOT IMPORT (needs parsica_spirit; SKIPs cleanly without it): the SessionStart import
    joins an EXISTING pool only (a boot never scaffolds), imports the instruction files
    read-only (byte-for-byte proof), skips under isolation, and honors the config off
    switch.

Run: python test_native_memory_display.py
"""
import io
import json
import os
import sys
import tempfile
from pathlib import Path

os.environ["PARSICA_WEAVE_ROOT"] = tempfile.mkdtemp(prefix="nm_root_")
os.environ.pop("PARSICA_ISOLATED", None)
SCRIPTS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPTS)
import store, pcx, query  # noqa: E402

fails = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        fails.append(msg)


def _hit(body, **kw):
    h = {"id": "h" + str(abs(hash(body)) % 99999), "kind": "record", "source_record_id": "",
         "content_hash": "", "body": body, "session": "", "created": ""}
    h.update(kw)
    return h


def _chunk(cid, user="", asst=""):
    return {"id": cid, "user": user, "asst": asst}


def collapse_unit():
    warm = [_chunk("c1", user="deploy question",
                   asst="Always run the smoke suite before deploying anywhere. That is the rule."),
            _chunk("c2", user="unrelated", asst="totally different topic entirely")]

    # (1) promoted twin: id-level collapse, warm wins + [both]
    kept, both = query._collapse_durables([_hit("anything", source_record_id="enriched:c1")], warm)
    check(kept == [] and both == {"c1"}, "COLLAPSE: promoted twin folds onto the warm chunk (id)")

    # (2) durable repeating durable: second copy never prints
    a = _hit("Always run the smoke suite before deploying anywhere.")
    b = _hit("Always run the smoke suite before deploying anywhere.")
    kept, both = query._collapse_durables([a, b], [])
    check(len(kept) == 1, "COLLAPSE: an exact durable duplicate folds to one copy")

    # (3) near-dup vs a warm chunk (the quoted-section case): warm wins the slot
    sec = _hit("Always run the smoke suite before deploying anywhere")
    kept, both = query._collapse_durables([sec], warm)
    check(kept == [] and both == {"c1"},
          "COLLAPSE: a section quoted inside a warm chunk folds onto it ([both])")

    # (4) genuinely distinct content survives
    fresh = _hit("The retry policy uses exponential backoff with jitter, capped at five.")
    kept, both = query._collapse_durables([fresh], warm)
    check(kept == [fresh] and not both, "COLLAPSE: distinct durable content prints")

    # (5) below-threshold similarity is NOT a duplicate
    near = _hit("Always ask before deleting anything anywhere, that is the other rule entirely.")
    kept, _ = query._collapse_durables([near], warm)
    check(kept == [near], "COLLAPSE: partial word overlap below the floor still prints")

    # (6) NEGATION-SAFE: a single polarity flip must never dedupe two contradictory rules.
    # durable-vs-durable (the silent-drop case): both must print.
    for pos, neg in [
        ("Deploy production on Friday after validation.",
         "Never deploy production on Friday after validation."),
        ("The feature flag must be enabled in production.",
         "The feature flag must not be enabled in production."),
        ("Always run migrations before the deploy.",
         "Never run migrations before the deploy."),
        ("Logging is enabled for the payment service.",
         "Logging is disabled for the payment service."),
    ]:
        kept, _ = query._collapse_durables([_hit(pos), _hit(neg)], [])
        check(len(kept) == 2, "COLLAPSE: a negation is not a duplicate (%r)" % neg.split()[0:2])

    # durable POS vs a warm chunk carrying the NEGATION: the durable survives (contradiction kept).
    warm_neg = [_chunk("cn", user="deploy?",
                       asst="Never deploy production on Friday after validation, per policy.")]
    d_pos = _hit("Deploy production on Friday after validation.")
    kept, both = query._collapse_durables([d_pos], warm_neg)
    check(kept == [d_pos] and not both,
          "COLLAPSE: a warm chunk that NEGATES a durable does not fold it away")

    # (7) a NUMBER/version/port difference is not a duplicate either
    for a, b in [("Bind the service to port 443.", "Bind the service to port 8443."),
                 ("Pin the client to schema v1.", "Pin the client to schema v2."),
                 ("Retain backups for 7 days.", "Retain backups for 30 days.")]:
        kept, _ = query._collapse_durables([_hit(a), _hit(b)], [])
        check(len(kept) == 2, "COLLAPSE: a changed number is not a duplicate (%r)" % b.split()[-1])

    # (8) but a genuine EXACT duplicate (same rule, two native files) still folds to one
    dup = "Run the whole test suite before any push to main."
    kept, _ = query._collapse_durables([_hit(dup), _hit(dup)], [])
    check(len(kept) == 1, "COLLAPSE: an exact-duplicate rule from two files still folds to one")

    # (9) SUBSTITUTION-SAFE (W5): a one-token antonym SWAP clears the overlap floor but is not a
    # negator or a number, so the prior heuristic folded it and hid the opposite instruction. It is
    # a two-sided difference (each side has a token the other lacks), so containment must keep BOTH.
    for a, b in [
        ("The system should retain archived records after the completed audit review finishes.",
         "The system should delete archived records after the completed audit review finishes."),
        ("Grant the intern read access to the production database for the duration of the audit.",
         "Deny the intern read access to the production database for the duration of the audit."),
        ("Include the staging endpoint in the nightly synthetic monitoring sweep run.",
         "Omit the staging endpoint in the nightly synthetic monitoring sweep run."),
    ]:
        kept, _ = query._collapse_durables([_hit(a), _hit(b)], [])
        check(len(kept) == 2, "COLLAPSE: an antonym substitution is not a duplicate (%r)" % b.split()[1])

    # (9b) and the TRUNCATION/quote case (one-sided difference) MUST still fold - the feature the
    # near-dup path exists for. The durable is a strict subset of the warm chunk that quotes it.
    quoted_warm = [_chunk("cq", user="what is the audit rule?",
                          asst="Per policy: The system should retain archived records after the "
                               "completed audit review finishes, and then notify the compliance lead.")]
    durable_sub = _hit("The system should retain archived records after the completed audit review finishes")
    kept, both = query._collapse_durables([durable_sub], quoted_warm)
    check(kept == [] and both == {"cq"},
          "COLLAPSE: a durable strictly contained in a warm quote still folds ([both])")

    # (9c) REORDERING-SAFE: identical token SETS in a DIFFERENT order are NOT a quote. Set containment
    # folded these (only_a and only_b both empty); contiguous-sequence containment keeps both, so a
    # reordered instruction can't hide behind a warm chunk that shares its vocabulary but not its text.
    reordered_warm = [_chunk("cr", user="ordering?",
                             asst="Restore the database before you restart the api gateway service.")]
    durable_reorder = _hit("Restart the api gateway service before you restore the database")
    kept, both = query._collapse_durables([durable_reorder], reordered_warm)
    check(kept == [durable_reorder] and not both,
          "COLLAPSE: same tokens in a different ORDER are not a quote - both stay on screen")

    # (9d) CONTRACTION-NEGATION: word tokenizing splits "don't" to {don, t}, so a contraction
    # negation used to slip past the polarity guard and fold behind its positive twin. _tok_seq now
    # expands n't -> "... not", surfacing the negation - the durable positive survives.
    for warm_asst, dur in [
        ("Reminder: don't deploy the billing service during the freeze window.",
         "Deploy the billing service during the freeze window."),
        ("Note that the nightly cache warmer won't run the reindex job on weekends.",
         "The nightly cache warmer will run the reindex job on weekends."),
    ]:
        warm_c = [_chunk("cc", user="policy?", asst=warm_asst)]
        kept, both = query._collapse_durables([_hit(dur)], warm_c)
        check(len(kept) == 1 and not both,
              "COLLAPSE: a contraction negation (%s) does not fold its positive twin" % warm_asst.split()[2])

    # and the SAME-polarity contraction quote MUST still fold (both carry the negation).
    same_pol_warm = [_chunk("cp", user="rule?",
                            asst="Per policy: don't deploy the billing service during the freeze window, ever.")]
    same_pol_dur = _hit("don't deploy the billing service during the freeze window")
    kept, both = query._collapse_durables([same_pol_dur], same_pol_warm)
    check(kept == [] and both == {"cp"},
          "COLLAPSE: a durable contained in a same-polarity contraction quote still folds ([both])")

    # (9e) REPEATED NEGATOR: a contradiction hides from a SET-based polarity check when the base text
    # already carries the negator token - "not deploy" vs "we can't not deploy" (double negative = DO
    # deploy) share the 'not' token, so a set symmetric difference masks the extra, inverting one. A
    # negator-COUNT check catches it (1 vs 2). The durable positive-of-a-negation survives.
    warm_dbl = [_chunk("cx", user="policy?",
                       asst="Reviewers agreed we can't not deploy the hotfix during the freeze.")]
    durable_single = _hit("not deploy the hotfix during the freeze")
    kept, both = query._collapse_durables([durable_single], warm_dbl)
    check(kept == [durable_single] and not both,
          "COLLAPSE: a repeated/inverting negator is not masked by a shared 'not' token")


def provenance_render():
    def _render(h, full=False):
        out = io.StringIO()
        old = sys.stdout
        sys.stdout = out
        try:
            query._print_durable(h, full)
        finally:
            sys.stdout = old
        return out.getvalue()

    native = _hit("Retries use exponential backoff.", source="claude-code-memory",
                  source_scope="user", heading_path="Rules / Retry policy",
                  kind="claude_md_instruction")
    text = _render(native)
    check("claude-native/user" in text and "Rules / Retry policy" in text,
          "PROVENANCE: a native hit renders claude-native/<scope> + its heading trail")

    legacy = {"kind": "lesson", "id": "x1", "body": "old shape hit"}   # pre-provenance Spirit
    text = _render(legacy)
    check("[durable] lesson" in text and "claude-native" not in text,
          "PROVENANCE: an older Spirit hit renders exactly the classic line")


def boot_import():
    try:
        from parsica_spirit import open_markdown
    except ImportError:
        print("SKIP boot-import checks: parsica_spirit not installed (pure-Weave env)")
        return

    home = Path(tempfile.mkdtemp(prefix="nm_home_"))
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "CLAUDE.md").write_text(
        "# User rules\n\n## Style\nPrefer plain language in every commit message.\n",
        encoding="utf-8")
    cwd = tempfile.mkdtemp(prefix="nm_proj_")
    (Path(cwd) / "CLAUDE.md").write_text(
        "# Project rules\n\n## Tests\nRun the whole suite before any push.\n", encoding="utf-8")

    spirit_base = Path(tempfile.mkdtemp(prefix="nm_spirit_"))
    os.environ["PARSICA_SPIRIT_ROOT"] = str(spirit_base)
    old_home = store.HOME
    store.HOME = home
    try:
        def _boot(**data):
            payload = {"cwd": cwd, "session_id": "sess1234"}
            payload.update(data)
            out = io.StringIO()
            old = sys.stdout
            sys.stdout = out
            try:
                pcx.handle_session_start(payload)
            finally:
                sys.stdout = old

        def _snap(paths):
            return {str(f): f.read_bytes() for p in paths for f in Path(p).rglob("*") if f.is_file()}

        sid = store.workspace_id(create=True)
        pool = spirit_base / sid

        # (1) no pool yet: the boot must not scaffold one
        _boot()
        check(not pool.exists(), "IMPORT: a boot never creates the pool (promote does)")

        # (2) pool exists: the boot imports, read-only on the sources
        open_markdown(str(pool))                      # the operator's pool (promote analog)
        before = _snap([home, cwd])
        _boot()
        ctx = open_markdown(str(pool), read_only=True)
        native = [r for r in ctx.all_records()
                  if (r.labels or {}).get("source") == "claude-code-memory"]
        scopes = {(r.labels or {}).get("source_scope") for r in native}
        check(len(native) >= 2 and {"user", "project"} <= scopes,
              "IMPORT: the boot imported the user + project instruction files")
        check(_snap([home, cwd]) == before, "IMPORT: source files stay byte-for-byte")

        # (3) idempotent: a second boot stores nothing new
        n = len(native)
        _boot()
        ctx = open_markdown(str(pool), read_only=True)
        check(len([r for r in ctx.all_records()
                   if (r.labels or {}).get("source") == "claude-code-memory"]) == n,
              "IMPORT: an unchanged boot is a no-op (idempotent)")

        # (4) isolation skips (a durable write); (5) the config switch turns it off
        (Path(cwd) / "CLAUDE.md").write_text(
            "# Project rules\n\n## Tests\nRun the whole suite before any push. Always.\n",
            encoding="utf-8")
        os.environ["PARSICA_ISOLATED"] = "1"
        try:
            _boot()
        finally:
            os.environ.pop("PARSICA_ISOLATED", None)
        ctx = open_markdown(str(pool), read_only=True)
        check(len([r for r in ctx.all_records()
                   if (r.labels or {}).get("source") == "claude-code-memory"]) == n,
              "IMPORT: isolation never imports (zero footprint)")

        store.ensure_workspace(cwd)
        json.dump({"native_memory_import": False},
                  open(store.project_dir(cwd) / "config.json", "w"))
        _boot()
        ctx = open_markdown(str(pool), read_only=True)
        check(len([r for r in ctx.all_records()
                   if (r.labels or {}).get("source") == "claude-code-memory"]) == n,
              "IMPORT: native_memory_import=false is a hard off switch")
    finally:
        store.HOME = old_home
        os.environ.pop("PARSICA_SPIRIT_ROOT", None)


def native_import_fence():
    """W1: the native import is a WRITE into the shared durable pool, so Weave's own fence must
    gate it - Spirit's write applies only the secret/PII scrub. Refusal terms are redacted and
    [[human-only]] / do-not-index content is dropped, at collection time, exactly like Weave's
    capture gateways. Uses FAKE sentinels. The assertion is the ACTIVE/retrievable surface
    (all_records content + labels): a forget soft-deletes to a recoverable, owner-only recycle
    bin excluded from every retrieval and output path, so what SURFACES is the fence's promise."""
    try:
        from parsica_spirit import open_markdown
    except ImportError:
        print("SKIP native-import-fence checks: parsica_spirit not installed (pure-Weave env)")
        return

    RZ, HZ = "ZZREFUSALZZ", "ZZHUMANZZ"
    home = Path(tempfile.mkdtemp(prefix="nmf_home_"))
    (home / ".claude").mkdir(parents=True)
    # The project DIRECTORY name embeds the refusal term too (a client-codename layout), so the
    # fence must cover the source_path provenance LABEL, not only the body/heading (W3).
    cwd = str(Path(tempfile.mkdtemp(prefix="nmf_")) / ("client-%s-proj" % RZ))
    os.makedirs(cwd)
    (Path(cwd) / "CLAUDE.md").write_text(
        "# Rules\n\nKeep %s private and never share it.\n\n"
        "## Private notes\n\n[[human-only]] %s must never be indexed.\n\n"
        "## Ops\n\nOrdinary operational text, no sentinels.\n" % (RZ, HZ), encoding="utf-8")

    spirit_base = Path(tempfile.mkdtemp(prefix="nmf_spirit_"))
    os.environ["PARSICA_SPIRIT_ROOT"] = str(spirit_base)
    old_home = store.HOME
    store.HOME = home

    def _active_hits(pool):
        ctx = open_markdown(str(pool), read_only=True)
        blob = "\n".join((r.content or "") + json.dumps(r.labels or {}) for r in ctx.all_records())
        return {RZ: blob.count(RZ), HZ: blob.count(HZ), "marker": blob.count("[[human-only]]")}

    def _native_n(pool):
        ctx = open_markdown(str(pool), read_only=True)
        return len([r for r in ctx.all_records()
                    if (r.labels or {}).get("source") == "claude-code-memory"])

    def _boot():
        out = io.StringIO()
        old = sys.stdout
        sys.stdout = out
        try:
            pcx.handle_session_start({"cwd": cwd, "session_id": "sessfnce"})
        finally:
            sys.stdout = old

    def _cfg(refusal, human_only):
        store.STORE_ROOT.mkdir(parents=True, exist_ok=True)
        (store.STORE_ROOT / "config.json").write_text(
            json.dumps({"refusal": refusal, "human_only_terms": human_only}), encoding="utf-8")

    try:
        sid = store.workspace_id(create=True)
        pool = spirit_base / sid
        open_markdown(str(pool))                          # operator's pool (promote analog)

        # (1) fresh fenced import: no sentinel reaches the retrievable surface, human-only dropped
        _cfg([RZ], [HZ])
        _boot()
        h = _active_hits(pool)
        check(h == {RZ: 0, HZ: 0, "marker": 0},
              "W1: refusal fenced + human-only dropped on a fresh native import (active surface)")
        check(_native_n(pool) >= 1, "W1: non-human-only sections still import")

        # W3 EXPLICIT: the term is also in the PROJECT PATH, so it rides source_path (a durable
        # provenance LABEL). Spirit scrubs secrets/PII in labels but not Weave's refusal union, so
        # the collection-time fence must cover source_path. Assert the label carries no raw term.
        ctx_w3 = open_markdown(str(pool), read_only=True)
        src_paths = " ".join((r.labels or {}).get("source_path", "") for r in ctx_w3.all_records())
        check(RZ not in src_paths,
              "W3: a refusal term in the project path is fenced from the source_path label")

        # (2) migration: a term added AFTER a raw import retires the stale copy from retrieval
        raw_pool = Path(tempfile.mkdtemp(prefix="nmf_pool2_")) / "p"
        raw_pool.parent.mkdir(parents=True, exist_ok=True)
        old_root = os.environ["PARSICA_SPIRIT_ROOT"]
        os.environ["PARSICA_SPIRIT_ROOT"] = str(raw_pool.parent)
        try:
            sid2 = store.workspace_id(create=True)        # same workspace id (already minted)
            pool2 = raw_pool.parent / sid2
            open_markdown(str(pool2))
            _cfg([], [])                                  # RC3-style: nothing fenced
            _boot()
            check(_active_hits(pool2)[RZ] >= 1, "W1: raw import contains the term (baseline)")
            _cfg([RZ], [HZ])                              # operator adds the fence LATER
            _boot()                                       # re-import must migrate
            hm = _active_hits(pool2)
            check(hm == {RZ: 0, HZ: 0, "marker": 0},
                  "W1: adding a fence term retires the stale raw copy from the active surface")
        finally:
            os.environ["PARSICA_SPIRIT_ROOT"] = old_root
    finally:
        store.HOME = old_home
        os.environ.pop("PARSICA_SPIRIT_ROOT", None)


if __name__ == "__main__":
    collapse_unit()
    provenance_render()
    boot_import()
    native_import_fence()
    print("\n" + ("ALL NATIVE-MEMORY DISPLAY TESTS PASSED (green)"
                  if not fails else "RED: %d failing -> %s" % (len(fails), " | ".join(fails))))
    sys.exit(1 if fails else 0)
