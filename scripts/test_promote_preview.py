#!/usr/bin/env python3
"""promote --dry-run must name what it will promote, not just count it.

The dry run used to print totals only ("store: 34"). An operator was told how MANY items land
and nothing about WHICH, so the only way to find out was to let them land - the one outcome a
dry run exists to prevent, and directly against promote's own instruction to promote only what
you want to keep.

The property is PARITY, and that is what these tests measure rather than assert by hand: the
preview is generated from a plan, the SAME plan is then really applied against a throwaway Spirit
store with the applier's own progress callback captured, and every line the commit emits has to
have been named by the preview first. A test that only checked the preview against a hand-copied
format string would keep passing if the applier's line shape drifted - the two would disagree and
nothing would say so.
"""
import io
import os
import sys
import tempfile
from contextlib import redirect_stdout

import pytest

os.environ.setdefault("PARSICA_WEAVE_ROOT", tempfile.mkdtemp(prefix="preview_boot_"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pcx  # noqa: E402

spirit = pytest.importorskip("parsica_spirit", reason="promote preview needs Parsica Spirit")


def _candidates(n, scope_id):
    from parsica_spirit.promote import PromotionCandidate
    # pk is normally minted by collect_candidates from the store's salt; the constructor refuses a
    # missing one because every such candidate would collapse onto a single content id. Distinct
    # per-candidate values here keep them distinct, which is the only property these tests need.
    return [PromotionCandidate(scope_id=scope_id, source_project="proj",
                               source_kind="lesson",
                               source_record_id="lesson:2026-07-26:%d" % i,
                               body="Lesson number %d\nsecond line never shown" % i,
                               pk="pk-%s-%04d" % (scope_id[:8], i))
            for i in range(n)]


def _prefix(line):
    """The identifying head of a progress/preview line: everything up to the preview's excerpt
    separator. The commit has no excerpt, so this is the whole line there."""
    return line.split("  |  ", 1)[0].strip()


# --- the excerpt ---------------------------------------------------------------

def test_excerpt_is_one_collapsed_line():
    assert pcx._body_excerpt("first  line\n\nsecond") == "first line"


def test_excerpt_skips_leading_blank_lines():
    assert pcx._body_excerpt("\n   \nreal content") == "real content"


def test_excerpt_truncates_long_text_visibly():
    out = pcx._body_excerpt("x" * 300)
    assert out.endswith("..."), "a cut excerpt must SHOW that it was cut"
    assert len(out) <= 71


def test_excerpt_of_empty_body_is_stated_not_blank():
    assert pcx._body_excerpt("") == "(no text)"
    assert pcx._body_excerpt(None) == "(no text)"


# --- the no-cap property -------------------------------------------------------

def test_preview_names_every_item_with_no_cap():
    """A preview that silently drops its tail reads as complete and is worse than the counts it
    replaced. 250 actions in, 250 lines out, numbered through to the last one."""
    from parsica_spirit.promote import PromotionAction
    plan = [PromotionAction("store", c) for c in _candidates(250, "scope-nocap")]
    buf = io.StringIO()
    with redirect_stdout(buf):
        pcx._emit_plan_preview(plan)
    lines = [ln for ln in buf.getvalue().splitlines() if ln.strip()]
    assert len(lines) == 250
    assert lines[0].startswith("[1/250] ")
    assert lines[-1].startswith("[250/250] ")


# --- the parity property (the load-bearing one) --------------------------------

def test_preview_names_every_item_the_commit_names(tmp_path):
    """Generate a plan, preview it, then really apply it and capture what the applier says.
    Every line the commit emits must already have been named by the preview."""
    from parsica_spirit import open_markdown
    from parsica_spirit.promote import (RouterScope, SyncLedger, apply_promotion,
                                        plan_promotion)
    sid = "0123456789abcdef01234567"
    root = tmp_path / "spirit"
    root.mkdir()
    ctx = open_markdown(str(root))
    ctx.scope_identity(sid)
    ledger = SyncLedger(root, sid)
    cands = _candidates(12, sid)
    plan = plan_promotion(ctx, RouterScope(scope_id=sid), cands, ledger)
    assert plan, "no plan to compare - the fixture would prove nothing"

    buf = io.StringIO()
    with redirect_stdout(buf):
        pcx._emit_plan_preview(plan)
    previewed = [_prefix(ln) for ln in buf.getvalue().splitlines() if ln.strip()]

    committed = []
    apply_promotion(ctx, plan, ledger, progress=committed.append)
    committed = [_prefix(ln) for ln in committed if ln.strip()]
    assert committed, "the applier emitted nothing - parity would be vacuous"

    missing = [ln for ln in committed if ln not in previewed]
    assert not missing, "the commit named items the preview did not: %r" % (missing[:5],)


def test_dry_run_still_writes_nothing(tmp_path, monkeypatch, capsys):
    """The listing must not have turned the preview into a write. A dry run against a store that
    does not exist yet must not even scaffold one."""
    import store
    monkeypatch.setattr(store, "workspace_id", lambda *a, **k: "0123456789abcdef01234567")
    spirit_root = tmp_path / "no_store_here"
    monkeypatch.setenv("PARSICA_SPIRIT_ROOT", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["pcx.py", "promote", "--dry-run", "--cwd", str(tmp_path)])
    try:
        pcx.handle_promote(["promote", "--dry-run", "--cwd", str(tmp_path)])
    except SystemExit as exc:
        assert exc.code in (0, None), "dry run exited %r" % (exc.code,)
    assert "No writes performed." in capsys.readouterr().out
    assert not spirit_root.exists(), "the dry run scaffolded a Spirit store"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
