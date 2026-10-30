#!/usr/bin/env python3
"""Two v1.0.1 fixes, both polarities each (2026-07-30).

CONFIG REWRITE FAIL-CLOSED: save_global_config / write_policy_list used the READ-side
degradation (unparseable -> {}) on a read-modify-WRITE path, so one JSON typo plus one
config edit atomically published an empty config - destroying every other setting,
refusal and human-only lists included. Now a present-but-unreadable file REFUSES the
write (ValueError, nothing written); an absent file still starts empty.

TORN-TAIL GUARD: a write that died mid-line left a partial record with no trailing
newline, and the next append GLUED onto it - the concatenated line parsed as neither
record, so one torn write destroyed the next good record too. The appender now isolates
a torn tail on its own line; the fragment stays lost to its own fault, the new record
survives.
"""
import io
import json
import os
import sys
import tempfile

import pytest

os.environ.setdefault("PARSICA_WEAVE_ROOT", tempfile.mkdtemp(prefix="cfgtail_root_"))
SCRIPTS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPTS)
import store  # noqa: E402


# ------------------------------------------------------- config rewrite fail-closed


def test_a_typo_in_global_config_refuses_the_write_and_preserves_the_file():
    store.GLOBAL_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    before = '{"agent_name": "Keeper", "refusal": ["term-alpha"],  TYPO'
    store.GLOBAL_CONFIG.write_text(before, encoding="utf-8")
    try:
        with pytest.raises(ValueError) as exc:
            store.save_global_config({"agent_name": "NewName"})
        assert "refusing to rewrite" in str(exc.value)
        assert store.GLOBAL_CONFIG.read_text(encoding="utf-8") == before, \
            "the refused write still touched the file"
    finally:
        store.GLOBAL_CONFIG.unlink()


def test_a_scalar_global_config_refuses_the_write():
    store.GLOBAL_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    store.GLOBAL_CONFIG.write_text("42", encoding="utf-8")
    try:
        with pytest.raises(ValueError):
            store.save_global_config({"agent_name": "NewName"})
    finally:
        store.GLOBAL_CONFIG.unlink()


def test_polarity_absent_and_healthy_configs_still_write():
    """The false-positive companion: the refusal must never block the legitimate path."""
    if store.GLOBAL_CONFIG.exists():
        store.GLOBAL_CONFIG.unlink()
    out = store.save_global_config({"agent_name": "Fresh"})     # absent -> empty start
    assert out.get("agent_name") == "Fresh"
    out = store.save_global_config({"agent_name": "Again"})     # healthy -> normal patch
    assert out.get("agent_name") == "Again"
    doc = json.loads(store.GLOBAL_CONFIG.read_text(encoding="utf-8"))
    assert doc["agent_name"] == "Again"
    store.GLOBAL_CONFIG.unlink()


def test_policy_list_twin_refuses_on_a_corrupt_file(tmp_path):
    """write_policy_list holds the policy lists THEMSELVES - the sharper instance of the
    same wipe. A corrupt file must refuse the edit and keep its bytes."""
    cwd = tempfile.mkdtemp(prefix="cfgtail_proj_")
    store.ensure_workspace(cwd)
    path = store._policy_config_path("workspace", cwd)
    path.parent.mkdir(parents=True, exist_ok=True)
    before = '{"human_only_terms": ["secret-project"], "refusal": ["te'
    path.write_text(before, encoding="utf-8")
    try:
        with pytest.raises(ValueError):
            store.write_policy_list("refusal", "workspace", ["new-term"], cwd)
        assert path.read_text(encoding="utf-8") == before, "the refused policy write touched the file"
    finally:
        # this IS the workspace config: since October 2026 a corrupt config fails every later read
        # closed (StoreReadError), so it must never be left behind for the tests that follow
        path.unlink()


# ----------------------------------------------------------------- torn-tail guard


def _mirror_shard(cwd):
    md = store.mirror_dir(cwd)
    store._verified_dir(md, store.STORE_ROOT, 0o700)
    return md / "2026-07-30.99999.jsonl"


def test_a_torn_tail_no_longer_destroys_the_next_good_record():
    cwd = tempfile.mkdtemp(prefix="torn_proj_")
    store.ensure_workspace(cwd)
    cfg = store.load_config(cwd)
    shard = _mirror_shard(cwd)
    # a good record, then a TORN write (partial JSON, no trailing newline)
    shard.write_text(json.dumps({"id": "good-1", "text": "survives"}) + "\n"
                     + '{"id": "torn-1", "text": "died mid-wr', encoding="utf-8")
    store.append_record(shard, {"id": "good-2", "text": "must survive"}, cfg,
                        content_fields_scrubbed=True)
    rows = store.read_jsonl_rows(shard)
    ids = [r.get("id") for r in rows]
    assert "good-2" in ids, "the appended record was glued onto the torn tail and lost"
    assert "good-1" in ids
    raw = shard.read_text(encoding="utf-8")
    assert 'died mid-wr\n{"id": "good-2"' in raw.replace('", "text', '","text') or \
        "died mid-wr" in raw.splitlines()[1], "the torn fragment must sit isolated on its own line"


def test_polarity_a_clean_tail_gets_no_extra_blank_line():
    cwd = tempfile.mkdtemp(prefix="clean_proj_")
    store.ensure_workspace(cwd)
    cfg = store.load_config(cwd)
    shard = _mirror_shard(cwd)
    store.append_record(shard, {"id": "r1", "text": "first"}, cfg, content_fields_scrubbed=True)
    store.append_record(shard, {"id": "r2", "text": "second"}, cfg, content_fields_scrubbed=True)
    lines = shard.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2 and all(l.strip() for l in lines), \
        "the guard inserted a spurious blank line on a healthy file: %r" % lines
    assert len(store.read_jsonl_rows(shard)) == 2


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
