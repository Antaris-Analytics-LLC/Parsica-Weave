#!/usr/bin/env python3
"""human-only fence - integration tests at the Weave write gateway.

The two open-code floors, the [[human-only]] sentinel and the configured human_only_terms, are
the whole policy: parsica_spirit is NOT on this path and nothing is loaded from the environment
(the import path named by an environment variable was removed in October 2026; see
test_no_environment_loaded_code.py for the proof that the variable now has no effect).
"""
import json
import os
import sys
import tempfile
from pathlib import Path

# set BEFORE the store import so even a first-import (pytest collection order, standalone
# run) can never bind STORE_ROOT to the real HOME store - _fresh() then re-points per test.
os.environ.setdefault("PARSICA_WEAVE_ROOT", tempfile.mkdtemp(prefix="ho_fence_boot_"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store   # noqa: E402
import enrich  # noqa: E402

CWD = os.path.join(tempfile.gettempdir(), "ho_fence_proj")


def _fresh():
    """Point the store at a throwaway root and (re)create the workspace. Reassigns the module
    globals directly, since STORE_ROOT is resolved once at import."""
    tmp = tempfile.mkdtemp(prefix="ho_fence_root_")
    store.STORE_ROOT = Path(tmp)
    store.GLOBAL_CONFIG = store.STORE_ROOT / "config.json"
    store.ensure_workspace(CWD)
    return tmp


def _dir_text(dirpath):
    p = Path(dirpath)
    if not p.is_dir():
        return ""
    return "\n".join(f.read_text(encoding="utf-8") for f in p.iterdir() if f.is_file())


def test_config_union_and_dedup():
    _fresh()
    store.GLOBAL_CONFIG.write_text(json.dumps({"human_only_terms": ["GlobalTerm"]}), encoding="utf-8")
    (store.project_dir(CWD) / "config.json").write_text(
        json.dumps({"human_only_terms": ["ProjTerm", "GlobalTerm"]}), encoding="utf-8")
    cfg = store.load_config(CWD)
    assert "GlobalTerm" in cfg["human_only_terms"]
    assert "ProjTerm" in cfg["human_only_terms"]
    assert cfg["human_only_terms"].count("GlobalTerm") == 1     # unioned + deduped across layers


def test_gateway_blocks_sentinel_record():
    _fresh()
    cfg = store.load_config(CWD)
    shard = store.mirror_dir(CWD) / "2026-07-03.1.jsonl"
    r = store.append_record(shard, {"id": "x1", "user": "forensic [[human-only]] dump", "ts": "t"}, cfg)
    assert r == {"stored": False, "reason": "human_only"}
    assert not shard.exists()                                    # ZERO disk artifacts


def test_gateway_stores_clean_record():
    _fresh()
    cfg = store.load_config(CWD)
    shard = store.mirror_dir(CWD) / "2026-07-03.2.jsonl"
    r = store.append_record(shard, {"id": "ok1", "user": "ordinary note", "ts": "t"}, cfg)
    assert isinstance(r, dict) and r.get("id") == "ok1"          # success still returns the record
    assert shard.exists() and "ordinary note" in shard.read_text(encoding="utf-8")


def test_gateway_blocks_docs_case_and_separator():
    _fresh()
    cfg = store.load_config(CWD)
    p = store.project_dir(CWD) / "w.md"
    assert store.write_doc(p, "keep this [[HUMAN-ONLY]] out", cfg) == {"stored": False, "reason": "human_only"}
    assert not p.exists()
    la = store.project_dir(CWD) / "l.md"
    assert store.append_doc(la, "note [[human_only]] here", cfg) == {"stored": False, "reason": "human_only"}
    assert not la.exists()


def test_forced_blocks_clean_content():
    _fresh()
    cfg = store.load_config(CWD)
    p = store.project_dir(CWD) / "f.md"
    # no marker in the text, but forced (the --human-only path) still refuses
    assert store.append_doc(p, "totally ordinary content", cfg, forced=True) == {"stored": False, "reason": "human_only"}
    assert not p.exists()
    # the same content WITHOUT forced lands normally
    assert store.append_doc(p, "totally ordinary content", cfg) is None
    assert p.exists()


def test_public_helpers():
    _fresh()
    cfg = store.load_config(CWD)
    assert store.is_human_only_text("a [[human-only]] b", cfg) is True
    assert store.is_human_only_text("ordinary", cfg) is False
    assert store.is_human_only_record({"id": "i", "asst": "x [[human-only]] y"}, cfg) is True
    assert store.is_human_only_record({"id": "i", "asst": "clean"}, cfg) is False
    assert store.is_human_only_text("clean", cfg, forced=True) is True


def test_enrich_source_guard_drops_slipped_chunk():
    _fresh()
    md = store.mirror_dir(CWD)
    md.mkdir(parents=True, exist_ok=True)
    cid = "slipped-1"
    # a human_only chunk that entered the mirror BEFORE the fence existed: write it RAW,
    # bypassing the gateway (which would block it now).
    (md / "2026-07-03.9.jsonl").write_text(
        json.dumps({"id": cid, "ts": "2026-07-03T00:00:00Z",
                    "user": "forensic parse [[human-only]]", "asst": ""}) + "\n",
        encoding="utf-8")
    written = enrich.apply_enrichments(CWD, {cid: {"topic": "t", "summary": "s"}})
    assert written == 0                                          # guard dropped it, nothing enriched
    assert cid not in _dir_text(store.enrich_dir(CWD))           # no overlay entry anywhere


def test_enrich_still_enriches_clean_chunk():
    _fresh()
    md = store.mirror_dir(CWD)
    md.mkdir(parents=True, exist_ok=True)
    cid = "clean-1"
    (md / "2026-07-03.8.jsonl").write_text(
        json.dumps({"id": cid, "ts": "2026-07-03T00:00:00Z",
                    "user": "an ordinary working note about the benchmark", "asst": "ok"}) + "\n",
        encoding="utf-8")
    written = enrich.apply_enrichments(CWD, {cid: {"topic": "bench", "summary": "notes"}})
    assert written == 1                                          # clean source enriches as normal
    assert cid in _dir_text(store.enrich_dir(CWD))


def test_configured_term_blocks_without_any_classifier():
    # the PUBLIC promise: a configured `human_only_terms` entry blocks on its own (the open-code
    # substring floor), not only the [[human-only]] sentinel. Nothing outside this tree is consulted.
    _fresh()
    cfg = dict(store.DEFAULT_CONFIG)
    cfg["human_only_terms"] = ["Nimbus", "codeword"]
    assert store.is_human_only_text("discussing the Nimbus rollout", cfg) is True
    assert store.is_human_only_text("nothing sensitive here", cfg) is False
    assert store.is_human_only_record({"id": "i", "asst": "the CODEWORD is set"}, cfg) is True  # case-insensitive
    # the gateway itself refuses a term-matching record -> zero disk artifacts
    shard = store.mirror_dir(CWD) / "2026-07-03.7.jsonl"
    r = store.append_record(shard, {"id": "t1", "user": "Nimbus plan", "ts": "t"}, cfg)
    assert r == {"stored": False, "reason": "human_only"} and not shard.exists()


def test_raw_write_raises_on_persistent_failure():
    # a believed-preserved working set that never landed is the worst failure this tool can have:
    # _raw_write must RAISE on a persistent replace failure, never return silently.
    _fresh()
    p = store.project_dir(CWD) / "wfail.md"
    # atomic_write publishes with os.replace; patch THAT (the descriptor-based helper no longer goes
    # through pathlib.Path.replace). Every attempt raises, so the bounded retry exhausts and re-raises.
    orig = os.replace

    def _boom(src, dst):
        raise OSError("simulated persistent replace failure")

    os.replace = _boom
    try:
        raised = False
        try:
            store._raw_write(p, "content that must not silently vanish")
        except OSError:
            raised = True
        assert raised, "_raw_write must raise, not return silently, on a persistent failure"
        assert not p.exists(), "nothing half-written is published"
    finally:
        os.replace = orig
    leftovers = [f for f in os.listdir(store.project_dir(CWD)) if f.endswith(".tmp")]
    assert not leftovers, "a failed write leaves no temp file behind: %r" % leftovers


def _run():
    fns = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for name, fn in fns:
        fn()
        print(f"  ok  {name}")
    print(f"[test_human_only_fence] {len(fns)}/{len(fns)} passed")


if __name__ == "__main__":
    _run()
