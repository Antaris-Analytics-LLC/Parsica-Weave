#!/usr/bin/env python3
"""A lock that cannot be taken must be VISIBLE, even though taking it is optional.

`_state_lock` fails OPEN on purpose: continuity bookkeeping must never wedge a session, and a
guard that locks a user out of their own memory over a false positive is a worse product than
one that degrades. The cost of that policy is that a lock name occupied by a directory junction,
a symlink, a directory or a FIFO degrades to "unlocked" IDENTICALLY to an ordinary two-second
contention timeout. One is transient and fine. The other is permanent, and means every
concurrent session has been racing the state file with no protection for as long as the object
has been sitting there. Nothing in the system could tell them apart, so nothing could say so.

#122 is fixed on the DIAGNOSIS axis, not the enforcement axis. These tests therefore pin two
properties that pull in opposite directions, and both matter:

  1. lock_health() NAMES an unlockable sidecar (the attack case), and
  2. _state_lock still degrades quietly rather than raising (the fail-open policy), and
     lock_health() stays SILENT on an ordinary healthy store (the false-positive companion).

Without (2), a future "hardening" pass could turn the fail-open into a fail-closed and every
attack test here would still be green while the product got worse for real users.
"""
import os
import subprocess
import sys
import tempfile

import pytest

os.environ.setdefault("PARSICA_WEAVE_ROOT", tempfile.mkdtemp(prefix="lockhealth_boot_"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store  # noqa: E402

WINDOWS = os.name == "nt"


def _redirect(link, target):
    """Plant a directory redirect at `link`; confirm it took before the test relies on it. A
    failed mklink must never read as 'the probe found nothing'."""
    os.makedirs(str(target), exist_ok=True)
    if WINDOWS:
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                       capture_output=True, text=True)
    else:
        try:
            os.symlink(str(target), str(link))
        except OSError:
            return False
    return store.is_reparse_point(link)


# --------------------------------------------------------------------------- false positives


def test_a_healthy_store_reports_nothing(tmp_path):
    """The companion that keeps this honest. An ordinary store - including one with real lock
    sidecars already created - must produce ZERO findings. A probe that cries wolf on a normal
    workspace would be turned off within a day, which is the same as not shipping it."""
    root = tmp_path / "ws"
    os.makedirs(str(root))
    (root / ".state.json.lock").write_bytes(b"")
    (root / "workspace.json.lock").write_bytes(b"")
    assert store.lock_health(root) == []


def test_an_empty_store_reports_nothing(tmp_path):
    root = tmp_path / "ws"
    os.makedirs(str(root))
    assert store.lock_health(root) == []


def test_a_missing_root_reports_nothing_and_does_not_raise(tmp_path):
    """A doctor probe that can itself explode is not a diagnosis."""
    assert store.lock_health(tmp_path / "does-not-exist") == []


def test_non_lock_files_are_ignored(tmp_path):
    """Only *.lock is inspected - a junction elsewhere in the store is a different concern and
    must not be reported by THIS check, or the signal drowns."""
    root = tmp_path / "ws"
    os.makedirs(str(root))
    (root / ".state.json").write_text("{}", encoding="utf-8")
    if not _redirect(root / "somedir", tmp_path / "elsewhere"):
        pytest.skip("directory redirects unavailable here")
    assert store.lock_health(root) == []


# --------------------------------------------------------------------------- detection


def test_a_redirect_at_a_lock_name_is_named(tmp_path):
    """The load-bearing case: exactly the shape that silently disables locking.

    Asserts the REDIRECT clause by name, not merely that something was reported. A junction also
    lstats as a directory on Windows, so lock_health has two independent clauses that both catch
    it - and an assertion on the shared tail ("locking silently disabled") stays green when the
    redirect clause is deleted, measuring the probe's redundancy instead of the property. Verified
    by mutation: removing the reparse branch left the looser assertion passing."""
    root = tmp_path / "ws"
    os.makedirs(str(root))
    if not _redirect(root / ".state.json.lock", tmp_path / "outside"):
        pytest.skip("directory redirects unavailable here")
    found = store.lock_health(root)
    assert len(found) == 1, "the planted redirect was not reported: %r" % (found,)
    path, reason = found[0]
    assert path.name == ".state.json.lock"
    assert "link or redirect" in reason, \
        "reported, but by the generic non-regular clause rather than the redirect clause: %s" % reason
    assert "locking silently disabled" in reason, \
        "reported, but without saying locking is off: %s" % reason


def test_a_directory_at_a_lock_name_is_named(tmp_path):
    """A plain directory needs no privilege to create, so it is the cheapest way to reach this
    state by accident as well as on purpose."""
    root = tmp_path / "ws"
    os.makedirs(str(root / ".state.json.lock"))
    found = store.lock_health(root)
    assert len(found) == 1 and "directory" in found[0][1], found


def test_discovery_covers_a_lock_name_this_test_never_taught_it(tmp_path):
    """The enforcer must DISCOVER, not carry a hand-list. A lock sidecar added by some future
    call site has to be covered the day it appears, not the day someone remembers to update a
    constant here - a hand-list is vigilance wearing a test's clothes."""
    root = tmp_path / "ws"
    os.makedirs(str(root / "nested"))
    os.makedirs(str(root / "nested" / "a-name-nobody-listed.lock"))
    found = store.lock_health(root)
    assert len(found) == 1 and found[0][0].name == "a-name-nobody-listed.lock", found


def test_every_finding_carries_a_reason(tmp_path):
    """A finding with no cause is a shrug. Each one must say what is wrong."""
    root = tmp_path / "ws"
    os.makedirs(str(root / "one.lock"))
    os.makedirs(str(root / "two.lock"))
    found = store.lock_health(root)
    assert len(found) == 2
    assert all(reason and reason.strip() for _, reason in found), found


# --------------------------------------------------------------------------- the policy itself


def test_state_lock_degrades_quietly_when_the_sidecar_is_a_redirect(tmp_path, monkeypatch):
    """The fail-open policy, pinned. This is the test that must go red if anyone ever converts
    the diagnosis into enforcement: a planted redirect yields False (unlocked) and does NOT
    raise. Jake's rule - being locked out of your own continuity is a worse failure than an
    unlocked merge."""
    root = tmp_path / "ws"
    os.makedirs(str(root))
    monkeypatch.setattr(store, "STORE_ROOT", root)
    state = root / ".state.json"
    if not _redirect(root / ".state.json.lock", tmp_path / "outside"):
        pytest.skip("directory redirects unavailable here")
    with store._state_lock(state) as got:
        assert got is False, "a redirected lock name must degrade to unlocked, not appear taken"


def test_state_lock_still_takes_an_ordinary_lock(tmp_path, monkeypatch):
    """The other half of the same policy: degrading is for the broken case ONLY. If this goes
    red, locking has been disabled everywhere and the attack tests above would never notice."""
    root = tmp_path / "ws"
    os.makedirs(str(root))
    monkeypatch.setattr(store, "STORE_ROOT", root)
    with store._state_lock(root / ".state.json") as got:
        assert got is True, "an ordinary lock sidecar must actually lock"


def test_the_reparse_check_is_reached_not_skipped(tmp_path, monkeypatch):
    """Regression pin for the clause itself. Pre-fix, `except FileExistsError:` meant a junction
    (EACCES on Windows) never reached the reparse check at all - the outer handler swallowed it.
    Behaviour was identical either way, which is exactly what hid it. Assert the check RUNS by
    making it observable."""
    root = tmp_path / "ws"
    os.makedirs(str(root))
    monkeypatch.setattr(store, "STORE_ROOT", root)
    if not _redirect(root / ".state.json.lock", tmp_path / "outside"):
        pytest.skip("directory redirects unavailable here")

    seen = []
    real = store.is_reparse_point

    def spy(p):
        seen.append(str(p))
        return real(p)

    monkeypatch.setattr(store, "is_reparse_point", spy)
    with store._state_lock(root / ".state.json") as got:
        assert got is False
    assert any(s.endswith(".state.json.lock") for s in seen), \
        "the reparse check was never reached - the except clause is narrow again"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
