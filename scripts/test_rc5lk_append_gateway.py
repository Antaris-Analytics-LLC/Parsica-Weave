"""RC5L-K: _safe_append_open's cross-platform gap when O_NOFOLLOW is unavailable.

os.O_NOFOLLOW is an optional C-library extension, not a portable guarantee - it does not exist
in Python's os module on Windows at all (confirmed empirically: hasattr(os, "O_NOFOLLOW") is
False on this dev machine), so `getattr(os, "O_NOFOLLOW", 0)` silently degrades to 0 on exactly
the platform this store ships on. Without it, an O_CREAT|O_APPEND open of a pre-existing
symlinked name follows the link before fstat ever runs, and an attacker's ordinary external file
passes the old post-open checks (S_ISREG, st_nlink == 1) unchanged, because those checks inspect
the descriptor's TYPE, never its IDENTITY against what was supposed to be there.

This machine cannot create a real file-level symlink without elevated privilege (verified: a
direct os.symlink() attempt here raises WinError 1314), so the exact symlink-follow scenario is
reproduced by simulating what the OS does when it follows one - substituting the descriptor a
real open() would have returned, and forcing a reparse-point verdict - rather than by literally
planting a symlink. Hardlink and ordinary-file cases ARE fully real: this filesystem creates
hardlinks without privilege, same as every other RC5L round's tests.
"""
import os
import stat as stat_mod
import sys
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("PARSICA_WEAVE_ROOT", tempfile.mkdtemp(prefix="rc5lk_root_"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store  # noqa: E402
import pcx  # noqa: E402


def _hardlink(link: Path, target: Path) -> bool:
    try:
        os.link(str(target), str(link))
        return True
    except (OSError, NotImplementedError):
        return False


@pytest.fixture()
def fresh_root(monkeypatch, tmp_path):
    root = tmp_path / "store_root"
    monkeypatch.setattr(store, "STORE_ROOT", root)
    return root


# --- ordinary use: creation and append must still work -------------------------------------------

def test_ordinary_first_time_create_then_append_round_trips(fresh_root, tmp_path):
    target = tmp_path / "ws" / "lessons.md"
    fd = store._safe_append_open(target, tmp_path / "ws")
    os.write(fd, b"first\n")
    os.close(fd)
    fd = store._safe_append_open(target, tmp_path / "ws")
    os.write(fd, b"second\n")
    os.close(fd)
    assert target.read_text(encoding="utf-8") == "first\nsecond\n"


# --- hardlink: fully real on this filesystem, exercises the EEXIST -> inspect -> reopen path -----

def test_hardlinked_existing_file_is_refused(fresh_root, tmp_path):
    root = tmp_path / "ws2"
    root.mkdir()
    target = root / "lessons.md"
    external = tmp_path / "external_victim.md"
    external.write_text("ORIGINAL\n", encoding="utf-8")
    assert _hardlink(target, external), "hardlinks unavailable here"
    with pytest.raises(store.MigrationRefused):
        store._safe_append_open(target, root)
    assert external.read_text(encoding="utf-8") == "ORIGINAL\n"


# --- symlink-shaped attack, simulated (real symlinks unavailable without privilege here) ---------

def test_a_reparse_point_at_the_leaf_is_refused_regardless_of_o_nofollow(
        fresh_root, tmp_path, monkeypatch):
    """Simulates a pre-existing symlink at the leaf: a real (ordinary) file occupies the name so
    the O_EXCL create fails EEXIST and the EEXIST branch runs, but is_reparse_point is monkeypatched
    to report True for this exact path - what a real symlink would report via lstat, which this
    filesystem cannot construct for a FILE without elevated privilege."""
    root = tmp_path / "ws3"
    root.mkdir()
    target = root / "lessons.md"
    target.write_text("placeholder\n", encoding="utf-8")
    real_is_reparse = store.is_reparse_point
    monkeypatch.setattr(store, "is_reparse_point",
                        lambda p: True if Path(p) == target else real_is_reparse(p))
    with pytest.raises(store.MigrationRefused):
        store._safe_append_open(target, root)


def test_identity_mismatch_is_refused_even_with_o_nofollow_forced_absent(
        fresh_root, tmp_path, monkeypatch):
    """The core reproduction: forces O_NOFOLLOW to 0 (already its real value on this platform) and
    substitutes what a followed symlink's open() would return - a descriptor for a DIFFERENT,
    perfectly ordinary, single-link file - to prove the pre-lstat/post-fstat identity comparison
    (not O_NOFOLLOW) is what actually stops this, exactly like _read_text_no_follow's read-side
    twin already relies on for the same platform gap."""
    monkeypatch.setattr(store.os, "O_NOFOLLOW", 0, raising=False)
    root = tmp_path / "ws4"
    root.mkdir()
    target = root / "lessons.md"
    target.write_text("placeholder\n", encoding="utf-8")   # occupies the name -> EEXIST branch
    victim = tmp_path / "victim.md"
    victim.write_text("ORIGINAL\n", encoding="utf-8")

    real_open = os.open

    def _fake_open(path, flags, *a, **kw):
        # only the SECOND call (the reopen-without-O_CREAT) is redirected, mimicking a followed
        # symlink; the first O_EXCL create attempt must run for real so the EEXIST branch fires.
        if not (flags & os.O_CREAT):
            return real_open(str(victim), flags, *a, **kw)
        return real_open(path, flags, *a, **kw)

    monkeypatch.setattr(store.os, "open", _fake_open)
    with pytest.raises(store.MigrationRefused, match="changed identity"):
        store._safe_append_open(target, root)
    assert victim.read_text(encoding="utf-8") == "ORIGINAL\n"


# --- public entry point: the note handler must not report success through any of the above -------

def test_public_note_refuses_a_hardlinked_lessons_file(fresh_root, tmp_path, monkeypatch, capsys):
    import io
    cwd = tmp_path / "project"
    cwd.mkdir()
    store.ensure_workspace(str(cwd))
    lessons = store.lessons_path()
    lessons.unlink()
    external = tmp_path / "external_lesson_victim.md"
    external.write_text("ORIGINAL\n", encoding="utf-8")
    assert _hardlink(lessons, external), "hardlinks unavailable here"

    class _FakeStdin:
        buffer = io.BytesIO(b"a fresh lesson")

    monkeypatch.setattr(sys, "stdin", _FakeStdin())
    # append_doc has no try/except around the write - a refusal here propagates uncaught, same as
    # every other MigrationRefused from this gateway (pre-existing, unrelated to this fix). What
    # matters for this test is that nothing reports success and the victim is untouched.
    with pytest.raises(store.MigrationRefused):
        pcx.handle_note(["note", "--kind", "lesson", "--cwd", str(cwd), "--sess", "s1"])
    out = capsys.readouterr().out
    assert '"stored":true' not in out.replace(" ", "")
    assert external.read_text(encoding="utf-8") == "ORIGINAL\n"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
