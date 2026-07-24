#!/usr/bin/env python3
"""RC5L-E family 2: a malformed record shape must not reach the active store, and must not crash a read.

import_jsonl only checked isinstance(rec, dict), so a legacy shard carrying `{"files": [7]}` landed
in the active mirror verbatim; query._fmt then did `",".join(chunk["files"][:4])` on the raw list and
raised `TypeError: sequence item 0: expected str instance, int found`, aborting the WHOLE query - every
valid sibling row included - because main() has no enclosing guard.

Two layers, both pinned here:
  * PRIMARY - store.normalize_import_row quarantines a shape-invalid row at import (counted, visible),
    so the store never holds a `files: [7]`. A bare string coerces to a one-item list (legacy compat).
  * DEFENSE - query._strlist coerces the three collection joins, and query._fmt never raises: a row
    corrupted by a later hand-edit drops to a marked placeholder while its siblings still render.

Both-directions twins: a fully valid shard imports with ZERO quarantines, and a valid chunk renders
byte-identically to before.
"""
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("PARSICA_WEAVE_ROOT", tempfile.mkdtemp(prefix="rc5le2_boot_"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store   # noqa: E402
import query   # noqa: E402


def _fresh():
    tmp = tempfile.mkdtemp(prefix="rc5le2_root_")
    store.STORE_ROOT = Path(tmp)
    store.GLOBAL_CONFIG = store.STORE_ROOT / "config.json"
    return Path(tmp)


def _import(records, dataset="mirror"):
    """Write a shard and import it, returning (imported, quarantined, active_text)."""
    root = _fresh()
    src_dir = root / "silo" / dataset
    src_dir.mkdir(parents=True, exist_ok=True)
    shard = src_dir / "2026-07-20.4242.jsonl"
    shard.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    dst = store.STORE_ROOT / "mirror" / "proj-0123456789" / shard.name
    cfg = store.load_config(None)
    imp, q = store.import_jsonl(shard, dst, "proj-0123456789", dataset, cfg, root=src_dir)
    text = dst.read_text(encoding="utf-8") if dst.exists() else ""
    return imp, q, text


# --------------------------------------------------------------------------
# normalize_import_row: the shape gate in isolation
# --------------------------------------------------------------------------

def test_collection_with_non_str_member_is_rejected():
    _fresh()
    row, reason = store.normalize_import_row("mirror", {"files": [7]})
    assert row is None and "files" in reason

def test_bare_string_collection_is_coerced_to_one_item():
    _fresh()
    row, reason = store.normalize_import_row("mirror", {"files": "alpha.py"})
    assert reason is None and row["files"] == ["alpha.py"], "bare str must become a one-item list"

def test_valid_collection_passes_verbatim():
    _fresh()
    row, reason = store.normalize_import_row("mirror", {"files": ["a.py", "b.py"], "cmds": ["pytest"]})
    assert reason is None and row["files"] == ["a.py", "b.py"] and row["cmds"] == ["pytest"]

def test_text_field_as_list_is_rejected():
    _fresh()
    row, reason = store.normalize_import_row("mirror", {"user": ["poisoned", "list"]})
    assert row is None and "user" in reason

def test_enrich_list_field_shape_is_checked():
    _fresh()
    bad, r1 = store.normalize_import_row("enrich", {"topic": [7]})
    assert bad is None and "topic" in r1
    good, r2 = store.normalize_import_row("enrich", {"topic": ["auth"], "summary": "ok"})
    assert r2 is None and good["topic"] == ["auth"]

def test_enrich_decisions_list_of_dicts_passes_untouched():
    """decisions is list[dict], NOT list[str] - it must never be shape-rejected."""
    _fresh()
    rec = {"decisions": [{"text": "retries use backoff", "conviction": {"value": 0.8}}]}
    row, reason = store.normalize_import_row("enrich", rec)
    assert reason is None and row["decisions"] == rec["decisions"]

def test_calibration_family_is_untouched():
    _fresh()
    rec = {"did": "d-1", "event": "confirmed", "weight": 1.0, "target": "test_x"}
    row, reason = store.normalize_import_row("calibration", rec)
    assert reason is None and row == rec


# --------------------------------------------------------------------------
# import_jsonl: the shape gate at the active-store insertion point (PRIMARY)
# --------------------------------------------------------------------------

def test_poisoned_row_is_quarantined_valid_sibling_imports():
    imp, q, text = _import([
        {"ts": "2026-07-20T10:00:00Z", "sess": "s1", "user": "needle alpha", "files": [7]},
        {"ts": "2026-07-20T11:00:00Z", "sess": "s2", "user": "valid needle beta", "files": ["real.py"]},
    ])
    assert imp == 1 and q == 1, "expected 1 imported / 1 quarantined, got %d/%d" % (imp, q)
    assert "valid needle beta" in text, "the valid sibling row must survive"
    assert "[7]" not in text and '"files": [7]' not in text, "the poisoned row reached the store"

def test_valid_shard_imports_with_zero_quarantines():
    """Both-directions twin: a clean shard sails through untouched."""
    imp, q, text = _import([
        {"ts": "2026-07-20T10:00:00Z", "sess": "s1", "user": "one", "files": ["a.py"], "cmds": ["pytest"]},
        {"ts": "2026-07-20T11:00:00Z", "sess": "s2", "user": "two", "tools": ["Edit"]},
    ])
    assert imp == 2 and q == 0, "a valid shard was wrongly quarantined: %d/%d" % (imp, q)


# --------------------------------------------------------------------------
# query._fmt: the render never crashes (DEFENSE in depth for later hand-edits)
# --------------------------------------------------------------------------

def test_fmt_coerces_a_poisoned_collection_without_crashing():
    _fresh()
    out = query._fmt({"ts": "2026-07-20T10:00:00Z", "sess": "s1", "files": [7], "cmds": [1, 2]}, False)
    assert "files=7" in out, out                       # int coerced to text, not a TypeError
    assert "[unreadable" not in out                    # coercion handled it; no placeholder needed

def test_fmt_bare_string_collection_is_not_char_split():
    _fresh()
    out = query._fmt({"ts": "2026-07-20T10:00:00Z", "sess": "s1", "files": "alpha.py"}, False)
    assert "files=alpha.py" in out and "a,l,p,h" not in out

def test_fmt_returns_a_placeholder_when_a_row_truly_cannot_render(monkeypatch):
    _fresh()
    monkeypatch.setattr(query, "_fmt_inner",
                        lambda *a, **k: (_ for _ in ()).throw(ValueError("boom")))
    out = query._fmt({"ts": "x", "sess": "s1"}, False)
    assert out == "[unreadable chunk - malformed fields, skipped]"

def test_fmt_placeholder_carries_no_row_content(monkeypatch):
    """Output-sink discipline: the placeholder must not interpolate the malformed row's fields."""
    _fresh()
    secret = "CODENAME-LEAK-9931"
    monkeypatch.setattr(query, "_fmt_inner",
                        lambda *a, **k: (_ for _ in ()).throw(ValueError(secret)))
    out = query._fmt({"user": secret}, False)
    assert secret not in out

def test_fmt_valid_chunk_renders_normally():
    """Both-directions twin: a clean chunk renders as before, unchanged."""
    _fresh()
    out = query._fmt({"ts": "2026-07-20T10:00:00Z", "sess": "s1",
                      "files": ["a.py", "b.py"], "cmds": ["pytest"]}, False)
    assert "files=a.py,b.py" in out and "cmds=pytest" in out


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
