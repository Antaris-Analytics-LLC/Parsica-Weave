"""RC5L-M: _verify_opened_leaf rebinds the final leaf but not the current lexical parent chain.

A parent directory (journal/, working/, or a project directory holding a state lock) can be moved
outside the root and its old name replaced by a link back to that SAME directory, between the
initial _verified_parent()/assert_contained_file() proof at the top of each gateway and the final
leaf check _verify_opened_leaf() performs. The leaf file itself is unaffected by this - it is
still the same inode, just reached through a now-redirected parent - so RC5L-L's leaf-only
identity check never notices. _verify_opened_leaf must re-walk the CURRENT lexical parent chain
too, every time, right before it accepts the descriptor.

Portability: the redirect these attacks plant is a DIRECTORY redirect, which needs no elevated
privilege on either platform - an NTFS junction on Windows (`mklink /J`), a directory symlink on
POSIX (`os.symlink(..., target_is_directory=True)`). The helper picks the right primitive per
platform; a `require_redirect` fixture probes once up front and SKIPS the whole test where neither
is available, so the attack flag is only ever set after a redirect has actually been created - a
platform that cannot build the redirect never reaches the assertion vacuously.
"""
import contextlib
import io
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store  # noqa: E402
import pcx  # noqa: E402


def _redirect_dir(link, target):
    """Point directory `link` at `target` without elevated privilege. Windows: NTFS junction.
    POSIX: directory symlink. Returns True on success, False if the primitive is unavailable."""
    if os.name == "nt":
        r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                           capture_output=True, text=True)
        return r.returncode == 0 and os.path.isdir(str(link))
    try:
        os.symlink(str(target), str(link), target_is_directory=True)
        return True
    except (OSError, NotImplementedError):
        return False


def _can_redirect(base):
    base.mkdir(parents=True, exist_ok=True)
    target = base / "t"
    target.mkdir()
    return _redirect_dir(base / "l", target)


@pytest.fixture()
def require_redirect(tmp_path):
    if not _can_redirect(tmp_path / "_redirect_probe"):
        pytest.skip("directory redirects unavailable on this platform/account")


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


# --- append: journal/ swapped between the parent proof and the final leaf check ----------------

def test_append_refuses_a_parent_swapped_between_proof_and_open(
        env, require_redirect, tmp_path, monkeypatch):
    root, _ = env
    jdir = store.journal_dir()
    leaf = jdir / "2099-01-01.md"
    leaf.write_text("ORIGINAL\n", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    moved = outside / "journal-real"
    real_open = os.open
    swapped = False

    def attack(path, flags, *args, **kwargs):
        nonlocal swapped
        if Path(path) == leaf and not (flags & os.O_CREAT) and not swapped:
            os.rename(jdir, moved)
            assert _redirect_dir(jdir, moved), "redirect creation failed after probe passed"
            swapped = True
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(store.os, "open", attack)
    with pytest.raises(store.MigrationRefused):
        store._safe_append_open(leaf, root)
    assert swapped, "the attack never got a chance to fire"
    assert (moved / "2099-01-01.md").read_text(encoding="utf-8") == "ORIGINAL\n"


def test_public_journal_note_refuses_after_a_parent_swap(
        env, require_redirect, tmp_path, monkeypatch, capsys):
    root, project = env
    jdir = store.journal_dir()
    leaf = jdir / f"{pcx._now_utc().date().isoformat()}.md"
    leaf.write_text("ORIGINAL\n", encoding="utf-8")
    outside = tmp_path / "outside2"
    outside.mkdir()
    moved = outside / "journal-real2"
    real_open = os.open
    swapped = False

    def attack(path, flags, *args, **kwargs):
        nonlocal swapped
        if Path(path) == leaf and not (flags & os.O_CREAT) and not swapped:
            os.rename(jdir, moved)
            assert _redirect_dir(jdir, moved), "redirect creation failed after probe passed"
            swapped = True
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(store.os, "open", attack)
    monkeypatch.setattr(sys, "stdin", _Stdin(b"attacker payload"))
    with pytest.raises(store.MigrationRefused):
        pcx.handle_note(["note", "--kind", "journal", "--cwd", str(project), "--sess", "s1"])
    assert '"stored": true' not in capsys.readouterr().out
    assert "attacker payload" not in (moved / leaf.name).read_text(encoding="utf-8")


# --- read: working/ swapped between the parent proof and the final leaf check ------------------

def test_read_refuses_a_parent_swapped_between_proof_and_open(
        env, require_redirect, tmp_path, monkeypatch):
    root, project = env
    wp = store.working_path(str(project))
    wdir = wp.parent
    wp.write_text("BENIGN\n", encoding="utf-8")
    outside = tmp_path / "outside3"
    outside.mkdir()
    moved = outside / "working-real"
    real_open = os.open
    swapped = False

    def attack(path, flags, *args, **kwargs):
        nonlocal swapped
        if Path(path) == wp and not swapped:
            os.rename(wdir, moved)
            assert _redirect_dir(wdir, moved), "redirect creation failed after probe passed"
            (moved / wp.name).write_text("needle attacker content\n", encoding="utf-8")
            swapped = True
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(store.os, "open", attack)
    with pytest.raises(store.MigrationRefused):
        store._read_text_no_follow(wp, root)
    assert swapped, "the attack never got a chance to fire"


def test_public_sessionstart_does_not_inject_after_a_parent_swap(
        env, require_redirect, tmp_path, monkeypatch):
    root, project = env
    wp = store.working_path(str(project))
    wdir = wp.parent
    wp.write_text("# Working Set\n\nBENIGN\n", encoding="utf-8")
    outside = tmp_path / "outside4"
    outside.mkdir()
    moved = outside / "working-real2"
    real_open = os.open
    swapped = False

    def attack(path, flags, *args, **kwargs):
        nonlocal swapped
        if Path(path) == wp and not swapped:
            os.rename(wdir, moved)
            assert _redirect_dir(wdir, moved), "redirect creation failed after probe passed"
            (moved / wp.name).write_text(
                "# Working Set\n\nneedle parent-swap injected continuity\n", encoding="utf-8")
            swapped = True
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(store.os, "open", attack)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        pcx.handle_session_start({"cwd": str(project), "session_id": "s1"})
    assert swapped, "the attack never got a chance to fire"
    assert "needle parent-swap injected continuity" not in out.getvalue()


# --- lock: a project directory swapped between the parent proof and the final leaf check -------

def test_state_lock_refuses_a_parent_swapped_between_proof_and_open(
        env, require_redirect, tmp_path, monkeypatch):
    root, project = env
    state = store.project_dir(str(project)) / ".state.json"
    pdir = state.parent
    lock = Path(str(state) + ".lock")
    lock.write_text("", encoding="utf-8")
    outside = tmp_path / "outside5"
    outside.mkdir()
    moved = outside / "project-real"
    real_open = os.open
    swapped = False

    def attack(path, flags, *args, **kwargs):
        nonlocal swapped
        if Path(path) == lock and not (flags & os.O_CREAT) and not swapped:
            os.rename(pdir, moved)
            assert _redirect_dir(pdir, moved), "redirect creation failed after probe passed"
            swapped = True
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(store.os, "open", attack)
    # Pre-fix, the reopen went through the redirected parent and the lock was acquired on the
    # external file (got was True); the parent recheck refuses it. Until October 2026 that refusal
    # degraded to an unlocked yield (got False); the lock now fails closed (StateLockError) and the
    # body never runs.
    with pytest.raises(store.StateLockError):
        with store._state_lock(state, timeout=0.01):
            pass
    assert swapped, "the attack never got a chance to fire"


# --- ordinary use: an UNSWAPPED chain must keep working exactly as before ----------------------

def test_ordinary_append_and_read_still_work_with_no_swap(env):
    root, project = env
    leaf = store.journal_dir() / "2098-06-15.md"
    fd = store._safe_append_open(leaf, root)
    os.write(fd, b"hello\n")
    os.close(fd)
    assert store._read_text_no_follow(leaf, root) == "hello\n"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
