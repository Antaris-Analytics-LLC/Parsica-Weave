#!/usr/bin/env python3
"""RC5L-E family 1: one config sanitizer, no raw path from JSON to an effective config.

Two confirmed regressions, both a config value reaching an EFFECT without the type gate that
config_value_ok/valid_policy_terms were supposed to guarantee everywhere:

  * _legacy_cfg (pcx.py) copied a silo's config.json values RAW, so a legacy `{"scrub_pii": 0}`
    governed a whole migration's re-scrub - every migrated lesson, journal page, working set and
    mirror record written with the PII fence OFF - while import_metadata_json correctly quarantined
    the very same document. PII to disk, the finalized bar.
  * load_config's refusal/human_only_terms UNION loops iterated `src.get(k) or []` directly, so a
    SCALAR string `{"refusal": "a"}` iterated character by character into ['a'], and the fence then
    redacted the letter 'a' everywhere it appeared in stored prose - silent corruption, note
    reporting success. The identical shape lived in _legacy_cfg's own policy loop.

normalize_config is now the ONE path from raw JSON to effective config, shared by load_config,
_legacy_cfg, and (already) import_metadata_json. These pin both the failure and its safe twin:
a real `scrub_pii: false` opt-out and a real multi-term refusal list must still work.
"""
import io
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("PARSICA_WEAVE_ROOT", tempfile.mkdtemp(prefix="rc5le1_boot_"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store   # noqa: E402
import pcx     # noqa: E402

EMAIL = "alice@example.com"          # reserved-range PII vectors (RFC 2606 / NANP), never a live value
PHONE = "202-555-0147"               # 555-01xx is the fictional NANP exchange; full area+exchange+line


def _fresh():
    tmp = tempfile.mkdtemp(prefix="rc5le1_root_")
    store.STORE_ROOT = Path(tmp)
    store.GLOBAL_CONFIG = store.STORE_ROOT / "config.json"
    return Path(tmp)


def _set_global(cfg_dict):
    store.GLOBAL_CONFIG.write_text(json.dumps(cfg_dict), encoding="utf-8")


def _silo(root, key, *, working, config=None):
    s = root / key
    (s / "mirror").mkdir(parents=True, exist_ok=True)
    (s / "working.md").write_text(working, encoding="utf-8")
    if config is not None:
        (s / "config.json").write_text(json.dumps(config), encoding="utf-8")
    return s


def _seed_mirror(silo, records):
    (silo / "mirror" / "2026-07-20.4242.jsonl").write_text(
        "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


def _migrate():
    old = sys.stdout
    sys.stdout = io.StringIO()
    try:
        pcx.handle_migrate(["migrate"])
        return sys.stdout.getvalue()
    finally:
        sys.stdout = old


def _note(kind, cwd, text):
    old_in, old_out = sys.stdin, sys.stdout
    sys.stdin = io.TextIOWrapper(io.BytesIO(text.encode("utf-8")))
    sys.stdout = io.StringIO()
    try:
        pcx.handle_note(["note", "--kind", kind, "--cwd", str(cwd)])
        return sys.stdout.getvalue()
    finally:
        sys.stdin, sys.stdout = old_in, old_out


def _active_working_text(key):
    hits = list((store.STORE_ROOT / "working").glob("%s*.md" % key))
    return hits[0].read_text(encoding="utf-8") if hits else ""


def _active_mirror_text(key):
    md = store.STORE_ROOT / "mirror" / key
    return "".join(p.read_text(encoding="utf-8") for p in md.rglob("*.jsonl"))


# --------------------------------------------------------------------------
# normalize_config: the sanitizer in isolation
# --------------------------------------------------------------------------

def test_scalar_policy_value_is_rejected_not_char_split():
    _fresh()
    _values, policy, dropped = store.normalize_config({"refusal": "a"})
    assert policy["refusal"] == [], "a scalar refusal must yield NO terms, never ['a']"
    assert "refusal" in dropped

def test_wrong_typed_scalar_knob_is_dropped():
    _fresh()
    values, _policy, dropped = store.normalize_config({"scrub_pii": 0})
    assert "scrub_pii" not in values, "integer 0 must not satisfy the boolean scrub_pii knob"
    assert "scrub_pii" in dropped

def test_real_bool_opt_out_passes():
    _fresh()
    values, _policy, _dropped = store.normalize_config({"scrub_pii": False})
    assert values["scrub_pii"] is False, "a genuine JSON false is a supported opt-out"

def test_valid_refusal_list_passes_verbatim():
    _fresh()
    _values, policy, _dropped = store.normalize_config({"refusal": ["ProjectHydra", "codename-x"]})
    assert policy["refusal"] == ["ProjectHydra", "codename-x"]

def test_non_dict_is_empty_not_iterated():
    _fresh()
    assert store.normalize_config("garbage") == ({}, {}, {})
    assert store.normalize_config(None) == ({}, {}, {})


# --------------------------------------------------------------------------
# load_config: the read path (Sol regression #2 root)
# --------------------------------------------------------------------------

def test_load_config_scalar_refusal_is_not_char_split():
    _fresh()
    _set_global({"refusal": "a"})
    cfg = store.load_config(None)
    assert cfg["refusal"] == [], "scalar refusal char-split into single-letter terms: %r" % cfg["refusal"]

def test_load_config_scalar_human_only_is_not_char_split():
    _fresh()
    _set_global({"human_only_terms": "abc"})
    cfg = store.load_config(None)
    assert cfg["human_only_terms"] == []

def test_load_config_valid_refusal_list_survives():
    _fresh()
    _set_global({"refusal": ["ProjectHydra"]})
    assert store.load_config(None)["refusal"] == ["ProjectHydra"]


# --------------------------------------------------------------------------
# _legacy_cfg: the migration doc-merge effect (Sol regression #1 root)
# --------------------------------------------------------------------------

# _legacy_cfg is now a PURE function over an already-parsed, already-validated config object - the
# reading moved to store.preflight_silo_metadata (the true first read, W-F1). These unit-pin the
# effect it computes; the end-to-end read+refuse path is covered by test_metadata_preflight.

def test_legacy_cfg_type_gates_scrub_pii_zero():
    assert pcx._legacy_cfg({"scrub_pii": 0})["scrub_pii"] is True, \
        "a silo {'scrub_pii': 0} disabled the migration re-scrub"

def test_legacy_cfg_scalar_refusal_is_not_char_split():
    cfg = pcx._legacy_cfg({"refusal": "a"})
    assert "a" not in cfg["refusal"], "scalar silo refusal char-split: %r" % cfg["refusal"]

def test_legacy_cfg_real_opt_out_is_honored():
    assert pcx._legacy_cfg({"scrub_pii": False})["scrub_pii"] is False

def test_legacy_cfg_none_snapshot_is_global_only():
    """A silo with no config (or an absent-on-rerun snapshot) yields the global policy, no crash."""
    assert pcx._legacy_cfg(None)["scrub_pii"] is True


# --------------------------------------------------------------------------
# Command level: Sol's two repros, end to end
# --------------------------------------------------------------------------

def test_migrate_with_scrub_pii_zero_still_scrubs_working_and_mirror():
    root = _fresh()
    key = "silo-0123456789"
    silo = _silo(root, key,
                 working="## Pinned\n- reach %s or %s for staging\n" % (EMAIL, PHONE),
                 config={"scrub_pii": 0})
    _seed_mirror(silo, [{"ts": "2026-07-20T10:00:00Z", "sess": "s1",
                         "user": "contact %s about the incident" % EMAIL}])
    out = _migrate()

    work = _active_working_text(key)
    mirror = _active_mirror_text(key)
    assert work, "no active working set was written"
    assert EMAIL not in work and PHONE not in work, "PII survived migration under scrub_pii=0: %r" % work
    assert EMAIL not in mirror, "PII survived into the migrated mirror under scrub_pii=0"
    # receipt consistency: the config that carried the bad knob is quarantined, NOT reported active
    assert "config.json" in out and "quarantin" in out.lower()

def test_migrate_with_real_scrub_pii_false_opt_out_is_honored():
    """Both-directions twin: a genuine bool opt-out must still pass through migration."""
    root = _fresh()
    key = "silo-0123456789"
    _silo(root, key,
          working="## Pinned\n- reach %s for staging\n" % EMAIL,
          config={"scrub_pii": False})
    _migrate()
    work = _active_working_text(key)
    assert EMAIL in work, "a real scrub_pii=false opt-out was ignored - the email should remain"

def test_note_with_scalar_refusal_does_not_char_redact():
    root = _fresh()
    _set_global({"refusal": "a"})
    cwd = str(root / "proj")
    os.makedirs(cwd, exist_ok=True)
    (Path(cwd) / "README.md").write_text("p\n", encoding="utf-8")
    _note("working", cwd, "## Pinned\n- banana bandana carbonara\n")
    work = store.working_path(cwd).read_text(encoding="utf-8")
    assert "banana bandana carbonara" in work, "scalar refusal char-redacted the letter 'a': %r" % work

def test_note_with_valid_refusal_still_redacts():
    """Both-directions twin: a real multi-char refusal term must still fence."""
    root = _fresh()
    _set_global({"refusal": ["bandana"]})
    cwd = str(root / "proj")
    os.makedirs(cwd, exist_ok=True)
    (Path(cwd) / "README.md").write_text("p\n", encoding="utf-8")
    _note("working", cwd, "## Pinned\n- banana bandana carbonara\n")
    work = store.working_path(cwd).read_text(encoding="utf-8")
    assert "bandana" not in work, "a valid refusal term failed to redact: %r" % work
    assert "banana" in work and "carbonara" in work, "the term over-matched its neighbors"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
