#!/usr/bin/env python3
"""A decision's `did` must be reachable from the surface the docs point at.

SKILL.md tells the operator to record a calibration event "against its `did` (visible in the
stored overlay / `query.py --full`)". The overlay half was true; the `query.py --full` half was
not. `_fmt_inner` read `d.get("did")` only to look up the trajectory and never emitted it, so
the entire calibration loop - the feature README, SKILL.md and ONBOARDING all advertise - had no
reachable entry point short of grepping raw enrich JSONL, which no doc mentions. The machinery
underneath worked the whole time; only the handle was missing.

So the load-bearing assertion here is not "some id appears" but ROUND-TRIP: lift the did out of
rendered `--full` output the way a user would, feed it straight to `calibrate.record`, and watch
the calibration actually move. That is the documented loop, end to end, with no privileged
knowledge of the store's internals.

The companion pulls the other way. A did arrives from a worker-written overlay, so it is
untrusted text at a terminal sink. calibrate's `_target_is_clean` is the codebase's ONE display
oracle for exactly this ("SHARED by record() ... and every ECHO of a raw --target"), and its
posture is withhold-never-redact: an id is echoed verbatim or not at all, because a
half-scrubbed did would round-trip into --target as a different, wrong key. These tests pin both
directions, since a fix that printed every did raw would pass the first half alone.
"""
import json
import os
import re
import sys
import tempfile

import pytest

os.environ.setdefault("PARSICA_WEAVE_ROOT", tempfile.mkdtemp(prefix="didshow_boot_"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import calibrate  # noqa: E402
import query  # noqa: E402
import store  # noqa: E402

DID = "a1b2c3d4e5f6"          # the real shape: sha1(decision text)[:12], enrich.py:320


def _chunk(did=DID, text="ship the sweep before the README pass", value=0.8):
    d = {"text": text, "conviction": {"kind": "conviction_at_creation", "value": value,
                                      "at": "2026-07-26T12:00:00Z"}}
    if did is not None:
        d["did"] = did
    return {"ts": "2026-07-26T12:00:00.000Z", "sess": "s-abcdefgh",
            "user": "which way on the sweep", "asst": "shipping it",
            "enrich": {"summary": "a decision worth calibrating", "decisions": [d]}}


def _render(chunk, full=True, stats=None):
    return query._fmt(chunk, full, stats or {})


# --------------------------------------------------------------- the handle is actually printed


def test_full_output_prints_the_did():
    """The bug, stated plainly."""
    assert DID in _render(_chunk()), "query.py --full renders no did - the calibration loop " \
                                    "documented in SKILL.md has no entry point"


def test_a_user_can_lift_the_did_and_calibrate_with_it(tmp_path, monkeypatch):
    """The whole point, end to end: copy the id out of rendered output, run the command SKILL.md
    prints, and confirm the calibration MOVED. Nothing here reaches into the overlay - if the
    only path to a did were internal knowledge of the store, this test could not be written."""
    monkeypatch.setattr(store, "STORE_ROOT", tmp_path)
    monkeypatch.setattr(store, "GLOBAL_CONFIG", tmp_path / "config.json")   # moves with the root
    cwd = str(tmp_path / "proj")
    os.makedirs(cwd, exist_ok=True)
    store.ensure_workspace(cwd)

    rendered = _render(_chunk())
    found = re.search(r"did ([A-Za-z0-9_.:\-]{1,96})", rendered)
    assert found, "no copy-pasteable did in the rendered output: %r" % rendered
    lifted = found.group(1)

    assert calibrate.record(cwd, lifted, "confirmed", source="test_decision_did_display"), \
        "the did lifted from --full was REFUSED by calibrate.record - the printed handle and " \
        "the accepted handle have drifted apart"
    before = calibrate.trajectory(0.8, lifted, stats={})
    after = calibrate.trajectory(0.8, lifted, stats=calibrate.load_stats(cwd))
    assert after["calibration"] > before["calibration"], \
        "recording a confirmation against the printed did did not move the calibration"


def test_the_printed_did_is_verbatim_not_reformatted():
    """An id survives display or is withheld; it is never prettified. A truncated or case-folded
    did would look right, copy cleanly, and silently address a different ledger key."""
    odd = "A9_b.c:d-e01"          # every character class _VALID_TARGET admits
    out = _render(_chunk(did=odd))
    assert odd in out, "the did was altered in transit: %r" % out


# --------------------------------------------------------------------- the oracle still withholds


@pytest.mark.parametrize("dirty", [
    # U+FFFD as an ESCAPE, not a literal: this repo's public surface is plain ASCII, and a raw
    # replacement char in a source file is the exact byte a denylist is supposed to catch.
    "corrupt\ufffdline",                     # the mangled-shard case test_calibrate.py pins
    "did with spaces",
    "id\nevent=confirmed",                   # a newline would forge a second ledger row downstream
    "x" * 200,                               # over the 96-char id ceiling
])
def test_a_display_unsafe_did_is_withheld_not_echoed(dirty):
    """Withhold, never redact. Anything the input boundary would refuse must not be echoed at the
    display sink either - one oracle, so the two can never drift (validate at the effect)."""
    assert not calibrate._target_is_clean(dirty), "test vector is not actually unsafe: %r" % dirty
    out = _render(_chunk(did=dirty))
    assert dirty not in out, "an unsafe did was echoed raw to the terminal: %r" % out
    assert "| did " not in out, "a withheld did left a dangling, uncopyable label: %r" % out


def test_a_withheld_did_still_renders_the_rest_of_the_trajectory():
    """Withholding the handle must not cost the operator the conviction reading beside it. A
    guard that blanked the whole line would pass the test above and still lose information."""
    out = _render(_chunk(did="corrupt\ufffdline"))
    assert "conviction 0.8" in out and "calibration" in out and "effective" in out, out
    assert "ship the sweep" in out, "the decision text vanished with its did"


def test_a_decision_with_no_did_at_all_renders():
    """Older overlays predate dids entirely. They render; they just offer no handle."""
    out = _render(_chunk(did=None))
    assert "ship the sweep" in out and "conviction 0.8" in out, out
    assert "| did " not in out, out


# ------------------------------------------------------------------------- scope of the change


def test_the_compact_view_is_unchanged():
    """--full is the detail surface; the default one-line-per-hit view stays a gist. A did on
    every compact row would be noise in the surface used a hundred times more often."""
    out = _render(_chunk(), full=False)
    assert DID not in out, "the compact view grew a did: %r" % out


def test_a_malformed_decision_entry_still_cannot_crash_retrieval():
    """_fmt's blanket guard makes a crash here look like a skipped chunk rather than an error, so
    the failure mode this pins is silent. Non-dict decisions, and a conviction of the wrong type,
    are both already-tolerated shapes - the did lookup must not have narrowed that."""
    c = _chunk()
    c["enrich"]["decisions"] = ["not a dict", {"did": DID, "text": "ok", "conviction": "bad"}]
    out = _render(c)
    assert "unreadable chunk" not in out, "a malformed decision broke rendering: %r" % out
    assert DID in out and "conviction 0.5" in out, out


def test_the_docs_still_point_here():
    """This fix is only correct while SKILL.md keeps naming `query.py --full` as the place to
    find a did. If that sentence is ever rewritten, this test should be revisited, not deleted."""
    skill = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "skills", "weave", "SKILL.md")
    if not os.path.exists(skill):
        pytest.skip("SKILL.md not present in this layout")
    text = open(skill, encoding="utf-8").read()
    assert "query.py --full" in text and "did" in text, \
        "SKILL.md no longer points at query.py --full for a decision's did"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
