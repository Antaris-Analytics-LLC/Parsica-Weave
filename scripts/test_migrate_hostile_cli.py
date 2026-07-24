"""The four migrate doors, exercised END TO END through the real `pcx.py migrate` CLI.

Round 6 found four category-1 defects in this verb, and every one of them lived in a door the
existing suites did not reach: they covered the typed JSONL datasets and the lower-level helpers,
so a raw `working.md` move, a self-redacting config, a junctioned silo ROOT and an unfenced durable
receipt all passed a green suite. A unit test of a helper cannot see a door that never calls it.
So this file drives the actual command in a subprocess and then reads the FILESYSTEM - the active
store, the receipt, and the legacy source - the way an operator would.

Every guard here ships with its false-positive twin. A fence that refuses everything passes an
attack test and destroys the product, so each refusal is paired with an ordinary-input case that
must still migrate, and the working-set pair additionally proves the content is now SCRUBBED (the
old raw rename could not scrub, so the credential sat on disk in the new tree).

Junctions need no privilege on Windows and are the load-bearing case: a junction reports
is_symlink() False and is traversed by rglob/os.walk. Real symlinks need admin/Developer Mode, so
they skip explicitly rather than passing vacuously.
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("PARSICA_WEAVE_ROOT", tempfile.mkdtemp(prefix="hostile_root_"))
os.environ.pop("PARSICA_ISOLATED", None)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store  # noqa: E402

WINDOWS = os.name == "nt"
PCX = Path(__file__).resolve().parent / "pcx.py"
KEY = "legacyproj-abcdef1234"
#: credential-SHAPED test vectors. Never a live secret - the point is the fence's judgement.
SECRET = "sk-ant-api03-" + "A" * 32
PHONE = "415-555-1212"
CODENAME = "AlphaCodename"


def _junction(link: Path, target: Path) -> bool:
    """NTFS directory junction - no privilege required. False when unavailable."""
    if not WINDOWS:
        return False
    target.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                       capture_output=True, text=True)
    return r.returncode == 0


def _migrate(root: Path):
    """Run the REAL verb. Returns (returncode, stdout).

    Asserts the CLI actually RAN: an argparse usage error exits non-zero and prints text that a
    loose grep cannot tell apart from a refusal, which is exactly how an earlier guard in this
    product passed while fully defanged."""
    env = dict(os.environ, PARSICA_WEAVE_ROOT=str(root),
               PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    env.pop("PARSICA_ISOLATED", None)
    r = subprocess.run([sys.executable, str(PCX), "migrate"],
                       capture_output=True, text=True, env=env)
    out = (r.stdout or "") + (r.stderr or "")
    assert "usage:" not in out and "unrecognized arguments" not in out, (
        "the CLI did not actually run - argparse rejected the invocation:\n%s" % out[:400])
    return r.returncode, out


def _silo(root: Path, working="# Working Set\n\n- a genuine pinned fact\n", key=KEY):
    """A legacy per-project silo: a dir under the store root holding working.md."""
    s = root / key
    s.mkdir(parents=True, exist_ok=True)
    (s / "working.md").write_text(working, encoding="utf-8")
    return s


def _active_working(root: Path, key=KEY) -> Path:
    return root / "working" / ("%s - Working Set.md" % key)


def _receipt(silo: Path) -> dict:
    p = silo / (store.MIGRATED_MARKER + ".receipt.json")
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}


# --------------------------------------------------------------------- W1: the silo ROOT
@pytest.mark.skipif(not WINDOWS, reason="NTFS junctions are Windows-only")
def test_a_junctioned_silo_root_is_refused_and_its_external_files_are_untouched(tmp_path):
    """The discovery boundary proved containment INSIDE a silo and took the root on trust.

    A junctioned root made every downstream proof vacuous: `silo/working.md` resolves through the
    junction to a real regular file, so nothing is a link, nothing refuses, and the files consumed
    live outside the store entirely."""
    root, outside = tmp_path / "store", tmp_path / "outside"
    root.mkdir()
    (outside).mkdir()
    (outside / "working.md").write_text("# external private notes\n", encoding="utf-8")
    (outside / "lessons.md").write_text("## an external lesson\n", encoding="utf-8")
    if not _junction(root / KEY, outside):
        pytest.skip("mklink /J unavailable")

    # The naive predicates really are blind - so this test cannot quietly stop testing anything
    # if the guard is ever "simplified" back to is_symlink().
    assert (root / KEY).is_symlink() is False, "a junction is not a symlink to Python"
    assert (root / KEY / "working.md").is_file() is True, "and it is transparently traversable"

    rc, out = _migrate(root)
    assert "REFUSED" in out, "a refused silo root must be NAMED, never silently skipped:\n%s" % out
    assert (outside / "working.md").is_file(), "the external file was consumed"
    assert (outside / "lessons.md").is_file(), "the external file was consumed"
    assert not _active_working(root).exists(), "external content was activated in the store"


def test_an_ordinary_silo_still_migrates_end_to_end(tmp_path):
    """The false-positive twin of every refusal in this file: the normal path must still work."""
    root = tmp_path / "store"
    root.mkdir()
    silo = _silo(root)
    rc, out = _migrate(root)
    assert rc == 0, out
    assert _active_working(root).is_file(), "an ordinary working set must land:\n%s" % out
    assert "a genuine pinned fact" in _active_working(root).read_text(encoding="utf-8")
    assert (silo / store.MIGRATED_MARKER).exists(), "a clean run must stamp the marker"


# --------------------------------------------------------------------- W2: working.md
def test_human_only_working_md_is_never_activated_or_injected(tmp_path):
    """working.md was the last raw MOVE in the verb, and it is the document SessionStart injects.

    A rename re-homes bytes no fence ever reads, so [[human-only]] content was activated - and
    since a rename cannot scrub, its credential sat raw on disk in the new tree too."""
    root = tmp_path / "store"
    root.mkdir()
    silo = _silo(root, working="[[human-only]] Call %s and use %s.\n" % (PHONE, SECRET))
    rc, out = _migrate(root)

    dst = _active_working(root)
    assert not dst.exists(), "human-only content was activated as the working set"
    assert "human-only fence" in out, "the refusal must be reported loudly:\n%s" % out
    assert (silo / "working.md").is_file(), "refused content must be LEFT IN PLACE, never unlinked"
    # and nothing anywhere in the active tree carries it
    for p in root.rglob("*"):
        if p.is_file() and p.suffix == ".md" and "working.md" not in str(p):
            body = p.read_text(encoding="utf-8", errors="ignore")
            assert SECRET not in body and "human-only" not in body, "leaked into %s" % p


def test_an_ordinary_working_set_lands_but_is_scrubbed_through_the_gateway(tmp_path):
    """The false-positive twin AND the proof the fix added scrubbing, not just refusal.

    Ordinary (non-human-only) content must still migrate - and because it now crosses write_doc
    rather than a rename, a credential inside it must be redacted on disk, which the old move
    could not do."""
    root = tmp_path / "store"
    root.mkdir()
    _silo(root, working="# Working Set\n\n- deploy key %s, ping %s\n" % (SECRET, PHONE))
    rc, out = _migrate(root)
    dst = _active_working(root)
    assert dst.is_file(), "ordinary content must still migrate:\n%s" % out
    body = dst.read_text(encoding="utf-8")
    assert SECRET not in body, "the gateway must scrub a credential it now sees"
    assert PHONE not in body, "the gateway must scrub PII it now sees"
    assert "deploy key" in body, "and must preserve the surrounding content"


# --------------------------------------------------------------------- W3: the config policy
def test_migrated_config_keeps_its_refusal_policy_intact(tmp_path):
    """A refusal term scrubbed by a policy carrying that same term redacts ITSELF.

    The migrated config came out {"refusal": ["<redacted>"]}: the fence survived in name only, and
    every later write refused the literal string `<redacted>` while the real codename wrote raw."""
    root = tmp_path / "store"
    root.mkdir()
    silo = _silo(root)
    (silo / "config.json").write_text(
        json.dumps({"refusal": [CODENAME], "human_only_terms": []}), encoding="utf-8")
    rc, out = _migrate(root)
    cfg = json.loads((root / "projects" / KEY / "config.json").read_text(encoding="utf-8"))
    assert cfg.get("refusal") == [CODENAME], (
        "the migrated policy must survive VERBATIM, got %r" % (cfg.get("refusal"),))


def test_migrated_config_still_scrubs_everything_that_is_not_policy(tmp_path):
    """The false-positive twin: the policy exemption must be precise, not a blanket bypass."""
    root = tmp_path / "store"
    root.mkdir()
    silo = _silo(root)
    (silo / "config.json").write_text(
        json.dumps({"refusal": [CODENAME], "agent_name": "reach me on %s" % PHONE}),
        encoding="utf-8")
    rc, out = _migrate(root)
    cfg = json.loads((root / "projects" / KEY / "config.json").read_text(encoding="utf-8"))
    assert cfg.get("refusal") == [CODENAME], "policy still verbatim"
    assert PHONE not in json.dumps(cfg), "a NON-policy field must still be scrubbed: %r" % cfg


def test_an_unusable_policy_term_is_dropped_and_reported(tmp_path):
    """Validated, not trusted - and never silently. A 1-character term would fence ordinary prose."""
    root = tmp_path / "store"
    root.mkdir()
    silo = _silo(root)
    (silo / "config.json").write_text(
        json.dumps({"refusal": ["x", CODENAME, 17]}), encoding="utf-8")
    rc, out = _migrate(root)
    cfg = json.loads((root / "projects" / KEY / "config.json").read_text(encoding="utf-8"))
    assert cfg.get("refusal") == [CODENAME], "only the usable term survives: %r" % (cfg,)
    assert "dropped" in out, "a shrunken policy must be reported, never silent:\n%s" % out


# --------------------------------------------------------------------- W4: the durable receipt
def test_the_durable_receipt_never_holds_a_raw_credential_shaped_filename(tmp_path):
    """The manifest LINE was fenced; the receipt FILE was not.

    A legacy directory can hold a file named for a credential, and the receipt recorded leftover
    names verbatim - so the display looked clean while the file on disk held the secret."""
    root = tmp_path / "store"
    root.mkdir()
    silo = _silo(root)
    mirror = silo / "mirror"
    mirror.mkdir()
    (mirror / "2026-07-01.jsonl").write_text("", encoding="utf-8")
    (mirror / (SECRET + ".txt")).write_text("stray\n", encoding="utf-8")
    rc, out = _migrate(root)

    raw = (silo / (store.MIGRATED_MARKER + ".receipt.json")).read_text(encoding="utf-8")
    assert SECRET not in raw, "the credential-shaped name reached the durable receipt"
    assert SECRET not in out, "and it must not reach the manifest either"


def test_the_receipt_still_records_what_happened(tmp_path):
    """The false-positive twin: sanitising the receipt must not empty it of audit value."""
    root = tmp_path / "store"
    root.mkdir()
    silo = _silo(root)
    mirror = silo / "mirror"
    mirror.mkdir()
    (mirror / "2026-07-01.jsonl").write_text("", encoding="utf-8")
    (mirror / (SECRET + ".txt")).write_text("stray\n", encoding="utf-8")
    rc, out = _migrate(root)

    rec = _receipt(silo)
    assert rec.get("version") and rec.get("key") == KEY, "receipt shape intact: %r" % rec
    mirror_row = rec.get("datasets", {}).get("mirror", {})
    assert mirror_row.get("skipped") == 1, "the COUNT must survive: %r" % mirror_row
    fps = mirror_row.get("skipped_fingerprints") or []
    assert len(fps) == 1 and fps[0] == store.name_fingerprint(SECRET + ".txt"), (
        "a fingerprint must still identify WHICH file was skipped: %r" % mirror_row)


# ------------------------------------------------- RC5L-C: containment BEFORE the read
#
# Round 6 put the containment proof on migrate_move at the TAIL of each doc step. That is after
# the read and after the workspace-wide append, so a linked source was read from outside the silo
# and committed into lessons.md / the journal - the documents SessionStart injects into every
# project and every session - and only then refused. Worse: the refusal was uncaught, so the run
# died on a traceback with no marker, and every re-run appended the same content again.

def _marker(silo: Path) -> bool:
    return (silo / store.MIGRATED_MARKER).exists()


@pytest.mark.skipif(not WINDOWS, reason="NTFS junctions are Windows-only")
def test_a_junctioned_journal_never_reaches_the_active_journal(tmp_path):
    """The no-admin variant, and the one that reproduced end to end: journal/ as a junction."""
    root, outside = tmp_path / "store", tmp_path / "outside_journal"
    root.mkdir()
    silo = _silo(root)
    outside.mkdir()
    (outside / "2026-07-20.md").write_text("EXTERNAL_JOURNAL_SENTINEL\n", encoding="utf-8")
    if not _junction(silo / "journal", outside):
        pytest.skip("mklink /J unavailable")

    rc, out = _migrate(root)

    assert "Traceback" not in out, "a refusal must be reported, not raised:\n%s" % out[:600]
    assert "REFUSED" in out, "the refused journal must be named:\n%s" % out
    active = root / "journal"
    landed = "".join(p.read_text(encoding="utf-8") for p in active.rglob("*.md")) if active.exists() else ""
    assert "EXTERNAL_JOURNAL_SENTINEL" not in landed, "external content reached the active journal"
    assert (outside / "2026-07-20.md").is_file(), "the external file was consumed"
    assert not _marker(silo), "a refused silo must not be marked done"


@pytest.mark.skipif(not WINDOWS, reason="NTFS junctions are Windows-only")
def test_a_refused_journal_does_not_accumulate_across_re_runs(tmp_path):
    """The persistence primitive: no marker + content already appended = unbounded re-injection.
    Measured at 3 copies after 3 runs before the fix."""
    root, outside = tmp_path / "store", tmp_path / "outside_journal"
    root.mkdir()
    silo = _silo(root)
    outside.mkdir()
    (outside / "2026-07-20.md").write_text("REPEAT_SENTINEL\n", encoding="utf-8")
    if not _junction(silo / "journal", outside):
        pytest.skip("mklink /J unavailable")

    for _ in range(3):
        _migrate(root)

    active = root / "journal"
    landed = "".join(p.read_text(encoding="utf-8") for p in active.rglob("*.md")) if active.exists() else ""
    assert landed.count("REPEAT_SENTINEL") == 0, (
        "external content accumulated across re-runs: %d copies" % landed.count("REPEAT_SENTINEL"))


def test_ordinary_lessons_journal_and_channels_still_migrate(tmp_path):
    """The false-positive twin for all three doc doors at once."""
    root = tmp_path / "store"
    root.mkdir()
    silo = _silo(root)
    (silo / "lessons.md").write_text("## a real scar\n\nwhat went wrong\n", encoding="utf-8")
    jd = silo / "journal"
    jd.mkdir()
    (jd / "2026-07-20.md").write_text("a real journal entry\n", encoding="utf-8")

    rc, out = _migrate(root)

    assert "Traceback" not in out, out[:600]
    assert (root / "lessons.md").is_file(), "ordinary lessons did not migrate:\n%s" % out
    assert "a real scar" in (root / "lessons.md").read_text(encoding="utf-8")
    active_page = root / "journal" / "2026-07-20.md"
    assert active_page.is_file(), "ordinary journal page did not migrate:\n%s" % out
    assert "a real journal entry" in active_page.read_text(encoding="utf-8")
    assert _marker(silo), "a clean silo must be marked done:\n%s" % out


# ------------------------------------------------- RC5L-C: active metadata is never clobbered

def _active_meta(root: Path, name: str, payload: dict, key=KEY) -> Path:
    p = root / "projects" / key / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


def test_an_active_config_is_never_overwritten_by_the_legacy_copy(tmp_path):
    """The worst finding of the round: ordinary use, no attacker, and it silently retired a live
    privacy control - fence_output() stopped redacting a term the operator had configured, with no
    copy of the destination surviving anywhere in the store."""
    root = tmp_path / "store"
    root.mkdir()
    silo = _silo(root)
    active = _active_meta(root, "config.json",
                          {"agent_name": "CurrentAgent", "refusal": ["CURRENT_POLICY_ZZZ"]})
    before = active.read_text(encoding="utf-8")
    (silo / "config.json").write_text(
        json.dumps({"agent_name": "LegacyAgent", "refusal": ["LEGACY_POLICY_QQQ"]}),
        encoding="utf-8")

    rc, out = _migrate(root)

    assert active.read_text(encoding="utf-8") == before, (
        "the operator's active policy was overwritten by the legacy copy")
    assert "REFUSED" in out and "config.json" in out, "the collision must be named:\n%s" % out
    assert "CURRENT_POLICY_ZZZ" not in out and "LEGACY_POLICY_QQQ" not in out, (
        "the refusal must not echo either file's contents:\n%s" % out)
    assert (silo / "config.json").is_file(), "the legacy source must be left in place"
    assert not _marker(silo), "a refused silo must stay rerunnable"


def test_an_active_state_file_wins_without_failing_the_silo(tmp_path):
    """.state.json is machine state and save_state() writes it during ordinary use, so a collision
    is the NORMAL case for any project touched before migrate ran. Refusing it would strand that
    silo permanently, so the active file simply wins and the run still completes."""
    root = tmp_path / "store"
    root.mkdir()
    silo = _silo(root)
    active = _active_meta(root, ".state.json",
                          {"cursors": {"ACTIVE_SESSION": 4242}, "turns": {"ACTIVE_SESSION": 17}})
    before = active.read_text(encoding="utf-8")
    (silo / ".state.json").write_text(json.dumps({"cursors": {"LEGACY": 1}}), encoding="utf-8")

    rc, out = _migrate(root)

    assert active.read_text(encoding="utf-8") == before, "active machine state was clobbered"
    assert "skip" in out.lower(), "the skip must be reported:\n%s" % out
    assert _marker(silo), "a state collision must NOT strand the silo:\n%s" % out


def test_fresh_metadata_still_imports_when_no_active_file_exists(tmp_path):
    """False-positive twin: the collision rules must not break the ordinary upgrade."""
    root = tmp_path / "store"
    root.mkdir()
    silo = _silo(root)
    (silo / "config.json").write_text(json.dumps({"agent_name": "Imported"}), encoding="utf-8")
    (silo / ".state.json").write_text(json.dumps({"cursors": {"s": 1}}), encoding="utf-8")

    rc, out = _migrate(root)

    dst = root / "projects" / KEY / "config.json"
    assert dst.is_file(), "a fresh config did not import:\n%s" % out
    assert json.loads(dst.read_text(encoding="utf-8"))["agent_name"] == "Imported"
    assert (root / "projects" / KEY / ".state.json").is_file(), "fresh state did not import"
    assert _marker(silo), "a clean silo must be marked done:\n%s" % out


# ------------------------------------------------- RC5L-C: keys and numbers are content too
#
# _scrub_record rebuilt every dict as {k: _walk(x)} and ended in `return v`, so a dict KEY rode
# through verbatim and every non-string scalar was handed back untouched. Its own docstring
# promised "a secret can never ride a reserved key onto disk" - a claim about VALUES under
# reserved keys, which a credential used AS a key walks straight past.
#
# The row is SCRUBBED rather than quarantined: discarding a user's genuine conversation because
# an attacker appended a key would lose their data to protect them from it. The secret is gone
# from the active tree either way, which is the property that matters.

def test_a_credential_shaped_key_and_numeric_pan_never_reach_the_active_mirror(tmp_path):
    root = tmp_path / "store"
    root.mkdir()
    silo = _silo(root)
    mirror = silo / "mirror"
    mirror.mkdir()
    row = {"ts": "2026-07-21T12:00:00Z", "sess": "s-0123456789abcdef",
           "id": "cid-0123456789abcdef", "user": "ordinary message",
           "asst": "ordinary response", SECRET: "key smuggling",
           "payment_card": 4111111111111111}
    (mirror / "2026-07-21.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")

    rc, out = _migrate(root)

    landed = "".join(p.read_text(encoding="utf-8")
                     for p in (root / "mirror").rglob("*.jsonl")) if (root / "mirror").exists() else ""
    assert landed, "the ordinary mirror row did not migrate at all:\n%s" % out
    assert SECRET not in landed, "a credential-shaped KEY reached the active mirror"
    assert "4111111111111111" not in landed, "a Luhn-valid PAN reached the active mirror as a number"
    assert SECRET not in out and "4111111111111111" not in out, "the operator output leaked it"
    # the user's own content is preserved - scrubbing the hostile parts must not cost them the row
    assert "ordinary message" in landed and "ordinary response" in landed


def test_ordinary_numbers_and_keys_survive_the_scrub(tmp_path):
    """False-positive twin: counts, ratios and flags are not PII and must stay typed."""
    root = tmp_path / "store"
    root.mkdir()
    silo = _silo(root)
    mirror = silo / "mirror"
    mirror.mkdir()
    row = {"ts": "2026-07-21T12:00:00Z", "sess": "s-0123456789abcdef",
           "id": "cid-9876543210fedcba", "user": "how many retries", "asst": "seven",
           "red": 7}
    (mirror / "2026-07-21.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")

    rc, out = _migrate(root)

    rows = [json.loads(l) for p in (root / "mirror").rglob("*.jsonl")
            for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert rows, "the ordinary row did not migrate:\n%s" % out
    got = rows[0]
    assert got.get("red") == 7, "an ordinary integer was mangled: %r" % got.get("red")
    assert got.get("id") == "cid-9876543210fedcba", "a reserved key's value was mangled"
    assert "how many retries" in json.dumps(got)
