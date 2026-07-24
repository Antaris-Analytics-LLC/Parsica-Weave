"""RC5-L blocker 1, discovery boundary: migration refuses links and anything resolving outside the
silo, judged by lstat so refusal SEES the link rather than its target.

The Windows half is the load-bearing half. An NTFS directory JUNCTION (`mklink /J`) needs no admin
rights, reports Path.is_symlink() False and stat.S_ISLNK False and S_ISDIR True - and BOTH
Path.rglob() and os.walk(followlinks=False) traverse it and yield files from outside the tree as if
they were native. The reparse-point attribute is the only signal that sees it. These tests exist
because the obvious implementation of this guard is silently broken on the platform we ship on.

Junction tests need no privilege. Real-symlink tests DO (WinError 1314 without admin or Developer
Mode), so they skip explicitly rather than passing vacuously - a skip that reads as a pass is how a
positive control disappears from a CI matrix.
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("PARSICA_WEAVE_ROOT", tempfile.mkdtemp(prefix="mc_root_"))
os.environ.pop("PARSICA_ISOLATED", None)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store  # noqa: E402

WINDOWS = os.name == "nt"
SECRET = "sk-ant-api03-" + "a" * 40


@pytest.fixture()
def silo(tmp_path):
    """A legacy silo with one real file, and an OUTSIDE dir holding a credential to steal."""
    s = tmp_path / "silo"
    s.mkdir()
    (s / "real.md").write_text("legit content", encoding="utf-8")
    out = tmp_path / "outside"
    out.mkdir()
    (out / "STOLEN.md").write_text(SECRET, encoding="utf-8")
    return s


def _junction(link: Path, target: Path) -> bool:
    """Create an NTFS directory junction. Returns False if unavailable (non-Windows)."""
    if not WINDOWS:
        return False
    r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                       capture_output=True, text=True)
    return r.returncode == 0


def _symlink(link: Path, target: Path) -> bool:
    """Create a real symlink. Returns False when the platform refuses (no privilege)."""
    try:
        os.symlink(str(target), str(link), target_is_directory=target.is_dir())
        return True
    except (OSError, NotImplementedError, AttributeError):
        return False


# --- the junction family: no privilege required, and the reason this file exists ------------

@pytest.mark.skipif(not WINDOWS, reason="NTFS junctions are Windows-only")
def test_junction_inside_a_silo_is_refused_not_followed(silo):
    outside = silo.parent / "outside"
    if not _junction(silo / "escape", outside):
        pytest.skip("mklink /J unavailable")

    # First: prove the naive predicates really are blind, so this test cannot quietly stop
    # testing anything if someone "simplifies" the guard back to is_symlink().
    j = silo / "escape"
    assert j.is_symlink() is False, "a junction is not a symlink to Python"
    leaked = [p.name for p in silo.rglob("*") if p.is_file()]
    assert "STOLEN.md" in leaked, "rglob must still demonstrate the leak this guard prevents"

    assert store.is_reparse_point(j) is True
    with pytest.raises(store.MigrationRefused) as e:
        store.walk_contained(silo)
    assert "link" in str(e.value)
    assert SECRET not in str(e.value), "the refusal must not echo the content it refused"


@pytest.mark.skipif(not WINDOWS, reason="NTFS junctions are Windows-only")
def test_a_silo_root_that_is_itself_a_junction_is_refused(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    (real / "working.md").write_text("x", encoding="utf-8")
    root = tmp_path / "silo_link"
    if not _junction(root, real):
        pytest.skip("mklink /J unavailable")
    with pytest.raises(store.MigrationRefused):
        store.walk_contained(root)


@pytest.mark.skipif(not WINDOWS, reason="NTFS junctions are Windows-only")
def test_junction_pointing_INSIDE_the_silo_is_still_refused(silo):
    """The vector the containment check alone cannot catch, found by mutating is_reparse_point to
    the naive shape and noticing which tests survived: a junction whose target is an ANCESTOR
    resolves INSIDE the silo, so `within` says yes and only the reparse check says no. Without it
    the walker descends silo/loop/loop/loop... forever. Containment and link-refusal are two
    properties, not one - a guard that catches the escaping junction can still hang on this one."""
    if not _junction(silo / "loop", silo):
        pytest.skip("mklink /J unavailable")
    assert store.within(silo / "loop", silo) is True, "this vector IS contained - that is the point"
    with pytest.raises(store.MigrationRefused) as e:
        store.walk_contained(silo)
    assert "link" in str(e.value)


@pytest.mark.skipif(not WINDOWS, reason="NTFS junctions are Windows-only")
def test_migrate_tree_refuses_rather_than_importing_through_a_junction(silo, tmp_path):
    if not _junction(silo / "escape", silo.parent / "outside"):
        pytest.skip("mklink /J unavailable")
    dst = tmp_path / "dst"
    with pytest.raises(store.MigrationRefused):
        store.migrate_tree(silo, dst)
    # PRESERVE-FIRST: a refused migration leaves the legacy input exactly as found.
    assert (silo / "real.md").is_file(), "the legacy file was moved despite the refusal"
    assert (silo.parent / "outside" / "STOLEN.md").is_file()


# --- real symlinks: same contract, but the platform may not let us build the vector ---------

def test_symlinked_file_inside_a_silo_is_refused(silo, tmp_path):
    target = silo.parent / "outside" / "STOLEN.md"
    if not _symlink(silo / "notes.md", target):
        pytest.skip("symlink creation requires admin/Developer Mode on this platform")
    assert store.is_reparse_point(silo / "notes.md") is True
    with pytest.raises(store.MigrationRefused):
        store.walk_contained(silo)


# --- containment arithmetic ----------------------------------------------------------------

def test_within_is_not_a_prefix_test(tmp_path):
    root = tmp_path / "silo"
    root.mkdir()
    sibling = tmp_path / "silo2"          # shares the string prefix, is NOT inside
    sibling.mkdir()
    assert store.within(root / "a.md", root) is True
    assert store.within(sibling / "a.md", root) is False


def test_within_fails_closed_when_containment_cannot_be_proven(tmp_path):
    # commonpath raises ValueError across drives AND for UNC-vs-drive naming the same place.
    # Either way the answer must be "not contained", never an exception escaping to the caller.
    assert store.within("Z:\\elsewhere\\a.md" if WINDOWS else "/elsewhere/a.md", tmp_path) is False
    if WINDOWS:
        unc = "\\\\localhost\\C$\\Windows"
        assert store.within(unc, tmp_path) is False


def test_migrate_move_revalidates_at_the_effect(silo, tmp_path):
    """The move IS the write, so containment is re-proven there - a caller that reached this door
    by any route other than walk_contained still cannot move an outside file in."""
    outside_file = silo.parent / "outside" / "STOLEN.md"
    with pytest.raises(store.MigrationRefused):
        store.migrate_move(outside_file, tmp_path / "dst.md", root=silo)
    assert outside_file.is_file(), "the refused source must be left untouched"


def test_hardlink_is_contained_and_therefore_allowed(silo):
    """Documented boundary: a hardlink needs no admin on NTFS, but it is NOT a containment escape.
    The bytes are local, resolution never leaves the silo, and there is no redirect to follow. It
    is deliberately allowed; st_nlink is a provenance signal, not a containment one."""
    target = silo / "real.md"
    link = silo / "hard.md"
    try:
        os.link(str(target), str(link))
    except (OSError, NotImplementedError):
        pytest.skip("hardlinks unavailable here")
    assert store.is_reparse_point(link) is False
    assert sorted(p.name for p in store.walk_contained(silo)) == ["hard.md", "real.md"]


# --- false-positive companions: an ordinary silo must migrate exactly as before -------------

def test_clean_silo_walks_and_migrates_completely(tmp_path):
    s = tmp_path / "silo"
    (s / "mirror" / "deep").mkdir(parents=True)
    (s / "working.md").write_text("w", encoding="utf-8")
    (s / "mirror" / "a.jsonl").write_text("{}", encoding="utf-8")
    (s / "mirror" / "deep" / "b.jsonl").write_text("{}", encoding="utf-8")

    found = sorted(p.relative_to(s).as_posix() for p in store.walk_contained(s))
    assert found == ["mirror/a.jsonl", "mirror/deep/b.jsonl", "working.md"], found

    # dst-side containment (RC5L-H) checks against the real STORE_ROOT, not an arbitrary tmp_path -
    # every production caller constructs destinations under it, so the test must too.
    dst = store.STORE_ROOT / "clean_silo_dst"
    moved = store.migrate_tree(s, dst)
    assert len(moved) == 3
    assert sorted(p.relative_to(dst).as_posix() for _, p in moved) == found
    for _, landed in moved:
        assert landed.is_file(), "every discovered file must actually land"


# --- the import primitives re-prove containment AT THE EFFECT --------------------------------
# The single-file metadata doors never pass through walk_contained, so this is the ONLY boundary a
# linked .state.json meets. Tested at the primitive because the CLI route cannot discriminate on
# Windows: a junction targets a DIRECTORY, so read_text() fails with OSError and the file
# quarantines - the door refuses, but for the wrong reason, and a mutation deleting the containment
# check leaves the CLI test green (verified: it did). The refusal must arrive as MigrationRefused
# BEFORE the read, not as a quarantine receipt after it.

@pytest.fixture()
def cfg():
    return store.load_config()


def test_import_metadata_json_refuses_a_linked_source_before_reading(tmp_path, cfg):
    s = tmp_path / "silo"
    s.mkdir()
    out = tmp_path / "outside"
    out.mkdir()
    (out / "creds.json").write_text('{"token": "%s"}' % SECRET, encoding="utf-8")
    linked = s / ".state.json"
    if not (_junction(linked, out) or _symlink(linked, out / "creds.json")):
        pytest.skip("neither mklink /J nor os.symlink is available")

    dst = tmp_path / "dst" / ".state.json"
    with pytest.raises(store.MigrationRefused):
        store.import_metadata_json(linked, dst, "k", ".state.json", cfg, root=s)
    assert not dst.exists(), "the destination was written before the boundary refused"


def test_import_jsonl_refuses_a_linked_source_before_reading(tmp_path, cfg):
    s = tmp_path / "silo"
    s.mkdir()
    out = tmp_path / "outside"
    out.mkdir()
    (out / "loot.jsonl").write_text('{"text": "%s"}\n' % SECRET, encoding="utf-8")
    linked = s / "shard.jsonl"
    if not (_junction(linked, out) or _symlink(linked, out / "loot.jsonl")):
        pytest.skip("neither mklink /J nor os.symlink is available")

    dst = tmp_path / "dst" / "shard.jsonl"
    with pytest.raises(store.MigrationRefused):
        store.import_jsonl(linked, dst, "k", "mirror", cfg, root=s)
    assert not dst.exists(), "the destination was written before the boundary refused"


def test_the_import_primitives_accept_an_ordinary_contained_file(tmp_path, cfg):
    """False-positive companion: the at-the-effect check must not refuse the normal case."""
    s = tmp_path / "silo"
    s.mkdir()
    (s / ".state.json").write_text('{"last": "ok"}', encoding="utf-8")
    (s / "shard.jsonl").write_text('{"ts": "2026-07-01T00:00:00Z", "text": "hello"}\n',
                                   encoding="utf-8")

    # dst-side containment (RC5L-H) checks against the real STORE_ROOT - every production caller
    # (handle_migrate) constructs its destination under it, so the test must too.
    d = store.STORE_ROOT / "import_primitives_dst"
    assert store.import_metadata_json(s / ".state.json", d / ".state.json",
                                      "k", ".state.json", cfg, root=s) is True
    assert store.import_jsonl(s / "shard.jsonl", d / "shard.jsonl",
                              "k", "mirror", cfg, root=s) == (1, 0)


# --- the tag family: a reparse point is not the same thing as a link ------------------------
#
# The guard tested the FILE_ATTRIBUTE_REPARSE_POINT bit alone and called that "a link of ANY
# kind". It is not. OneDrive Files On-Demand, NTFS dedup and WOF compression all set that bit
# on ordinary files whose path resolves to itself, so the guard refused them - which would
# strand migration for anyone whose ~/.claude sits on a cloud-synced drive. Real placeholders
# cannot be manufactured in a test, so the tag is injected through lstat.

_ATTR = store._FILE_ATTRIBUTE_REPARSE_POINT


class _FakeStat:
    """An lstat result carrying a chosen reparse tag."""
    def __init__(self, tag, mode=0o100644):
        self.st_file_attributes = _ATTR
        self.st_reparse_tag = tag
        self.st_mode = mode


def _with_tag(monkeypatch, tag):
    monkeypatch.setattr(store.os, "lstat", lambda _p: _FakeStat(tag))


# CLOUD, CLOUD_1 .. CLOUD_F - the whole OneDrive family varies one nibble
@pytest.mark.parametrize("tag", [0x9000001A, 0x9000101A, 0x9000701A, 0x9000F01A])
def test_a_onedrive_placeholder_is_not_a_link(monkeypatch, tmp_path, tag):
    _with_tag(monkeypatch, tag)
    assert store.is_reparse_point(tmp_path / "any") is False, (
        "a cloud placeholder was refused as a link; a synced store would be unmigratable")


@pytest.mark.parametrize("tag,name", [
    (0x80000013, "dedup"), (0x80000017, "WOF-compressed"),
    (0xC0000004, "HSM"), (0x00000007, "single-instance-storage"),
])
def test_content_only_tags_are_not_links(monkeypatch, tmp_path, tag, name):
    _with_tag(monkeypatch, tag)
    assert store.is_reparse_point(tmp_path / "any") is False, "%s was refused as a link" % name


@pytest.mark.parametrize("tag,name", [
    (0xA000000C, "symlink"), (0xA0000003, "junction"), (0xA000001D, "WSL symlink"),
    (0x9000001C, "ProjFS"), (0x80000018, "container-isolation"),
])
def test_redirecting_tags_are_still_refused(monkeypatch, tmp_path, tag, name):
    _with_tag(monkeypatch, tag)
    assert store.is_reparse_point(tmp_path / "any") is True, "%s slipped past the guard" % name


def test_an_uncatalogued_tag_fails_closed(monkeypatch, tmp_path):
    """The allowlist's whole point: a redirect nobody has catalogued looks exactly like this."""
    _with_tag(monkeypatch, 0xDEADBEEF)
    assert store.is_reparse_point(tmp_path / "any") is True
    monkeypatch.setattr(store.os, "lstat", lambda _p: _FakeStat(0))   # tag absent -> 0
    assert store.is_reparse_point(tmp_path / "any") is True


def test_a_file_with_no_reparse_bit_is_never_a_link(monkeypatch, tmp_path):
    class _Plain:
        st_file_attributes = 0
        st_reparse_tag = 0xA000000C     # ignored: the bit is not set
        st_mode = 0o100644
    monkeypatch.setattr(store.os, "lstat", lambda _p: _Plain())
    assert store.is_reparse_point(tmp_path / "any") is False
