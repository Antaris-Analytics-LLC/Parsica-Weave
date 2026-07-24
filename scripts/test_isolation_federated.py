#!/usr/bin/env python3
"""Regression (B3): an ISOLATED federated read leaves ZERO footprint in BOTH roots.

The isolation matrix (test_isolation_matrix.py) runs every CLI against a FRESH, empty
PARSICA_WEAVE_ROOT - so `query.py --grep` federates against a workspace with no minted id and
NO Spirit store, and degrades to Weave-only before it ever opens the durable pool. That leaves
the one configuration where the bug actually lives untested: a workspace that ALREADY has a
minted id AND an EXISTING Spirit store on disk. In that state an isolated `query.py --grep`
really does reach into the pool - and the pre-fix federated read opened the markdown backend
read/WRITE, whose refresh-under-lock created a `.parsica_md.lock` inside the Spirit store: a
durable byte written by a session that promised to write nothing.

This test builds BOTH roots for real, clears the advisory lock the fixture write left behind (so
a lock appearing afterward can only have come from the read under test), runs the real
`query.py --grep` CLI as a subprocess under PARSICA_ISOLATED=1, and asserts:
  * federation DEMONSTRABLY ran - the Spirit record's content is in stdout (a vacuous green,
    where federation silently degraded, would miss the bug entirely); and
  * NOTHING new or changed appears under EITHER the Weave root or the Spirit root - no
    .parsica_md.lock, no mutated records.

Skips cleanly where parsica_spirit is not installed (the pure-Weave install has no pool to
federate against, exactly as the degrade path intends).
"""
import os
import sys
import json
import shutil
import tempfile
import subprocess

import pytest

SCRIPTS = os.path.dirname(os.path.abspath(__file__))

# 24 lowercase hex = the workspace_id shape store._WORKSPACE_ID_RE requires.
_SID = "abcdef0123456789abcdef01"
_SENTINEL = "zzfedsentinelzz"          # a distinctive single grep token -> proves the read ran


def _tree(root):
    """Every file under root -> (size, mtime_ns, bytes). A new path (e.g. .parsica_md.lock), a
    changed size/mtime, or altered content all surface as a dict diff."""
    out = {}
    for dp, _dirs, files in os.walk(root):
        for name in files:
            p = os.path.join(dp, name)
            st = os.stat(p)
            with open(p, "rb") as fh:
                out[os.path.relpath(p, root)] = (st.st_size, st.st_mtime_ns, fh.read())
    return out


def _build_spirit_store(sroot):
    """A real Spirit store at sroot, holding one scope-labeled record whose content carries the
    sentinel token. Built via the actual library so records.jsonl is exactly what federation reads."""
    from parsica_spirit import open_markdown
    ctx = open_markdown(sroot)                       # read/write open: scaffolds + stores for real
    ctx.store("%s a federated durable record" % _SENTINEL,
              labels={"scope_id": _SID, "source_kind": "note",
                      "created": "2026-07-13T00:00:00.000Z"})


def test_isolated_federated_read_zero_footprint_both_roots():
    pytest.importorskip("parsica_spirit")

    weave_root = tempfile.mkdtemp(prefix="isofed_weave_")
    spirit_parent = tempfile.mkdtemp(prefix="isofed_spirit_")
    try:
        # (1) an ALREADY-minted Weave workspace (federation reads this id; create=False, so an
        #     isolated session returns it without minting).
        with open(os.path.join(weave_root, "workspace.json"), "w", encoding="utf-8") as fh:
            json.dump({"workspace_id": _SID}, fh)

        # (2) an EXISTING Spirit store the federated read will open.
        sroot = os.path.join(spirit_parent, _SID)
        _build_spirit_store(sroot)

        # the fixture WRITE legitimately left a lock; clear it so any lock afterward is the read's.
        lock = os.path.join(sroot, ".parsica_md.lock")
        if os.path.exists(lock):
            os.remove(lock)
        assert ".parsica_md.lock" not in _tree(sroot), "baseline must have no lock"

        before_weave = _tree(weave_root)
        before_spirit = _tree(spirit_parent)

        env = dict(os.environ,
                   PARSICA_ISOLATED="1",
                   PARSICA_WEAVE_ROOT=weave_root,
                   PARSICA_SPIRIT_ROOT=spirit_parent,
                   PARSICA_CHANNEL="",
                   PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
        env.pop("PARSICA_ROUTER", None)          # ensure the router is not switched off
        cp = subprocess.run(
            [sys.executable, os.path.join(SCRIPTS, "query.py"), "--grep", _SENTINEL],
            input="", capture_output=True, text=True, env=env, timeout=60)

        # (a) federation DEMONSTRABLY engaged: the durable record surfaced in the output. Without
        #     this, a silent degrade would sail past the footprint check with nothing exercised.
        assert _SENTINEL in cp.stdout, (
            "federated read did not surface the Spirit record (federation did not engage) - "
            "stdout=%r stderr=%r" % (cp.stdout, cp.stderr))

        # (b) zero footprint in BOTH roots - the read created no lock and mutated nothing.
        after_weave = _tree(weave_root)
        after_spirit = _tree(spirit_parent)
        assert after_weave == before_weave, (
            "isolated federated read changed the WEAVE root: added=%r removed=%r"
            % (sorted(set(after_weave) - set(before_weave)),
               sorted(set(before_weave) - set(after_weave))))
        assert after_spirit == before_spirit, (
            "isolated federated read changed the SPIRIT root: added=%r removed=%r"
            % (sorted(set(after_spirit) - set(before_spirit)),
               sorted(set(before_spirit) - set(after_spirit))))
        assert ".parsica_md.lock" not in _tree(sroot), (
            "isolated federated read created the advisory lock inside the Spirit store")
    finally:
        shutil.rmtree(weave_root, ignore_errors=True)
        shutil.rmtree(spirit_parent, ignore_errors=True)
