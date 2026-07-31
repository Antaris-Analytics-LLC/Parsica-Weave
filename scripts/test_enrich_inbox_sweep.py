#!/usr/bin/env python3
"""A staged copy must not outlive the enrichment it was staged for.

enrich_prep writes each pending turn's plaintext to <temp>/parsica_enrich_inbox/<project>/<id>.txt
so a worker can read it. That is correct as a staging area and wrong as a resting place: the
README tells a user the store is one directory under ~/.claude and that deleting it erases their
data, while these copies live in the system temp dir and survived the uninstall. Cleanup existed,
but only at the START of the NEXT prep run for the SAME project - so a project enriched once and
never again kept its copies indefinitely. Measured before the fix on a real machine: 29 staged
turns across 20 project dirs, oldest 8 days.

_sweep ties the lifetime to PURPOSE instead of to the next run: the moment an overlay is applied
through the gateway, its staged copy has done its whole job.

The tests that matter most here are the ones that keep the sweep NARROW. Deletion is never a
default, and a sweep that is slightly too eager destroys work a retry needed - so every
"must be removed" case below is paired with a "must be kept" case.
"""
import json
import os
import sys
import tempfile

import pytest

os.environ.setdefault("PARSICA_WEAVE_ROOT", tempfile.mkdtemp(prefix="sweep_boot_"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import enrich_apply  # noqa: E402


def _inbox(tmp_path):
    """A minimally realistic inbox: the ownership marker plus a parent to prove the chain from."""
    parent = tmp_path / "parsica_enrich_inbox"
    inbox = parent / "proj-test"
    os.makedirs(str(inbox))
    (inbox / ".parsica-weave-inbox").write_text("", encoding="utf-8")
    return inbox, parent


def _stage(inbox, cid, overlay=True):
    (inbox / (cid + ".txt")).write_text("USER: hello\nASSISTANT: hi", encoding="utf-8")
    if overlay:
        (inbox / (cid + ".overlay.json")).write_text(
            json.dumps({"summary": "s", "keywords": ["k"]}), encoding="utf-8")


def _names(inbox):
    return sorted(p.name for p in inbox.iterdir())


# --------------------------------------------------------------------------- it removes


def test_an_applied_chunk_loses_both_staged_files(tmp_path):
    inbox, parent = _inbox(tmp_path)
    _stage(inbox, "abc123")
    removed = enrich_apply._sweep(inbox, parent, {"abc123"})
    assert removed == 2, _names(inbox)
    assert _names(inbox) == [".parsica-weave-inbox"], \
        "the staged plaintext outlived the enrichment it was staged for: %s" % _names(inbox)


def test_the_sweep_scales_to_every_applied_id(tmp_path):
    inbox, parent = _inbox(tmp_path)
    for cid in ("a", "b", "c"):
        _stage(inbox, cid)
    assert enrich_apply._sweep(inbox, parent, {"a", "b", "c"}) == 6
    assert _names(inbox) == [".parsica-weave-inbox"]


# --------------------------------------------------------------------------- it KEEPS


def test_an_unapplied_chunk_keeps_its_staged_copy(tmp_path):
    """The load-bearing companion. A chunk whose overlay was malformed (or that a worker never
    reached) is NOT in applied_ids, and its .txt is the only thing a retry has to work from.
    Sweeping it would silently convert a recoverable failure into lost work."""
    inbox, parent = _inbox(tmp_path)
    _stage(inbox, "landed")
    _stage(inbox, "malformed")
    removed = enrich_apply._sweep(inbox, parent, {"landed"})
    assert removed == 2
    assert "malformed.txt" in _names(inbox), \
        "swept a chunk that never landed - a retry has nothing left to read: %s" % _names(inbox)
    assert "malformed.overlay.json" in _names(inbox)


def test_the_ownership_marker_is_never_swept(tmp_path):
    """The marker is what proves the inbox is Weave-owned; deleting it would make the NEXT apply
    refuse the directory outright."""
    inbox, parent = _inbox(tmp_path)
    _stage(inbox, "abc")
    enrich_apply._sweep(inbox, parent, {"abc", ".parsica-weave-inbox"})
    assert (inbox / ".parsica-weave-inbox").exists()


def test_unrelated_files_are_never_swept(tmp_path):
    """Only the two suffixes this pipeline writes. Anything else in that directory belongs to
    someone else and is none of our business, whatever its stem."""
    inbox, parent = _inbox(tmp_path)
    _stage(inbox, "abc")
    (inbox / "abc.log").write_text("x", encoding="utf-8")
    (inbox / "abc").write_text("x", encoding="utf-8")
    enrich_apply._sweep(inbox, parent, {"abc"})
    assert "abc.log" in _names(inbox) and "abc" in _names(inbox), _names(inbox)


def test_an_empty_applied_set_sweeps_nothing(tmp_path):
    """Belt and braces on the caller's gate: even reached with no applied ids, it removes zero."""
    inbox, parent = _inbox(tmp_path)
    _stage(inbox, "abc")
    assert enrich_apply._sweep(inbox, parent, set()) == 0
    assert "abc.txt" in _names(inbox)


# --------------------------------------------------------------------------- it never crashes


def test_a_missing_inbox_returns_zero_rather_than_raising(tmp_path):
    """The sweep runs AFTER a successful apply. A failure here must never turn a completed
    enrichment into a crashed stage - the handshake the workflow parses has already earned its
    'applied' count."""
    parent = tmp_path / "parsica_enrich_inbox"
    os.makedirs(str(parent))
    assert enrich_apply._sweep(parent / "gone", parent, {"abc"}) == 0


def test_an_undeletable_file_is_survived(tmp_path, monkeypatch):
    inbox, parent = _inbox(tmp_path)
    _stage(inbox, "abc")

    import pathlib
    real = pathlib.Path.unlink

    def boom(self, *a, **k):
        if self.name.endswith(".txt"):
            raise OSError(13, "denied")
        return real(self, *a, **k)

    monkeypatch.setattr(pathlib.Path, "unlink", boom)
    assert enrich_apply._sweep(inbox, parent, {"abc"}) == 1      # the overlay went, the txt stayed
    assert "abc.txt" in _names(inbox)


# --------------------------------------------------------------------------- the handshake


def test_the_workflow_schema_accepts_the_swept_key():
    """The apply stage's schema is additionalProperties:false. A key the stage PRINTS but the
    schema omits fails validation and turns a successful apply into a stage error - the exact
    failure test_enrich_apply_guard.py exists for. Adding `swept` to the output without adding it
    to the schema would have reintroduced it, so pin the pair together."""
    import re
    wf = os.path.join(os.path.dirname(os.path.abspath(__file__)), "enrich-swarm.workflow.js")
    with open(wf, encoding="utf-8") as fh:
        src = fh.read()
    assert "additionalProperties: false" in src
    assert re.search(r"swept:\s*\{\s*type:\s*'number'\s*\}", src), \
        "enrich_apply prints `swept` but the workflow schema does not allow it"


def test_apply_advertises_swept_in_its_output_contract():
    """The stage prompt tells the worker what shape to expect; drifting it from the real output
    is how a worker starts 'helpfully' reshaping the JSON."""
    wf = os.path.join(os.path.dirname(os.path.abspath(__file__)), "enrich-swarm.workflow.js")
    with open(wf, encoding="utf-8") as fh:
        src = fh.read()
    assert '"swept":S' in src or '"swept": S' in src, \
        "the apply prompt still advertises the pre-sweep output shape"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
