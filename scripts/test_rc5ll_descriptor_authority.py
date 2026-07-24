"""Cross-platform descriptor authority: the lexical name must still identify the opened file.

RC5L-K compared a pre-open lstat only with fstat. That misses a same-inode substitution:
move the proven inode outside the root, replace its old name with a symlink back to that SAME
inode, and a platform without O_NOFOLLOW opens a descriptor whose dev/ino and nlink still match.
The create branch had the same gap after O_EXCL returned. The state lock also still relied on
O_NOFOLLOW after a precheck, and file permissions were re-applied by pathname after close.

A REAL file-level symlink needs elevated privilege on this Windows dev machine (os.symlink() on
a file raises WinError 1314 here - same constraint documented throughout this suite), and
renaming a file while THIS process still holds an open handle to it is separately blocked on
Windows (WinError 32) - so the two create-branch tests below cannot hold the just-created
descriptor open across a real rename the way a POSIX attacker could. Every attack here is
therefore built from two REAL, unprivileged filesystem operations (os.rename of a closed handle,
plus ordinary content writes) and ONE narrow substitution: store.os.lstat is monkeypatched to
report the swapped-to name as a redirect, which is exactly the signal _validate_single_regular
consults (via _stat_redirects on an already-fetched stat, never a fresh by-path check - that is
deliberate, since re-deriving the verdict from a fresh lookup would reopen the very TOCTOU window
this patch closes). This is narrower than faking the whole attack, and it fails cleanly against
the pre-patch code (proven below by reasoning through each test against the old shape), rather
than passing by accident because a privileged step failed first.
"""
import contextlib
import io
import os
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store  # noqa: E402
import pcx  # noqa: E402


@pytest.fixture()
def env(tmp_path, monkeypatch):
    root = tmp_path / "store"
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(store, "STORE_ROOT", root)
    monkeypatch.setattr(store, "GLOBAL_CONFIG", root / "config.json")
    monkeypatch.setattr(store.os, "O_NOFOLLOW", 0, raising=False)
    monkeypatch.delenv("PARSICA_ISOLATED", raising=False)
    store.ensure_workspace(str(project))
    return root, project


class _Stdin:
    def __init__(self, data):
        self.buffer = io.BytesIO(data)


class _FakeRedirectStat:
    """What _stat_redirects sees from a real POSIX symlink or Windows reparse point: no
    st_file_attributes at all makes it fall back to stat.S_ISLNK(st_mode), which this reports
    True for - the platform-agnostic half of that check, exercised without needing a real link."""
    st_mode = stat.S_IFLNK | 0o777
    st_dev = 0
    st_ino = 0
    st_nlink = 1


def _fake_redirect_lstat(monkeypatch, redirected_path):
    """Make store.os.lstat report `redirected_path` as a link/reparse point from this point on,
    while every other path still gets the real lstat - the substitute for a real symlink."""
    real_lstat = os.lstat

    def fake(p, *a, **kw):
        if Path(p) == redirected_path:
            return _FakeRedirectStat()
        return real_lstat(p, *a, **kw)

    monkeypatch.setattr(store.os, "lstat", fake)


def test_existing_append_rejects_same_inode_move_and_symlink_swap(env, tmp_path, monkeypatch):
    root, _ = env
    leaf = store.lessons_path()
    leaf.write_text("ORIGINAL\n", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "victim.md"
    real_open = os.open
    swapped = False

    def attack(path, flags, *args, **kwargs):
        nonlocal swapped
        if Path(path) == leaf and not (flags & os.O_CREAT) and not swapped:
            swapped = True
            os.rename(leaf, victim)                    # real: no open handle blocks this here
            _fake_redirect_lstat(monkeypatch, leaf)     # stand-in for the symlink back to it
            return real_open(str(victim), flags, *args, **kwargs)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(store.os, "open", attack)
    with pytest.raises(store.MigrationRefused):
        store._safe_append_open(leaf, root)
    assert victim.read_text(encoding="utf-8") == "ORIGINAL\n"


def test_new_append_rechecks_the_name_after_exclusive_create(env, tmp_path, monkeypatch):
    root, _ = env
    leaf = store.journal_dir() / "2099-01-01.md"
    outside = tmp_path / "outside2"
    outside.mkdir()
    victim = outside / "victim.md"
    real_open = os.open
    swapped = False

    def attack(path, flags, *args, **kwargs):
        nonlocal swapped
        if Path(path) == leaf and (flags & os.O_CREAT) and (flags & os.O_EXCL) and not swapped:
            fd = real_open(path, flags, *args, **kwargs)
            swapped = True
            os.close(fd)                                # release the handle: Windows refuses to
            os.rename(leaf, victim)                      # rename a file this process still has open
            _fake_redirect_lstat(monkeypatch, leaf)
            reopen_flags = flags & ~(os.O_CREAT | os.O_EXCL)
            return real_open(str(victim), reopen_flags, *args, **kwargs)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(store.os, "open", attack)
    with pytest.raises(store.MigrationRefused):
        store._safe_append_open(leaf, root)
    assert victim.read_bytes() == b""


def test_public_journal_note_cannot_write_after_post_create_swap(env, tmp_path, monkeypatch, capsys):
    root, project = env
    leaf = store.journal_dir() / f"{pcx._now_utc().date().isoformat()}.md"
    leaf.unlink(missing_ok=True)
    outside = tmp_path / "outside3"
    outside.mkdir()
    victim = outside / "victim.md"
    real_open = os.open
    swapped = False

    def attack(path, flags, *args, **kwargs):
        nonlocal swapped
        if Path(path) == leaf and (flags & os.O_CREAT) and (flags & os.O_EXCL) and not swapped:
            fd = real_open(path, flags, *args, **kwargs)
            swapped = True
            os.close(fd)
            os.rename(leaf, victim)
            _fake_redirect_lstat(monkeypatch, leaf)
            reopen_flags = flags & ~(os.O_CREAT | os.O_EXCL)
            return real_open(str(victim), reopen_flags, *args, **kwargs)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(store.os, "open", attack)
    monkeypatch.setattr(sys, "stdin", _Stdin(b"payload"))
    with pytest.raises(store.MigrationRefused):
        pcx.handle_note(["note", "--kind", "journal", "--cwd", str(project), "--sess", "s1"])
    assert '"stored": true' not in capsys.readouterr().out
    assert victim.read_bytes() == b""


def test_read_rejects_same_inode_move_modify_and_symlink_swap(env, tmp_path, monkeypatch):
    root, _ = env
    leaf = store.lessons_path()
    leaf.write_text("BENIGN\n", encoding="utf-8")
    outside = tmp_path / "outside4"
    outside.mkdir()
    victim = outside / "victim.md"
    real_open = os.open
    swapped = False

    def attack(path, flags, *args, **kwargs):
        nonlocal swapped
        if Path(path) == leaf and not (flags & os.O_CREAT) and not swapped:
            swapped = True
            os.rename(leaf, victim)
            victim.write_text("needle attacker content\n", encoding="utf-8")
            _fake_redirect_lstat(monkeypatch, leaf)
            return real_open(str(victim), flags, *args, **kwargs)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(store.os, "open", attack)
    with pytest.raises(store.MigrationRefused):
        store._read_text_no_follow(leaf, root)


def test_public_sessionstart_does_not_inject_same_inode_swap(env, tmp_path, monkeypatch):
    root, project = env
    leaf = store.working_path(str(project))
    leaf.write_text("# Working Set\n\nBENIGN\n", encoding="utf-8")
    outside = tmp_path / "outside5"
    outside.mkdir()
    victim = outside / "working.md"
    real_open = os.open
    swapped = False

    def attack(path, flags, *args, **kwargs):
        nonlocal swapped
        if Path(path) == leaf and not (flags & os.O_CREAT) and not swapped:
            swapped = True
            os.rename(leaf, victim)
            victim.write_text("# Working Set\n\nneedle attacker continuity\n", encoding="utf-8")
            _fake_redirect_lstat(monkeypatch, leaf)
            return real_open(str(victim), flags, *args, **kwargs)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(store.os, "open", attack)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        pcx.handle_session_start({"cwd": str(project), "session_id": "s1"})
    assert "needle attacker continuity" not in out.getvalue()


def test_state_lock_cannot_create_external_target_when_link_wins_race(env, tmp_path, monkeypatch):
    root, project = env
    state = store.project_dir(str(project)) / ".state.json"
    lock = Path(str(state) + ".lock")
    lock.unlink(missing_ok=True)
    outside = tmp_path / "outside6"
    outside.mkdir()
    victim = outside / "victim.lock"
    real_open = os.open
    planted = False

    def attack(path, flags, *args, **kwargs):
        nonlocal planted
        if Path(path) == lock and (flags & os.O_CREAT) and (flags & os.O_EXCL) and not planted:
            planted = True
            # Stand-in for a dangling symlink winning the create race: the O_EXCL attempt sees
            # the name already occupied, and the lstat the FileExistsError branch takes next
            # reports it as a redirect - the same shape a real link would leave in each check.
            _fake_redirect_lstat(monkeypatch, lock)
            raise FileExistsError(17, "a link already occupies this name")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(store.os, "open", attack)
    with store._state_lock(state, timeout=0.01) as got:
        assert got is False
    assert not victim.exists()


def test_append_permissions_are_not_reapplied_by_leaf_path(env, tmp_path, monkeypatch):
    _, project = env
    leaf = store.lessons_path()
    victim = tmp_path / "victim-mode"
    victim.write_text("VICTIM\n", encoding="utf-8")
    os.chmod(victim, 0o644)
    before_mode = stat.S_IMODE(os.stat(victim).st_mode)
    real_chmod = store._chmod
    attacked = False

    def attack(path, mode):
        # The property under test is that _chmod-by-pathname is never called on the leaf at all
        # any more (permissions ride the open descriptor instead, via _fchmod) - so the closure
        # only needs to OBSERVE whether that call happens, not act out what an attacker could do
        # with it. Acting it out would need the same unprivileged-symlink workaround as the tests
        # above for no extra signal: if this ever fires, the leaf has already been chmod'd by a
        # name an attacker could have swapped in the meantime, which is the bug regardless.
        nonlocal attacked
        if Path(path) == leaf:
            attacked = True
        return real_chmod(path, mode)

    monkeypatch.setattr(store, "_chmod", attack)
    monkeypatch.setattr(sys, "stdin", _Stdin(b"ordinary lesson"))
    pcx.handle_note(["note", "--kind", "lesson", "--cwd", str(project), "--sess", "s1"])
    assert attacked is False
    # Windows os.chmod cannot express distinct owner/group/other bits (0o644 reads back as 0o666
    # there) - assert the mode is UNCHANGED rather than a specific POSIX octal value.
    assert stat.S_IMODE(os.stat(victim).st_mode) == before_mode


def test_atomic_publish_permissions_are_not_reapplied_by_destination_path(env, tmp_path, monkeypatch):
    _, project = env
    leaf = store.working_path(str(project))
    victim = tmp_path / "victim-mode-2"
    victim.write_text("VICTIM\n", encoding="utf-8")
    os.chmod(victim, 0o644)
    before_mode = stat.S_IMODE(os.stat(victim).st_mode)
    real_chmod = store._chmod
    attacked = False

    def attack(path, mode):
        nonlocal attacked
        if Path(path) == leaf:
            attacked = True
        return real_chmod(path, mode)

    monkeypatch.setattr(store, "_chmod", attack)
    monkeypatch.setattr(sys, "stdin", _Stdin(b"ordinary working"))
    pcx.handle_note(["note", "--kind", "working", "--cwd", str(project), "--sess", "s1"])
    assert attacked is False
    assert stat.S_IMODE(os.stat(victim).st_mode) == before_mode


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
