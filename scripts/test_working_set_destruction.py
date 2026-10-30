#!/usr/bin/env python3
"""The working set is the one document a full replace can destroy. It must not be destroyable
by accident.

`note --kind working` replaces the file in full. Rotation never archives it (rotate handles
mirror and enrich shards only), and no copy exists anywhere else in the store. So a malformed
pipe - `echo '' | pcx note --kind working`, a command substitution that produced nothing, a
heredoc that closed early - truncated the most curated, least reproducible document in the
store to zero bytes and reported {"ok": true, "stored": true}. Reproduced against the shipped
v1.0 build before this guard existed.

Two independent protections, tested independently because either alone leaves a hole:

  1. an EMPTY replace is refused outright, and
  2. every successful replace keeps one previous copy, so a bad-but-non-empty write is
     recoverable too.

(1) without (2) still loses the file to a truncated-but-non-empty pipe. (2) without (1) leaves
the primary at zero bytes and relies on the user knowing a .prev exists. The false-positive
companions matter as much: an ordinary short working set must still write, or the guard has
made the feature worse than the bug.
"""
import os
import sys
import tempfile

import pytest

os.environ.setdefault("PARSICA_WEAVE_ROOT", tempfile.mkdtemp(prefix="worksetguard_boot_"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store  # noqa: E402

SEED = "# Working set\n\n## Pinned facts\n- the release tag is v1.0, not v1.0.0\n"


def _doc(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "STORE_ROOT", tmp_path)
    monkeypatch.setattr(store, "GLOBAL_CONFIG", tmp_path / "config.json")   # moves with the root
    p = tmp_path / "working.md"
    p.write_text(SEED, encoding="utf-8")
    return p


# --------------------------------------------------------------------------- refuse the empty


@pytest.mark.parametrize("payload", ["", "   ", "\n", "\n\n  \t\n", "\r\n"])
def test_an_empty_replace_is_refused_and_the_file_survives(tmp_path, monkeypatch, payload):
    """The load-bearing case, across every shape an accidental empty pipe actually takes."""
    p = _doc(tmp_path, monkeypatch)
    r = store.write_doc(p, payload, {})
    assert isinstance(r, dict) and r.get("stored") is False, \
        "an empty replace reported success: %r" % (r,)
    assert p.read_text(encoding="utf-8") == SEED, \
        "the working set was destroyed by an empty write"


def test_the_refusal_says_which_refusal_it_was(tmp_path, monkeypatch):
    """A caller must be able to tell an empty-replace refusal from the human_only fence - they
    mean completely different things to whoever is looking at the receipt."""
    p = _doc(tmp_path, monkeypatch)
    r = store.write_doc(p, "  ", {})
    assert r.get("reason") == "empty_replace_refused", r


def test_the_receipt_matches_the_human_only_shape(tmp_path, monkeypatch):
    """Same keys as the existing refusal, so pcx's note handler needs no new branch. If these
    diverge, a refusal starts rendering as a success somewhere downstream."""
    assert set(store._EMPTY_DOC_RECEIPT) == set(store._HUMAN_ONLY_RECEIPT)


# --------------------------------------------------------------------------- false positives


def test_an_ordinary_short_working_set_still_writes(tmp_path, monkeypatch):
    """The companion that keeps the guard honest. A minimum-LENGTH threshold would have failed
    here - which is exactly why the line is emptiness, not size."""
    p = _doc(tmp_path, monkeypatch)
    assert store.write_doc(p, "- one open loop", {}) is None
    assert "one open loop" in p.read_text(encoding="utf-8")


def test_a_single_character_working_set_still_writes(tmp_path, monkeypatch):
    p = _doc(tmp_path, monkeypatch)
    assert store.write_doc(p, "x", {}) is None
    assert p.read_text(encoding="utf-8").strip() == "x"


def test_content_that_is_only_whitespace_padded_still_writes(tmp_path, monkeypatch):
    p = _doc(tmp_path, monkeypatch)
    assert store.write_doc(p, "\n\n   real content   \n\n", {}) is None
    assert "real content" in p.read_text(encoding="utf-8")


def test_writing_a_doc_that_does_not_exist_yet_works(tmp_path, monkeypatch):
    """First write of a fresh workspace: nothing to back up, nothing to refuse."""
    monkeypatch.setattr(store, "STORE_ROOT", tmp_path)
    monkeypatch.setattr(store, "GLOBAL_CONFIG", tmp_path / "config.json")   # moves with the root
    p = tmp_path / "fresh.md"
    assert store.write_doc(p, "first", {}) is None
    assert p.read_text(encoding="utf-8").strip() == "first"


# --------------------------------------------------------------------------- the previous copy


def test_a_replace_keeps_the_previous_content_recoverable(tmp_path, monkeypatch):
    """The second protection: a truncated-but-NON-empty pipe passes the emptiness guard, so the
    only thing standing between the user and a lost working set is this copy."""
    p = _doc(tmp_path, monkeypatch)
    store.write_doc(p, "oops, a truncated pipe wrote this", {})
    prev = tmp_path / "working.md.prev"
    assert prev.is_file(), "no recoverable copy of the replaced working set"
    assert prev.read_text(encoding="utf-8") == SEED


def test_the_previous_copy_is_owner_only(tmp_path, monkeypatch):
    """It holds the same content as the working set, so it inherits the same posture. Skipped
    where POSIX modes are not meaningful."""
    if not hasattr(os, "geteuid"):
        pytest.skip("POSIX modes only")
    p = _doc(tmp_path, monkeypatch)
    store.write_doc(p, "replacement", {})
    assert (os.stat(str(tmp_path / "working.md.prev")).st_mode & 0o777) == 0o600


def test_only_one_generation_is_kept(tmp_path, monkeypatch):
    """A living document replaced many times a day must not grow a pile of copies. One
    generation undoes the mistake you just made; that is the whole job."""
    p = _doc(tmp_path, monkeypatch)
    for i in range(4):
        store.write_doc(p, "revision %d" % i, {})
    siblings = sorted(f.name for f in tmp_path.iterdir() if ".prev" in f.name)
    assert siblings == ["working.md.prev"], siblings
    assert "revision 2" in (tmp_path / "working.md.prev").read_text(encoding="utf-8")


def test_a_refused_empty_write_does_not_disturb_the_previous_copy(tmp_path, monkeypatch):
    """A refusal must be a no-op end to end - including not rotating a good backup out."""
    p = _doc(tmp_path, monkeypatch)
    store.write_doc(p, "good revision", {})
    store.write_doc(p, "", {})
    assert (tmp_path / "working.md.prev").read_text(encoding="utf-8") == SEED


def test_a_failing_backup_never_blocks_the_write(tmp_path, monkeypatch):
    """Best-effort by design: if the copy cannot be made, the user still gets the write they
    asked for. Losing undo is bad; losing the write is worse."""
    p = _doc(tmp_path, monkeypatch)
    monkeypatch.setattr(store, "atomic_write",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("no space")))
    monkeypatch.setattr(store, "_raw_write", lambda path, text: path.write_text(text, encoding="utf-8"))
    assert store.write_doc(p, "must still land", {}) is None
    assert "must still land" in p.read_text(encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
