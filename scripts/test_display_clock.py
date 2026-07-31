#!/usr/bin/env python3
"""The durable-hit header clock can never present fiction confidently (the one-canonical-
timestamp story, 2026-07-29).

The disease this pins against: 423 store records shared one RC5 migration instant in
`created`, and the display rendered it as if it were when the remembered thing happened.
The cure has three parts, each tested: `occurred` wins and renders at the precision its
`clock_source` supports (a synthetic midnight NEVER converts to local time - that would
manufacture a new fake wall-clock reading); `created` appears only explicitly labeled as
pool time; and a hit carrying neither renders no clock at all rather than a guess.
"""
import os
import sys
import tempfile

import pytest

os.environ.setdefault("PARSICA_WEAVE_ROOT", tempfile.mkdtemp(prefix="dispclock_boot_"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import query  # noqa: E402
import store  # noqa: E402


def _clock(**h):
    return query._display_clock(h)


def test_real_event_time_renders_a_full_local_stamp():
    out = _clock(occurred="2026-07-23T14:05:09Z", clock_source="event_ts")
    assert out == store.local_stamp("2026-07-23T14:05:09Z")
    assert out, "a real event time must render"


@pytest.mark.parametrize("src", ["rid_date", "body_date", "created"])
def test_synthetic_precision_renders_the_date_and_never_the_midnight(src):
    out = _clock(occurred="2026-07-03T00:00:00Z", clock_source=src)
    assert out == "2026-07-03", "date-precision clock rendered beyond its precision: %r" % out
    # the specific manufactured-fiction shape: local conversion of a placeholder midnight
    assert "00:00" not in out and "17:" not in out


def test_created_only_is_labeled_pool_time():
    out = _clock(created="2026-07-17T17:27Z")
    assert out.startswith("pooled "), \
        "a pool-clock fallback must SAY it is the pool clock: %r" % out


def test_occurred_beats_created():
    out = _clock(occurred="2026-07-03T00:00:00Z", clock_source="rid_date",
                 created="2026-07-17T17:27Z")
    assert out == "2026-07-03" and "pooled" not in out


def test_no_trustworthy_clock_renders_nothing():
    assert _clock() == ""
    assert _clock(clock_source="unknown") == "", \
        "clock_source=unknown with no occurred is an honest absence, not a guess"


def test_print_durable_old_spirit_hit_renders_exactly_as_before(capsys):
    """An older parsica_spirit's hits carry none of the clock keys - the two-part HEADER
    must survive untouched (federation across versions degrades, never breaks). The body
    gist printed after the header is out of scope here - header line only."""
    query._RF = []
    query._print_durable({"kind": "journal", "id": "abc123", "body": "b"}, full=False)
    header = capsys.readouterr().out.splitlines()[0].rstrip()
    assert header == "[durable] journal · abc123", header


def test_print_durable_shows_the_honest_clock(capsys):
    query._RF = []
    query._print_durable({"kind": "journal", "id": "abc123", "body": "b",
                          "occurred": "2026-07-03T00:00:00Z", "clock_source": "rid_date"},
                         full=False)
    header = capsys.readouterr().out.splitlines()[0].rstrip()
    assert header.endswith("2026-07-03"), header
    assert "00:00" not in header


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
