#!/usr/bin/env python3
"""W-D3 sibling: _format_pairing carried the identical unguarded field access _mirror_index did,
in a function _pair_query's own per-chunk try/except does NOT protect. That guard only wraps the
call to query._score - and since W-D3's fix made _score coerce rather than raise, a poisoned chunk
(user/asst/enrich.summary holding a list) now scores WITHOUT raising and is returned as a hit. The
render step is a separate, later use of the same raw chunk, and a raise there is caught nowhere
below handle_posttooluse's fail-silent wrapper - so one poisoned chunk would have silenced pairing
for the WHOLE PostToolUse(Grep) event, not just its own hit.

These pin: a poisoned hit does not crash the render, a poisoned hit drops alone while clean hits
around it still render, and the query._score interaction that lets a poisoned chunk reach this
function at all in the first place.
"""
import os
import sys
import tempfile

os.environ.setdefault("PARSICA_WEAVE_ROOT", tempfile.mkdtemp(prefix="wd3b_boot_"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store   # noqa: E402
import pcx     # noqa: E402
import query   # noqa: E402


def test_poisoned_hit_does_not_crash_the_render():
    hits = [
        {"ts": "2026-07-20T10:00:00Z", "sess": "s1", "user": "first good hit"},
        {"ts": "2026-07-20T11:00:00Z", "sess": "s2", "user": ["poisoned", "list"]},   # was fatal
        {"ts": "2026-07-20T12:00:00Z", "sess": "s3", "enrich": {"summary": ["also", "poisoned"]}},
        {"ts": "2026-07-20T13:00:00Z", "sess": "s4", "user": "last good hit"},
    ]
    out = pcx._format_pairing("term", hits)
    assert out, "the whole pairing render was suppressed by a single poisoned hit"
    assert "first good hit" in out and "last good hit" in out, out
    assert "poisoned" not in out   # the bad hits never render their raw list content


def test_all_hits_poisoned_still_returns_the_header_not_a_raise():
    hits = [{"ts": "2026-07-20T10:00:00Z", "sess": "s1", "user": ["only", "poisoned"]}]
    out = pcx._format_pairing("term", hits)   # must not raise
    assert out.startswith("[store] Prior context")


def test_well_formed_hits_render_as_before():
    hits = [
        {"ts": "2026-07-20T10:00:00Z", "sess": "s1", "user": "multi\nline turn"},
        {"ts": "2026-07-20T11:00:00Z", "sess": "s2", "user": "", "asst": "assistant carried it"},
        {"ts": "2026-07-20T12:00:00Z", "sess": "s3", "enrich": {"summary": "a clean summary"}},
    ]
    out = pcx._format_pairing("term", hits)
    assert "multi line turn" in out            # \n -> space, unchanged behavior
    assert "assistant carried it" in out       # user='' still falls through to asst
    assert "a clean summary" in out            # enrich.summary still wins when present


# --- the confluence that made this reachable in the first place -----------------------------

def test_a_poisoned_chunk_now_scores_without_raising_and_reaches_format_pairing():
    """This is the mechanism, not just the symptom: post-W-D3-fix, query._score no longer raises
    on a list-valued field, so a poisoned chunk with a term match elsewhere legitimately scores and
    is returned by _pair_query as a hit - _format_pairing is the very next thing that touches it."""
    import types
    poisoned = {"id": "m1", "user": ["poisoned", "term", "list"], "asst": "",
                "files": "alpha.py", "cmds": [1]}
    a = types.SimpleNamespace(grep="term", file=None, cmd=None, tool=None, category=None, since=None)
    ok, sc = query._score(poisoned, a, {})
    assert isinstance(ok, bool)          # does not raise
    # whether or not this particular chunk clears threshold, the render step must survive it either way
    out = pcx._format_pairing("term", [poisoned])
    assert out.startswith("[store] Prior context")


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
