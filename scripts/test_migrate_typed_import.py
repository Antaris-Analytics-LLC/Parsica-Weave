"""RC5-L blocker 1b: legacy record datasets become a TYPED IMPORT through the write gateways.

The defect: mirror / enrich / calibration shards and the two metadata JSONs were re-homed by a raw
file MOVE, so whatever the legacy file happened to hold landed live and unread. The sharpest
confirmed harm is not the credential (the display sinks re-scrub, so it is largely caught at read)
but the HUMAN-ONLY BYPASS: that fence is write-time only, with no read-side filter on query.py or
the SessionStart inject, so a moved chunk carrying [[human-only]] was queried AND injected.

These tests drive the REAL CLI as a subprocess, then recursively sweep the COMPLETE destination
tree for sentinels - not just the files the migration reported touching. A sentinel that only the
reported paths are checked for is a sentinel that hides in the path nobody thought to look at.
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

_HERE = Path(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, str(_HERE))

KEY = "legacyproj-0123456789"
# Distinct, greppable sentinels - one per thing that must NOT reach an indexed surface.
S_HUMAN = "ZZHUMANONLYPAYLOADZZ"
S_MALFORMED = "ZZMALFORMEDPAYLOADZZ"
S_NOTDICT = "ZZNOTADICTPAYLOADZZ"
S_OK = "ZZLEGITPAYLOADZZ"
S_STATE = "ZZSTATEVALUEZZ"


def _silo(tmp):
    """A legacy silo whose record datasets carry one of each hostile shape."""
    silo = Path(tmp) / KEY
    (silo / "mirror").mkdir(parents=True)
    (silo / "enrich").mkdir()
    (silo / "calibration").mkdir()
    (silo / "working.md").write_text("# Working set\n\n## Pinned facts\n- a fact\n", encoding="utf-8")

    lines = [
        json.dumps({"ts": "2026-07-01T00:00:00Z", "text": S_OK}),
        # the headline vector: the write gateway refuses this, a raw move did not
        json.dumps({"ts": "2026-07-01T00:00:01Z", "text": "[[human-only]] " + S_HUMAN}),
        "{ this is not json at all " + S_MALFORMED,
        json.dumps(["a list, not an object", S_NOTDICT]),
        "",
    ]
    (silo / "mirror" / "2026-07-01.4242.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (silo / "enrich" / "2026-07-01.4242.jsonl").write_text(
        json.dumps({"ts": "2026-07-01T00:00:02Z", "overlay": S_OK}) + "\n", encoding="utf-8")
    (silo / "calibration" / "2026-07-01.4242.jsonl").write_text(
        json.dumps({"target": "decision-1", "weight": 1.0}) + "\n", encoding="utf-8")
    (silo / ".state.json").write_text(json.dumps({"last": S_STATE}), encoding="utf-8")
    (silo / "config.json").write_text(json.dumps({"refusal": []}), encoding="utf-8")
    return silo


def _migrate(tmp):
    env = dict(os.environ, PARSICA_WEAVE_ROOT=str(tmp), PYTHONIOENCODING="utf-8")
    env.pop("PARSICA_ISOLATED", None)
    p = subprocess.run([sys.executable, str(_HERE / "pcx.py"), "migrate"],
                       capture_output=True, encoding="utf-8", errors="replace", env=env)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def _indexed_text(tmp):
    """Every byte on a surface that query or injection can reach: the workspace tree MINUS the
    legacy silo (preserved on purpose) and MINUS the quarantine area (refusal receipts).

    Deliberately a recursive sweep of everything else rather than a list of the files migrate said
    it wrote - the whole point is to catch a sentinel in a path nobody enumerated."""
    import store
    blob = []
    for p in sorted(Path(tmp).rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(tmp).as_posix()
        if rel.startswith(KEY + "/") or rel.startswith(store.QUARANTINE_DIRNAME + "/"):
            continue
        try:
            blob.append("%s\n%s" % (rel, p.read_text(encoding="utf-8", errors="replace")))
        except OSError:
            pass
    return "\n".join(blob)


@pytest.fixture()
def migrated():
    tmp = tempfile.mkdtemp(prefix="typedimport_")
    os.environ.setdefault("PARSICA_WEAVE_ROOT", tmp)
    silo = _silo(tmp)
    rc, out = _migrate(tmp)
    return tmp, silo, rc, out


# --- the headline: the human-only bypass is closed --------------------------------------------

def test_a_human_only_chunk_never_reaches_an_indexed_surface(migrated):
    tmp, silo, rc, out = migrated
    text = _indexed_text(tmp)
    assert S_OK in text, "the legitimate record must still land (this check is not vacuous)"
    assert S_HUMAN not in text, \
        "a [[human-only]] legacy chunk reached an indexed surface - the fence is write-time only " \
        "and nothing filters it on read, so this record would be queried AND injected"


def test_malformed_and_unsupported_records_never_reach_an_indexed_surface(migrated):
    tmp, silo, rc, out = migrated
    text = _indexed_text(tmp)
    for sentinel, shape in ((S_MALFORMED, "unparseable line"), (S_NOTDICT, "JSON that is not an object")):
        assert sentinel not in text, "%s was re-homed into the live store" % shape


def test_every_refusal_is_reported_and_the_original_is_preserved(migrated):
    tmp, silo, rc, out = migrated
    import store
    qdir = Path(tmp) / store.QUARANTINE_DIRNAME / KEY
    entries = []
    for q in sorted(qdir.rglob("*" + store.QUARANTINE_SUFFIX)):
        entries += [json.loads(l) for l in q.read_text(encoding="utf-8").splitlines() if l.strip()]
    reasons = " | ".join(e["reason"] for e in entries)
    assert len(entries) == 3, "expected 3 refusals (human-only, malformed, non-object): %r" % entries
    assert "human-only" in reasons and "not valid JSON" in reasons and "not a JSON object" in reasons

    # REFUSAL AND QUARANTINE, NEVER SILENT TRANSFORMATION: the receipt carries no payload, and the
    # legacy original is still on disk where the operator can read it.
    for e in entries:
        assert e["fingerprint"], "a receipt must be matchable back to its source line"
        for sentinel in (S_HUMAN, S_MALFORMED, S_NOTDICT):
            assert sentinel not in json.dumps(e), \
                "the quarantine receipt copied the refused payload into the store: %r" % e
    legacy = (silo / "mirror" / "2026-07-01.4242.jsonl.migrated").read_text(encoding="utf-8")
    for sentinel in (S_OK, S_HUMAN, S_MALFORMED, S_NOTDICT):
        assert sentinel in legacy, "the legacy original lost %s - nothing may be destroyed" % sentinel


# --- the quarantine area is genuinely outside every sweep, proven by DISCOVERY ------------------

def test_no_enumeration_surface_can_reach_the_quarantine_area(migrated):
    """DISCOVERED, not asserted. Every glob/rglob/iterdir in the shipped scripts is extracted from
    the SOURCE and checked against the quarantine layout, so a sweep added later that would pick it
    up fails here instead of silently indexing refused records.

    The two that actually constrain the design: query.py and store.py both iterate the mirror/ and
    projects/ roots treating EVERY subdirectory as a project key, which is why quarantine cannot
    live under either."""
    import re
    import store
    tmp, silo, rc, out = migrated
    qroot = Path(tmp) / store.QUARANTINE_DIRNAME
    assert qroot.is_dir(), "the run must actually have quarantined something"

    pats = set()
    for src in sorted(_HERE.glob("*.py")):
        if src.name.startswith("test_"):
            continue
        for m in re.finditer(r"\.r?glob\(\s*[\"']([^\"']+)[\"']", src.read_text(encoding="utf-8")):
            pats.add(m.group(1))
    assert pats, "the discovery found no globs at all - it is broken, not the code"

    qfiles = [p for p in qroot.rglob("*") if p.is_file()]
    assert qfiles, "no quarantine files to check"
    for pat in sorted(pats):
        for root in (Path(tmp), Path(tmp) / "mirror", Path(tmp) / "projects", qroot):
            if not root.exists():
                continue
            hits = [p for p in root.glob(pat) if p.is_file() and qroot in p.parents]
            assert not hits, "glob %r from a shipped script reaches quarantine: %r" % (pat, hits)

    # ...and no sweep may read the quarantine dir as a PROJECT KEY
    for root_name in ("mirror", "projects"):
        root = Path(tmp) / root_name
        if root.exists():
            assert store.QUARANTINE_DIRNAME not in [d.name for d in root.iterdir()], \
                "quarantine must not sit under %s/ - every subdir there is read as a project key" % root_name
    assert store.QUARANTINE_DIRNAME in store._WORKSPACE_DIRS, \
        "quarantine must be a known workspace dir or legacy_silos() may read it as an unmigrated silo"
    assert not store.legacy_silos() or all(
        s.name != store.QUARANTINE_DIRNAME for s in store.legacy_silos())


# --- the receipt, and the marker that depends on it --------------------------------------------

def test_a_versioned_migration_receipt_records_what_landed(migrated):
    tmp, silo, rc, out = migrated
    import store
    rp = silo / (store.MIGRATED_MARKER + ".receipt.json")
    assert rp.is_file(), "a completed migration writes its versioned receipt"
    rec = json.loads(rp.read_text(encoding="utf-8"))
    assert rec["version"] == store.MIGRATION_RECEIPT_VERSION and rec["key"] == KEY
    assert rec["datasets"]["mirror"] == {"imported": 1, "quarantined": 3}, rec["datasets"]
    assert rec["datasets"]["enrich"]["imported"] == 1
    assert (silo / store.MIGRATED_MARKER).is_file(), "a fully-landed silo is stamped done"
    for sentinel in (S_HUMAN, S_MALFORMED, S_NOTDICT):
        assert sentinel not in json.dumps(rec), "the receipt must carry counts, never payloads"


def test_a_refused_dataset_leaves_no_marker_so_a_rerun_cannot_short_circuit(tmp_path):
    """The migrate-verb twin of the merge-marker defect. A dataset refused outright (here: a
    junction pointing out of the silo) means the silo is NOT fully migrated, so the idempotency
    marker must not latch - otherwise every future run short-circuits past the refused data and
    the strand is permanent."""
    if os.name != "nt":
        pytest.skip("NTFS junctions are Windows-only")
    tmp = str(tmp_path)
    silo = _silo(tmp)
    # The junction target must live OUTSIDE the store root, or the sweep below finds the bait
    # sitting where the test planted it and reports an import that never happened.
    outside = Path(tempfile.mkdtemp(prefix="typedimport_outside_"))
    (outside / "STOLEN.jsonl").write_text(json.dumps({"text": "outside"}) + "\n", encoding="utf-8")
    r = subprocess.run(["cmd", "/c", "mklink", "/J", str(silo / "mirror" / "escape"), str(outside)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        pytest.skip("mklink /J unavailable")

    rc, out = _migrate(tmp)
    import store
    assert "REFUSED" in out and "NO MARKER" in out, out
    assert not (silo / store.MIGRATED_MARKER).exists(), \
        "a silo with a refused dataset must NOT be stamped migrated"
    assert (silo / (store.MIGRATED_MARKER + ".receipt.json")).is_file(), \
        "the receipt still records what did land, so the refusal is auditable"
    assert "STOLEN" not in _indexed_text(tmp), "a junction target was imported"


# --- false-positive companion ------------------------------------------------------------------

def test_a_clean_silo_migrates_completely_with_nothing_quarantined(tmp_path):
    """The direction that proves this is an import and not a wall: an ordinary silo lands every
    record, quarantines nothing, and is stamped done."""
    tmp = str(tmp_path)
    silo = Path(tmp) / KEY
    (silo / "mirror").mkdir(parents=True)
    (silo / "working.md").write_text("# Working set\n\n## Pinned facts\n- a fact\n", encoding="utf-8")
    (silo / "mirror" / "2026-07-01.4242.jsonl").write_text(
        "\n".join(json.dumps({"ts": "2026-07-01T00:00:0%dZ" % i, "text": "%s-%d" % (S_OK, i)})
                  for i in range(3)) + "\n", encoding="utf-8")

    rc, out = _migrate(tmp)
    import store
    assert "done: 1 migrated, 0 skipped" in out, out
    rec = json.loads((silo / (store.MIGRATED_MARKER + ".receipt.json")).read_text(encoding="utf-8"))
    assert rec["datasets"]["mirror"] == {"imported": 3, "quarantined": 0}, rec
    assert not (Path(tmp) / store.QUARANTINE_DIRNAME).exists(), \
        "a clean silo must not create a quarantine area at all"
    text = _indexed_text(tmp)
    for i in range(3):
        assert "%s-%d" % (S_OK, i) in text, "record %d did not land" % i


# --- convergence pass over RC5-L's own diffs ---------------------------------------------------
# Three defects the fixes above introduced or left open. All three live on the RECOVERY path - the
# re-run the refusal branch itself tells the operator to perform - which is why the original suite
# reached none of them: every test stopped at the first run.

def _refused_silo(tmp):
    """A silo whose mirror imports cleanly but whose calibration escapes via a junction, so the run
    refuses ONE dataset and takes the `failed` branch."""
    from test_migrate_containment import _junction
    silo = _silo(tmp)
    outside = Path(tempfile.mkdtemp(prefix="outside_"))
    (outside / "loot.jsonl").write_text(
        json.dumps({"ts": "2026-07-01T00:00:09Z", "text": "ZZSTOLENZZ"}) + "\n", encoding="utf-8")
    if not _junction(silo / "calibration" / "escape", outside):
        pytest.skip("mklink /J unavailable")
    return silo


def test_a_refused_silo_keeps_the_detection_signature_so_the_re_run_is_possible(tmp_path):
    """working.md is what legacy_silos() keys on. Moving it on a refused run destroyed the silo's
    only detection signature: the run printed "fix the cause and re-run", and the re-run answered
    "no legacy silos found - nothing to do". The refused dataset was then unreachable by the verb
    forever, and the instruction the tool itself printed was impossible to follow."""
    tmp = str(tmp_path)
    silo = _refused_silo(tmp)

    rc, out = _migrate(tmp)
    assert "REFUSED" in out, out
    assert "NO MARKER" in out, out
    assert (silo / "working.md").is_file(), \
        "the refused run moved working.md, erasing the signature legacy_silos() detects on"

    # the operator follows the printed instruction: remove the cause, re-run
    (silo / "calibration" / "escape").rmdir()
    rc2, out2 = _migrate(tmp)
    assert "nothing to do" not in out2, \
        "the re-run the tool told the operator to perform found no silo - the refused dataset is " \
        "stranded permanently:\n" + out2
    assert "done: 1 migrated" in out2, out2
    assert "ZZSTOLENZZ" not in _indexed_text(tmp)


def test_a_re_run_does_not_re_import_an_already_consumed_shard(tmp_path):
    """Discovery was extension-blind, so the `.migrated` suffix that makes a re-run safe in the
    doc-merge paths was not honored on the record path: the re-run re-imported every consumed shard
    into `<name>.jsonl.migrated`, a destination no `*.jsonl` glob reads, and renamed the source
    `.migrated.migrated` once per attempt."""
    tmp = str(tmp_path)
    silo = _refused_silo(tmp)
    rc, out = _migrate(tmp)
    assert "NO MARKER" in out, out

    mirror_dst = Path(tmp) / "mirror" / KEY
    before = sorted(p.name for p in mirror_dst.rglob("*") if p.is_file())
    assert any(n.endswith(".jsonl") for n in before), "nothing landed on the first run: " + out

    (silo / "calibration" / "escape").rmdir()
    rc2, out2 = _migrate(tmp)
    after = sorted(p.name for p in mirror_dst.rglob("*") if p.is_file())

    assert not [n for n in after if n.endswith(".migrated")], \
        "a consumed shard was re-imported into a *.jsonl.migrated destination, which no *.jsonl " \
        "glob can ever read - unqueryable dead weight in the store: %r" % after
    assert after == before, \
        "the re-run changed the destination shard set; a consumed shard was imported twice: " \
        "%r -> %r" % (before, after)
    assert not [p for p in (silo / "mirror").rglob("*.migrated.migrated")], \
        "the source picked up a second .migrated suffix, so every re-run stacks another"


def test_the_metadata_door_refuses_a_link_before_it_reads_and_keeps_the_run_alive(tmp_path):
    """The L1a boundary was applied to the tree walk but not to the two single-file metadata doors.
    `is_file()` FOLLOWS a link and reports True for its target, so a symlinked .state.json was read
    and COMMITTED into the store before any boundary saw it; the migrate_move that ran afterwards
    then raised MigrationRefused from inside no try block, aborting every remaining silo in the run
    - after the leaked write had already landed."""
    from test_migrate_containment import _junction
    tmp = str(tmp_path)
    silo = _silo(tmp)
    (silo / ".state.json").unlink()
    outside = Path(tempfile.mkdtemp(prefix="creds_"))
    (outside / ".state.json").write_text(json.dumps({"token": "ZZLEAKEDCREDZZ"}), encoding="utf-8")
    # a junction needs no privilege; the file-symlink twin is covered in test_migrate_containment
    if not _junction(silo / ".state.json", outside):
        pytest.skip("mklink /J unavailable")

    # a SECOND silo proves the run survives: an uncaught refusal aborted everything after it
    silo2 = Path(tmp) / "otherproj-9876543210"
    (silo2 / "mirror").mkdir(parents=True)
    (silo2 / "working.md").write_text("# Working set\n", encoding="utf-8")
    (silo2 / "mirror" / "2026-07-02.7777.jsonl").write_text(
        json.dumps({"ts": "2026-07-02T00:00:00Z", "text": "ZZSECONDSILOZZ"}) + "\n",
        encoding="utf-8")

    rc, out = _migrate(tmp)
    assert "Traceback" not in out, "the refusal escaped handle_migrate:\n" + out
    assert "REFUSED" in out, out
    text = _indexed_text(tmp)
    assert "ZZLEAKEDCREDZZ" not in text, \
        "an out-of-silo file was read through a link and committed into the store"
    assert "ZZSECONDSILOZZ" in text, \
        "the refusal aborted the whole run, leaving every later silo unmigrated"


def test_the_metadata_door_refuses_a_file_symlink_before_it_reads(tmp_path):
    """The read-and-COMMIT half of the same defect, which the junction vector above cannot reach.

    A junction can only target a DIRECTORY, so read_text() fails with OSError and the file
    quarantines - the door refuses for the wrong reason, and a mutation removing the containment
    check leaves that test green (verified: it did). Only a FILE symlink reaches the read, gets
    parsed as a valid JSON object, and is committed into the store. That needs admin or Developer
    Mode on Windows, so this skips explicitly rather than passing vacuously."""
    from test_migrate_containment import _symlink
    tmp = str(tmp_path)
    silo = _silo(tmp)
    (silo / ".state.json").unlink()
    outside = Path(tempfile.mkdtemp(prefix="creds_")) / "application_default_credentials.json"
    outside.write_text(json.dumps({"token": "ZZLEAKEDCREDZZ"}), encoding="utf-8")
    if not _symlink(silo / ".state.json", outside):
        pytest.skip("real symlinks need admin or Developer Mode (WinError 1314)")

    rc, out = _migrate(tmp)
    assert "Traceback" not in out, "the refusal escaped handle_migrate:\n" + out
    assert "REFUSED" in out, out
    assert "ZZLEAKEDCREDZZ" not in _indexed_text(tmp), \
        "a valid JSON object was read THROUGH a file symlink and committed into the store - an " \
        "out-of-silo secrets file is now inside a boundary whose whole point is that a legacy " \
        "path is untrusted"
    assert not (silo / ".state.json.migrated").exists(), \
        "the refused link was consumed anyway"
