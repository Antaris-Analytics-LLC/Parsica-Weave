#!/usr/bin/env python3
"""W-D2: a malformed scalar under a known config key must never disable a privacy knob.

`{"scrub_pii": 0}` (or null / "" / [] / 0.0) reaching cfg["scrub_pii"] turned the PII fence OFF by
truthiness at the read site, so an ordinary note - and every passive Stop capture - wrote raw
email/phone to the store while `migrate` printed "(validated)". The gate is a per-key TYPE-IDENTITY
check, not a falsiness rule, so:

  - a real JSON bool `false` is still honored (scrub_pii=false is a supported opt-out),
  - an integer 0 is still honored for the cadence/retention knobs where 0 means "off",
  - a wrong-typed scalar is dropped and the safe DEFAULT stands (scrub stays on).

These drive the real read choke (load_config) and the real scrub, plus the import-time quarantine.
"""
import json
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("PARSICA_WEAVE_ROOT", tempfile.mkdtemp(prefix="cfg_gate_boot_"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store   # noqa: E402
import scrub   # noqa: E402

CWD = os.path.join(tempfile.gettempdir(), "cfg_gate_proj")
_EMAIL = "first.last@example.com"
_PHONE = "+1-212-555-0173"       # 555-01xx: reserved fictional range


def _fresh():
    tmp = tempfile.mkdtemp(prefix="cfg_gate_root_")
    store.STORE_ROOT = Path(tmp)
    store.GLOBAL_CONFIG = store.STORE_ROOT / "config.json"
    store.ensure_workspace(CWD)
    return tmp


def _write_project_config(obj):
    p = store.project_dir(CWD) / "config.json"
    p.write_text(json.dumps(obj), encoding="utf-8")


def _pii_survives(cfg):
    """True if an email rides through the scrub verbatim under this cfg - i.e. the fence is OFF."""
    out, _ = scrub.scrub("reach me at %s or %s" % (_EMAIL, _PHONE),
                         scrub_pii=cfg["scrub_pii"])
    return _EMAIL in out


# --- config_value_ok unit table -------------------------------------------------------------

def test_bool_knob_takes_bool_only_and_int_zero_does_not_qualify():
    # scrub_pii default is a bool. The exact scrub-disabling bug: int 0 must NOT satisfy it.
    assert store.config_value_ok("scrub_pii", False) is True    # a real opt-out is honored
    assert store.config_value_ok("scrub_pii", True) is True
    for bad in (0, 1, 0.0, "", "false", [], {}, None):
        assert store.config_value_ok("scrub_pii", bad) is False, "int/str/null 0 disabled the fence: %r" % (bad,)


def test_int_knob_keeps_zero_but_rejects_bool():
    # heartbeat_every_n=0 is a DOCUMENTED 'off'. 0 must survive; a bool must not (bool is an int).
    assert store.config_value_ok("heartbeat_every_n", 0) is True
    assert store.config_value_ok("mirror_active_days", 30) is True
    assert store.config_value_ok("checkpoint_every_n", True) is False    # bool never satisfies an int knob
    assert store.config_value_ok("mirror_active_days", "14") is False


def test_float_knob_accepts_int_or_float_not_bool():
    assert store.config_value_ok("pairing_threshold", 2) is True         # int for a float knob is fine
    assert store.config_value_ok("pairing_threshold", 1.5) is True
    assert store.config_value_ok("pairing_threshold", True) is False


def test_unknown_key_is_never_ok():
    assert store.config_value_ok("not_a_real_key", "x") is False


# --- the security property, at the real read choke ------------------------------------------

def test_malformed_scrub_pii_falls_safe_to_default_on():
    """THE finding. Every falsy non-bool shape for scrub_pii must leave the fence ON."""
    for bad in (0, 0.0, "", [], None):
        _fresh()
        _write_project_config({"scrub_pii": bad})
        cfg = store.load_config(CWD)
        assert cfg["scrub_pii"] is True, "malformed %r disabled scrub_pii" % (bad,)
        assert not _pii_survives(cfg), "PII rode through the scrub with %r on disk" % (bad,)


def test_real_false_opt_out_is_honored():
    """LEGIT twin: an explicit JSON bool false is a supported opt-out and must still disable scrub."""
    _fresh()
    _write_project_config({"scrub_pii": False})
    cfg = store.load_config(CWD)
    assert cfg["scrub_pii"] is False
    assert _pii_survives(cfg), "a legitimate scrub_pii=false was overridden back to on"


def test_truthy_string_still_scrubs():
    """THIRD LEG: strings like "false"/"0" are wrong-typed, so the fence stays ON (they do not
    become a sneaky opt-out via truthiness)."""
    for s in ("false", "0", "no"):
        _fresh()
        _write_project_config({"scrub_pii": s})
        cfg = store.load_config(CWD)
        assert cfg["scrub_pii"] is True and not _pii_survives(cfg), "string %r changed the fence" % s


def test_int_zero_off_knob_still_takes_effect():
    """SECOND AXIS: 0 is a real value for cadence knobs and must survive the gate."""
    _fresh()
    _write_project_config({"heartbeat_every_n": 0})
    assert store.load_config(CWD)["heartbeat_every_n"] == 0


# --- import-time honesty: a malformed config is quarantined, not "(validated)" --------------

def test_import_metadata_quarantines_malformed_config():
    _fresh()
    notes = []
    silo = Path(tempfile.mkdtemp(prefix="cfg_gate_silo_"))
    src = silo / "config.json"
    src.write_text(json.dumps({"scrub_pii": 0, "refusal": ["SECRETWORD"]}), encoding="utf-8")
    dst = store.project_dir(CWD) / "config.json"
    if dst.exists():
        dst.unlink()
    ok = store.import_metadata_json(src, dst, "cfg_gate_proj", "config.json",
                                    store.load_config(CWD), root=silo, note=notes.append)
    assert ok is False, "a scrub-disabling config was imported as valid"
    assert not dst.exists(), "the malformed config was activated"
    # The durable audit trail names the rejected key, so an operator sees WHY it was quarantined.
    receipt = store.quarantine_dir("cfg_gate_proj") / ("config.json" + store.QUARANTINE_SUFFIX)
    assert receipt.exists(), "no quarantine receipt was written"
    body = receipt.read_text(encoding="utf-8")
    assert "wrong type" in body and "scrub_pii" in body, body


def test_import_metadata_accepts_a_well_typed_config():
    """Twin: a clean legacy config still imports, so the gate is not a scrub-everything hammer."""
    _fresh()
    silo = Path(tempfile.mkdtemp(prefix="cfg_gate_silo_ok_"))
    src = silo / "config.json"
    src.write_text(json.dumps({"scrub_pii": False, "mirror_active_days": 30}), encoding="utf-8")
    dst = store.project_dir(CWD) / "config.json"
    if dst.exists():
        dst.unlink()
    ok = store.import_metadata_json(src, dst, "cfg_gate_proj", "config.json",
                                    store.load_config(CWD), root=silo, note=lambda *_: None)
    assert ok is True and dst.exists()
    landed = json.loads(dst.read_text(encoding="utf-8"))
    assert landed["scrub_pii"] is False and landed["mirror_active_days"] == 30


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
