#!/usr/bin/env python3
"""Tests for `pcx.py migrate` - legacy per-project silos re-home into the workspace tree.
Run: python test_migrate.py

PRESERVE-EVERYTHING is the property under test: every move is a rename (collisions get
numbered siblings), every doc merge crosses the gateway with a retro stamp and consumes its
source by rename (never unlink), the marker is written dead last, a fence refusal leaves
content in place unindexed, and NOTHING in the store is ever deleted - the total file
count is conserved or grows, run after run."""
import io
import os
import re
import sys
import json
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def run():
    tmp = tempfile.mkdtemp()
    os.environ["PARSICA_WEAVE_ROOT"] = tmp
    import store
    import pcx
    import scrub
    fails = []

    def ck(c, m):
        print(("ok   " if c else "FAIL ") + m)
        if not c:
            fails.append(m)

    def _count():
        return sum(1 for p in Path(tmp).rglob("*") if p.is_file())

    def _migrate():
        old = sys.stdout
        sys.stdout = io.StringIO()
        try:
            pcx.handle_migrate(["migrate"])
            return sys.stdout.getvalue()
        finally:
            sys.stdout = old

    # --- empty store: loud no-op ----------------------------------------------------------
    out = _migrate()
    ck("no legacy silos found" in out, "empty store: migrate says so and does nothing")

    # --- build two fake legacy silos (the pre-workspace layout) ----------------------------
    KEY = "legacyproj-0123456789"
    silo = Path(tmp) / KEY
    (silo / "journal").mkdir(parents=True)
    (silo / "mirror").mkdir()
    (silo / "enrich").mkdir()
    (silo / "calibration").mkdir()
    (silo / "working.md").write_text(
        "# Working set\n\n## Pinned facts\n- migrated fact rides along\n\n"
        "## Open loops\n- close the migration\n", encoding="utf-8")
    (silo / "lessons.md").write_text(
        store.LESSONS_TEMPLATE + "\n## 2026-07-01 - hard lesson\nnever trust a snapshot\n"
        "\n## 2026-07-02 - second scar\nalways verify the pair\n",
        encoding="utf-8")
    (silo / "journal" / "2026-07-01.md").write_text(
        "## build day\nold journal body line\n", encoding="utf-8")
    (silo / "channels.json").write_text(json.dumps(
        {"channels": {"ch-1a2b3c4d": {"name": "lane", "created": "2026-07-01T00:00:00Z"}}}),
        encoding="utf-8")
    CHUNK = {"id": "m1", "ts": "2026-07-01T10:00:00Z", "sess": "oldsess1", "user": "old mirrored turn"}
    (silo / "mirror" / "2026-07-01.4242.jsonl").write_text(json.dumps(CHUNK) + "\n", encoding="utf-8")
    (silo / "enrich" / "2026-07-01.4242.jsonl").write_text(
        json.dumps({"id": "m1", "enrich": {"spec": "ctx-enrich/v2", "summary": "old overlay"}}) + "\n",
        encoding="utf-8")
    (silo / "calibration" / "2026-07-01.4242.jsonl").write_text(
        json.dumps({"target": "abc123def456", "event": "confirmed", "weight": 1.0,
                    "source": "t", "at": "2026-07-01T10:00:00Z"}) + "\n", encoding="utf-8")
    (silo / ".state.json").write_text(json.dumps({"cursors": {"tp:x.jsonl": 4}}), encoding="utf-8")
    (silo / "config.json").write_text(json.dumps({"checkpoint_every_n": 7}), encoding="utf-8")

    KEY2 = "fenced-abcdef0123"        # a silo whose lessons hit the human-only fence
    silo2 = Path(tmp) / KEY2
    silo2.mkdir()
    (silo2 / "working.md").write_text("## Pinned facts\n- fenced silo fact\n", encoding="utf-8")
    (silo2 / "lessons.md").write_text(
        store.LESSONS_TEMPLATE + "\n## scar\nabout FORBIDDENTOPIC internals\n", encoding="utf-8")
    (silo2 / "config.json").write_text(json.dumps({"human_only_terms": ["FORBIDDENTOPIC"]}),
                                       encoding="utf-8")

    # a workspace channel already named "lane" (different id) -> the import must suffix
    pre_lane = store.channel_id_for("lane")
    # a mirror-shard NAME collision at the destination -> the move must number, not clobber
    dst_md = store.STORE_ROOT / "mirror" / KEY
    dst_md.mkdir(parents=True)
    (dst_md / "2026-07-01.4242.jsonl").write_text("PRE-EXISTING SHARD\n", encoding="utf-8")

    # --- isolated sessions may not migrate (zero footprint) --------------------------------
    os.environ["PARSICA_ISOLATED"] = "1"
    iso_code, iso_out = None, ""
    try:
        old = sys.stdout
        sys.stdout = io.StringIO()
        try:
            pcx.handle_migrate(["migrate"])
        except SystemExit as ex:
            iso_code = ex.code
        finally:
            iso_out = sys.stdout.getvalue()
            sys.stdout = old
    finally:
        os.environ.pop("PARSICA_ISOLATED", None)
    ck(iso_code == 3 and "parsica migrate refused" in iso_out,
       "isolated migrate refuses with exit 3 and a clear line")
    ck((silo / "working.md").exists(), "isolated refusal migrated nothing")

    # --- the real run: everything re-homes, loud manifest, nothing deleted -----------------
    n_before = _count()
    out = _migrate()
    n_after = _count()
    ck("[parsica-weave] migrate: 2 legacy silo(s) found." in out,
       "manifest header counts the silos and carries no store-root path")
    ck(str(store.STORE_ROOT) not in out, "the header leaks no absolute store-root path")
    ck(("== %s ==" % KEY) in out and ("== %s ==" % KEY2) in out, "per-silo manifest sections")
    ck("done: 2 migrated, 0 skipped" in out, "footer counts the migrations")
    ck(n_after >= n_before, "nothing deleted anywhere: file count conserved or grown "
                            "(before %d, after %d)" % (n_before, n_after))

    # Mirror is a TYPED IMPORT now (RC5-L blocker 1), not a file move, so a name collision is no
    # longer resolved by parking a numbered sibling: the legacy RECORDS append into the destination
    # shard through append_record. Same invariant, checked one level down - the pre-existing bytes
    # are never clobbered, and the migrated record lands - but the unit is a record, not a file.
    # This matters beyond tidiness: the old numbered sibling ("<date>.<pid>.1.jsonl") was still
    # globbed and still date-parsed, so the records were live either way; what they had NOT passed
    # was the write gateway.
    _dst_shard = (dst_md / "2026-07-01.4242.jsonl").read_text(encoding="utf-8")
    ck(_dst_shard.startswith("PRE-EXISTING SHARD\n"),
       "a pre-existing destination shard is never clobbered")
    ck(json.dumps(CHUNK) in _dst_shard,
       "the migrated record is APPENDED into the destination shard, through the gateway")
    ck(not (dst_md / "2026-07-01.4242.1.jsonl").exists(),
       "a typed import appends records; it does not park a numbered sibling file")
    ck("import" in out and "record(s)" in out, "the manifest reports the import, not a move")

    # machine state re-homed under projects/<key>/
    pdir = store.STORE_ROOT / "projects" / KEY
    ck((pdir / "enrich" / "2026-07-01.4242.jsonl").exists(), "enrich overlay re-homed")
    ck((pdir / "calibration" / "2026-07-01.4242.jsonl").exists(), "calibration ledger re-homed")
    ck((pdir / ".state.json").exists() and (pdir / "config.json").exists(),
       ".state.json + config.json re-homed")

    # working set re-homed to its visible workspace name
    wdst = store.STORE_ROOT / "working" / ("%s - Working Set.md" % KEY)
    ck("migrated fact rides along" in wdst.read_text(encoding="utf-8"),
       "working.md re-homed to working/<key> - Working Set.md")

    # lessons merged with the retro stamp; the source consumed by RENAME (both copies live)
    lessons = store.lessons_path().read_text(encoding="utf-8")
    ck("never trust a snapshot" in lessons and ("*(migrated · %s)*" % KEY) in lessons,
       "lessons entries appended to workspace lessons.md with the retro stamp")
    # per-section stamping: EVERY migrated scar carries its own trailing stamp (the durable-tier
    # collector attributes each entry by its own stamp; a block-end stamp leaves earlier scars
    # unattributed and double-storable on cross-project promotes)
    ck(lessons.count("*(migrated · %s)*" % KEY) == 2,
       "every migrated section carries its own retro stamp (per-section, not block-end)")
    ck(lessons.split("never trust a snapshot", 1)[1].lstrip().startswith("*(migrated"),
       "the stamp directly follows each section body")
    ck("Self-authored" not in lessons,
       "the silo's template prefix is stripped on merge (no template text rides in)")
    ck((silo / "lessons.md.migrated").exists() and not (silo / "lessons.md").exists(),
       "the lessons source is consumed by rename, never unlinked")

    # journal merged under the migrated-from header into the SAME date's workspace page
    jpage = (store.journal_dir() / "2026-07-01.md").read_text(encoding="utf-8")
    ck(("### migrated from %s" % KEY) in jpage and "old journal body line" in jpage,
       "journal pages merge under '### migrated from <key>'")
    ck((silo / "journal" / "2026-07-01.md.migrated").exists(),
       "the journal source is consumed by rename")

    # channels merged: id unchanged, duplicate NAME suffixed, existing lane untouched
    reg = store.load_channels()["channels"]
    ck(reg.get("ch-1a2b3c4d", {}).get("name") == "lane (2)" and reg[pre_lane]["name"] == "lane",
       "channel id imported unchanged; duplicate name got ' (2)'")
    ck("channel ch-1a2b3c4d 'lane (2)' imported (renamed)" in out,
       "the channel rename is in the manifest")

    # fence refusal: policy, not error - reported, left in place, marker still written
    ck("human-only fence - left in place, never indexed" in out,
       "the fenced lessons merge is REFUSED loudly")
    ck((silo2 / "lessons.md").exists() and not (silo2 / "lessons.md.migrated").exists(),
       "fenced content stays in the silo, unconsumed")
    ck("FORBIDDENTOPIC" not in lessons, "the fenced term never reaches the workspace docs")
    ck((silo2 / store.MIGRATED_MARKER).exists(), "the fenced silo still completes its migration")

    # the marker is there and the legacy dirs remain (the user deletes, never the system)
    ck((silo / store.MIGRATED_MARKER).exists(), "marker written in the migrated silo")
    ck("marker  .migrated-to-workspace written" in out, "the marker is in the manifest")
    ck(silo.is_dir() and silo2.is_dir(), "legacy dirs are left in place")

    # --- idempotency ------------------------------------------------------------------------
    # working.md moved LAST, so a migrated silo drops out of detection naturally
    n_before2 = _count()
    out2 = _migrate()
    ck("no legacy silos found" in out2, "second run: migrated silos are no longer detected")
    ck(_count() == n_before2, "second run changes nothing (file count identical)")
    ck(store.lessons_path().read_text(encoding="utf-8").count("never trust a snapshot") == 1,
       "second run never re-appends a merged lesson")

    # a lingering working.md (crashed run / hand-restored) short-circuits on the marker
    (silo / "working.md").write_text("restored by hand\n", encoding="utf-8")
    out3 = _migrate()
    ck("skip: already migrated (marker present)" in out3 and "done: 0 migrated, 1 skipped" in out3,
       "marker short-circuits a re-detected silo")
    ck((silo / "working.md").exists(), "the marker skip moves nothing")

    # an unsafe silo name is skipped by hand-off, never joined onto the store paths
    bad = Path(tmp) / "bad key"
    bad.mkdir()
    (bad / "working.md").write_text("x\n", encoding="utf-8")
    out4 = _migrate()
    ck("not a safe project key" in out4 and "done: 0 migrated, 2 skipped" in out4,
       "an unsafe silo name is reported and skipped (migrate by hand)")
    ck((bad / "working.md").exists(), "the unsafe silo is left untouched")

    # --- a silo whose KEY carries an operator codename: the manifest fences it --------------
    # project_key scrubs a repo-dir name for secrets/PII only, NOT the refusal list, so a
    # codename survives into the legacy silo dir name and EVERY manifest line built from it
    # (== <key> ==, moved <path>, the marker). The output-fence sibling of the W2 channel line
    # and the W4 query error branches: _emit_migrate fences with the workspace union PLUS the
    # ALL-SILOS refusal union (every silo's terms loaded before the first line, since the silo
    # config has not reached projects/<key>/ where the output union would find it yet - so a
    # codename fenced in ANY silo is redacted in EVERY silo's line). (F7)
    RZK = "ZZFENCEDKEYZZ"                                    # fake refusal sentinel
    KEY3 = "client" + RZK + "app-abcdef0123"                 # a valid key that embeds the codename
    silo3 = Path(tmp) / KEY3
    (silo3 / "mirror").mkdir(parents=True)
    (silo3 / "mirror" / "2026-07-02.7777.jsonl").write_text(
        json.dumps({"id": "k1", "ts": "2026-07-02T10:00:00Z", "sess": "s3", "user": "t"}) + "\n",
        encoding="utf-8")
    (silo3 / "working.md").write_text("## Pinned facts\n- a fenced-key silo\n", encoding="utf-8")
    (silo3 / "config.json").write_text(json.dumps({"refusal": [RZK]}), encoding="utf-8")
    out5 = _migrate()
    ck(RZK not in out5, "a codename in the silo KEY never surfaces in the migrate manifest")
    ck("<redacted>" in out5, "the fenced key is redacted, not dropped (the line still prints)")
    ck((silo3 / store.MIGRATED_MARKER).exists(),
       "the fence is on the OUTPUT only - the silo still fully migrates")
    ck((store.STORE_ROOT / "mirror" / KEY3).is_dir(),
       "the migrated tree still uses the real key on disk; only the terminal echo is fenced")

    # --- round-4 3c: a SECRET-shaped legacy file name in the manifest ------------------------
    # fence_output is refusal-ONLY by contract, so a credential embedded in an untrusted legacy
    # path/file name (an old-version-minted key, a hand-placed file, an operator-named dir - the
    # current product's own minting cannot produce one) would survive it. _emit_migrate now runs
    # the FULL secret/PII scrub over every line at the one door; a normal product-shaped name
    # (slug + hash, ISO dates) is scrub-invariant and must print byte-identical.
    GHTOK = "ghp_1234567890abcdefABCDEF1234567890abcd"       # fake GitHub-token-shaped sentinel
    KEY4 = "tokensilo-abcdef9999"
    silo4 = Path(tmp) / KEY4
    (silo4 / "mirror").mkdir(parents=True)
    (silo4 / "mirror" / ("chat-%s.md" % GHTOK)).write_text("t\n", encoding="utf-8")
    (silo4 / "mirror" / "chat-normalname-2026-07-18.md").write_text("t\n", encoding="utf-8")
    (silo4 / "working.md").write_text("## Pinned facts\n- token-name silo\n", encoding="utf-8")
    out6 = _migrate()
    ck(GHTOK not in out6, "a secret-shaped legacy file name never surfaces raw in the manifest")
    ck("[REDACTED:" in out6, "the secret in the file name is redacted by the scrub, not dropped")
    ck("chat-normalname-2026-07-18.md" in out6,
       "a normal legacy file name prints byte-identical (the scrub is a no-op on clean names)")
    ck((silo4 / store.MIGRATED_MARKER).exists(),
       "the scrub is on the OUTPUT only - the token-named silo still fully migrates")

    # --- convergence MAJOR: the manifest fence must survive the scrub's normalization ----------
    # scrub() NFKC-folds and strips invisible characters; refusal terms are compiled from the
    # operator's config VERBATIM. Scrubbing BEFORE fencing therefore made a term typed full-width
    # (one IME toggle) or carrying a pasted zero-width character stop matching the folded line, and
    # the codename printed RAW where the pre-fix fence-only code printed <redacted> - a regression
    # introduced by the 3c fix. The fence now runs on BOTH sides of the scrub: before (terms as
    # configured) and after (terms exposed BY the folding). All THREE combinations are checked
    # here. Every non-ASCII vector is written as a \u ESCAPE, never a literal: the shipped tree is
    # plain ASCII and an auditable escape is the house form for a deliberate non-ASCII test vector.
    ZK = "ZZKRAKENZZ"                  # obviously-fake refusal sentinel (never a real codename)
    FW = ("\uff3a\uff3a\uff2b\uff32\uff21"      # full-width Z Z K R A
          "\uff2b\uff25\uff2e\uff3a\uff3a")     # full-width K E N Z Z -> folds to ZK
    KEY5 = "fwsilo-abcdef111111"
    silo5 = Path(tmp) / KEY5
    (silo5 / "mirror").mkdir(parents=True)
    (silo5 / "mirror" / ("plan-%s.md" % FW)).write_text("t\n", encoding="utf-8")
    (silo5 / "working.md").write_text("## Pinned facts\n- fullwidth silo\n", encoding="utf-8")
    (silo5 / "config.json").write_text(json.dumps({"refusal": [FW]}), encoding="utf-8")
    out7 = _migrate()
    ck(ZK not in out7 and FW not in out7,
       "a full-width refusal term is fenced even though the scrub folds it to ASCII")
    ck("<redacted>" in out7, "the folded term is redacted, not dropped")

    # the OTHER direction the two-sided fence buys: an ASCII term, content typed full-width to
    # EVADE it - the scrub folds the content and the second fence pass then catches it.
    KEY6 = "evasilo-abcdef222222"
    silo6 = Path(tmp) / KEY6
    (silo6 / "mirror").mkdir(parents=True)
    (silo6 / "mirror" / ("plan-%s.md" % FW)).write_text("t\n", encoding="utf-8")
    (silo6 / "working.md").write_text("## Pinned facts\n- evasion silo\n", encoding="utf-8")
    (silo6 / "config.json").write_text(json.dumps({"refusal": [ZK]}), encoding="utf-8")
    out8 = _migrate()
    ck(ZK not in out8, "full-width EVASION of an ASCII term is caught after the fold")

    # --- convergence pass-2 MAJOR: the THIRD combination - full-width TERM, ASCII content -----
    # The two-sided fence above covers ascii-term/folded-content and folded-term/folded-content,
    # but NOT a term typed full-width matched against plain-ASCII content: pass 1 compares the
    # full-width term to ASCII text (no match) and pass 2 compares it to the same ASCII text after
    # a fold that changes nothing (no match) - so the codename printed RAW. This is the IME-toggle
    # scenario the repair cites as its own motivation, and it closed only the half where the
    # CONTENT was also full-width. The fix normalizes the TERMS as well as the text.
    KEY7 = "fwtermsilo-abcdef333333"
    silo7 = Path(tmp) / KEY7
    (silo7 / "mirror").mkdir(parents=True)
    (silo7 / "mirror" / ("plan-%s.md" % ZK)).write_text("t\n", encoding="utf-8")   # ASCII content
    (silo7 / "working.md").write_text("## Pinned facts\n- fw-term silo\n", encoding="utf-8")
    (silo7 / "config.json").write_text(json.dumps({"refusal": [FW]}), encoding="utf-8")  # FW term
    out9 = _migrate()
    ck(ZK not in out9,
       "a FULL-WIDTH refusal term fences plain-ASCII content (the term is normalized too)")

    # --- convergence pass 4 MAJOR: the fold pass must cover the WIDER cross-project union too ----
    # fence_output() fences against store.workspace_refusal_terms() internally - EVERY project's
    # config, not just the migrate-scoped _MIGRATE_LINE_TERMS (global + the silo currently being
    # migrated). A term configured ONLY in an unrelated, already-migrated (or natively created)
    # project never entered _MIGRATE_LINE_TERMS, so its folded (full-width) spelling was never
    # added to `eff` - the exact bug KEY7 above closes for silo-scoped terms, left open one union
    # wider. Plant the term in a projects/<key>/config.json (never treated as a legacy silo -
    # legacy_silos() only scans STORE_ROOT's own top level, and "projects" is itself excluded).
    # Fresh sentinels (NOT ZK/FW): by this point in the run ZK/FW are already "burned" into
    # projects/*/config.json by KEY6/KEY7's OWN migrations re-homing their config.json - reusing
    # them made the first draft of this test a "cannot fail" test (it passed with the fix removed
    # too, fenced instead by a stale EXACT-ASCII match from KEY6's already-migrated config, not by
    # anything this fix does) - caught by mutation-testing per this session's own standing rule.
    ZK2 = "QQPHANTOMPROTOCOLQQ"
    FW2 = "".join(chr(ord(c) + 0xFEE0) if "A" <= c <= "Z" else c for c in ZK2)  # -> fullwidth
    otherproj = Path(tmp) / "projects" / "otherproj-9999999999"
    otherproj.mkdir(parents=True)
    (otherproj / "config.json").write_text(json.dumps({"refusal": [FW2]}), encoding="utf-8")
    KEY9 = "crossprojsilo-abcdef555555"
    silo9 = Path(tmp) / KEY9
    (silo9 / "mirror").mkdir(parents=True)
    (silo9 / "mirror" / ("plan-%s.md" % ZK2)).write_text("t\n", encoding="utf-8")  # ASCII content
    (silo9 / "working.md").write_text("## Pinned facts\n- cross-project silo\n", encoding="utf-8")
    (silo9 / "config.json").write_text(json.dumps({"refusal": []}), encoding="utf-8")  # not silo9's own term
    out_cross = _migrate()
    ck(ZK2 not in out_cross,
       "a full-width term configured in an UNRELATED project still fences THIS silo's ASCII content")

    # --- convergence pass 4 MINOR: a FORGED marker must not shield a live term from pass 2 -------
    # pass 2 trusted ANY scrub.MARKER_RE match as "already ours, skip it." Untrusted content can
    # embed a "[REDACTED:...]"-SHAPED bracket around a full-width codename: pass 1 misses it
    # (ASCII term vs full-width payload), the whole-line scrub folds the payload to ASCII, and the
    # forged bracket then reads exactly like a real marker to a naive MARKER_RE match - the
    # codename rides through untouched via m.group(0). _is_genuine_marker's SECRET_LABELS check
    # closes this: an unrecognized label inside "[REDACTED:...]" is treated as ordinary untrusted
    # content, not a marker to skip.
    # Direct unit call, not a real legacy file: "[REDACTED:...]"'s COLON is illegal in a Windows
    # filename (NTFS reads it as an alternate-data-stream separator), so routing this through a
    # real file on disk silently truncates the name at the colon and never reaches the exploit
    # path at all - the first draft of this test proved nothing on this platform for exactly that
    # reason (it passed identically with _is_genuine_marker fully defanged to `return True`,
    # caught only by mutation-testing per this session's own standing rule). _emit_migrate is a
    # general string transform with its own public signature; calling it directly with a crafted
    # line is the portable, precise way to exercise the forged-marker path.
    ZK3 = "RRSPECTERWATCHRR"
    FW3 = "".join(chr(ord(c) + 0xFEE0) if "A" <= c <= "Z" else c for c in ZK3)
    forged_line = ("  moved   mirror/plan-[REDACTED:x%sx].md -> "
                   "mirror/plan-[REDACTED:x%sx].md" % (FW3, FW3))
    buf = io.StringIO()
    old_stdout = sys.stdout
    sys.stdout = buf
    try:
        pcx._emit_migrate(forged_line, extra=[ZK3])
    finally:
        sys.stdout = old_stdout
    out_forged = buf.getvalue()
    ck(ZK3 not in out_forged,
       "a forged marker-shaped bracket around a full-width codename must not shield it: %r"
       % out_forged)

    # --- convergence pass-2 MINOR: the second pass must not redact INSIDE the marker ----------
    # apply_refusal has no marker protection and fence_output's "idempotent - safe to apply more
    # than once" contract is false for any refusal term that is a substring of the marker word:
    # a term "red" rewrote the <redacted> pass 1 had just emitted into "<<redacted>acted>", on
    # EVERY line of the run (the term union is all-silos). The fix splits on the marker so an
    # already-redacted span never re-enters. Checked as an invariant over the WHOLE manifest.
    KEY8 = "markersilo-abcdef444444"
    silo8 = Path(tmp) / KEY8
    (silo8 / "mirror").mkdir(parents=True)
    (silo8 / "mirror" / ("plan-%s.md" % ZK)).write_text("t\n", encoding="utf-8")
    (silo8 / "working.md").write_text("## Pinned facts\n- marker silo\n", encoding="utf-8")
    (silo8 / "config.json").write_text(json.dumps({"refusal": ["red", ZK]}), encoding="utf-8")
    out10 = _migrate()
    ck(ZK not in out10, "the real codename is still fenced alongside a marker-substring term")
    # STRUCTURAL, not word-based. The first cut of this assertion stripped the marker and then
    # looked for the word "redact" in what was left - which cannot fail, because the strip removes
    # exactly the evidence: "<<redacted>acted>" minus "<redacted>" is "<acted>". Worse for the other
    # families: "[EMAIL]" mangled to "[<redacted>IL]" leaves "[IL]", and "email" is not in it.
    # Assert instead that no bracket/angle span CONTAINS a marker fragment without BEING an intact
    # marker: a nested marker (the corruption signature) is caught, while an ordinary span like the
    # "[parsica-weave]" header is left alone because it holds no marker text. (convergence 3)
    def _mangled_markers(text):
        out = []
        for s in re.finditer(r"\[[^\]\n]*\]|<[^>\n]*>", text):
            span = s.group(0)
            if scrub.MARKER_RE.fullmatch(span):
                continue                                  # a clean marker
            inner = scrub.MARKER_RE.search(span)          # a marker nested INSIDE another span = corruption
            if inner or "redacted" in span.lower():
                out.append(span)
        return out

    mangled = _mangled_markers(out10)
    ck(not mangled, "every redaction marker is intact - no pass re-redacts inside one: %r" % mangled)
    ck(len(scrub.MARKER_RE.findall(out10)) >= 1, "markers are present (the check is not vacuous)")

    # --- convergence pass 3 MAJOR: the marker split must not blind the SECRET scrub --------------
    # A refusal term sitting INSIDE a credential-shaped legacy name plants a marker mid-assignment.
    # An earlier cut split the line on that marker and scrubbed each SEGMENT, so the assignment was
    # cut in half - left side too short to read as a secret, right side with no key - and the
    # credential printed VERBATIM where the pre-split door masked it. The scrub now sees the WHOLE
    # line and only the FENCE skips markers; the two run at different granularities on purpose.
    KEY9 = "credsilo-abcdef555555"
    silo9 = Path(tmp) / KEY9
    (silo9 / "mirror").mkdir(parents=True)
    (silo9 / "mirror" / ("password=abc%s123defghi789.md" % ZK)).write_text("t\n", encoding="utf-8")
    (silo9 / "working.md").write_text("## Pinned facts\n- cred silo\n", encoding="utf-8")
    (silo9 / "config.json").write_text(json.dumps({"refusal": [ZK]}), encoding="utf-8")
    out11 = _migrate()
    ck("[REDACTED:" in out11,
       "a credential is masked even when a refusal marker sits inside it (whole-line scrub)")
    ck(ZK not in out11, "the refusal term is still fenced on that same line")

    # --- convergence pass 3 MINOR: a folded term must stay SPECIFIC ------------------------------
    # NFKC can collapse a term to a generic one-character string (U+FF0E -> "."), which as a derived
    # spelling would redact every dot on every manifest line and destroy the audit trail. The
    # derived variant is gated on length + an alphanumeric; the term AS CONFIGURED is never gated.
    KEY10 = "dotsilo-abcdef666666"
    silo10 = Path(tmp) / KEY10
    (silo10 / "mirror").mkdir(parents=True)
    (silo10 / "mirror" / "2026-07-18.4242.jsonl").write_text("t\n", encoding="utf-8")
    (silo10 / "working.md").write_text("## Pinned facts\n- dot silo\n", encoding="utf-8")
    (silo10 / "config.json").write_text(json.dumps({"refusal": [chr(0xFF0E)]}), encoding="utf-8")
    out12 = _migrate()
    ck("2026-07-18.4242.jsonl" in out12,
       "a term that folds to '.' does not redact ordinary punctuation across the manifest")

    print("\nOK - migrate checks passed" if not fails
          else "\nRED: %d failing -> %s" % (len(fails), " | ".join(fails)))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(run())
