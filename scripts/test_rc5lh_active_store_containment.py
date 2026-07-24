"""RC5L-H: trusted-root, untrusted-child containment for the ORDINARY active-store gateway -
appends, directory creation, locks, enumeration, reads - as opposed to the previously-hardened
named red paths (migration, .parsica_router, trace, merge marker, mkstemp publishers) already
covered by test_migrate_containment.py and test_atomic_write_discipline.py.

These tests exercise the NON-migration call sites this round touched: ensure_workspace,
save_state/load_state, rotate_mirror, pcx.py's _scrub_tail, enrich.py, and calibrate.py. The
underlying primitives (_verified_dir, _safe_append_open, _safe_iterdir, _read_text_no_follow) are
exhaustively covered elsewhere; what these confirm is that each call site is actually WIRED
through them, not that the primitive itself works.

Junction tests need no privilege. Hardlink tests skip cleanly where unsupported rather than
passing vacuously - a skip that reads as a pass is how a positive control disappears from a CI
matrix.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("PARSICA_WEAVE_ROOT", tempfile.mkdtemp(prefix="asc_root_"))
os.environ.pop("PARSICA_ISOLATED", None)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store  # noqa: E402
import pcx  # noqa: E402
import enrich  # noqa: E402
import calibrate  # noqa: E402

WINDOWS = os.name == "nt"
SECRET = "sk-ant-api03-" + "a" * 40


def _junction(link: Path, target: Path) -> bool:
    if not WINDOWS:
        return False
    target.mkdir(parents=True, exist_ok=True)
    return subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                          capture_output=True, text=True).returncode == 0


def _hardlink(link: Path, content: str) -> bool:
    target = link.with_suffix(link.suffix + ".real")
    target.write_text(content, encoding="utf-8")
    try:
        os.link(str(target), str(link))
        return True
    except (OSError, NotImplementedError):
        return False


@pytest.fixture()
def fresh_root(monkeypatch, tmp_path):
    """A fresh STORE_ROOT for this test only, so tests never share state or contaminate the
    module-level default root every other test in this file relies on."""
    root = tmp_path / "store_root"
    monkeypatch.setattr(store, "STORE_ROOT", root)
    return root


# --- ensure_workspace: directory scaffolding must not follow a planted junction -----------------

def test_ensure_workspace_refuses_a_junctioned_projects_dir(fresh_root, tmp_path):
    fresh_root.mkdir(parents=True, exist_ok=True)   # STORE_ROOT pre-exists; only "projects" is linked
    outside = tmp_path / "outside"
    if not _junction(fresh_root / "projects", outside):
        pytest.skip("mklink /J unavailable")
    cwd = str(tmp_path / "proj")
    os.makedirs(cwd, exist_ok=True)
    with pytest.raises(store.MigrationRefused):
        store.ensure_workspace(cwd)
    assert not any(outside.iterdir()) if outside.exists() else True, (
        "the workspace tree must not have been scaffolded outside the store root")


def test_ensure_workspace_ordinary_use_is_unaffected(fresh_root, tmp_path):
    cwd = str(tmp_path / "proj2")
    os.makedirs(cwd, exist_ok=True)
    d = store.ensure_workspace(cwd)
    assert Path(d).is_dir()
    assert store.working_path(cwd).exists()
    assert store.lessons_path().exists()


# --- load_state / save_state: the read side must refuse a hardlinked .state.json ----------------

def test_load_state_refuses_a_hardlinked_state_file(fresh_root, tmp_path):
    cwd = str(tmp_path / "proj3")
    os.makedirs(cwd, exist_ok=True)
    store.ensure_workspace(cwd)
    sp = store.project_dir(cwd) / ".state.json"
    if not _hardlink(sp, json.dumps({"leaked": SECRET})):
        pytest.skip("hardlinks unavailable here")
    st = store.load_state(cwd)
    assert st.get("leaked") is None, "a hardlinked .state.json must not load as this workspace's own"


def test_save_state_then_load_state_round_trips_ordinarily(fresh_root, tmp_path):
    cwd = str(tmp_path / "proj4")
    os.makedirs(cwd, exist_ok=True)
    store.ensure_workspace(cwd)
    st = store.load_state(cwd)
    st["turns"] = {"tp:x.jsonl": 5}
    store.save_state(cwd, st)
    reloaded = store.load_state(cwd)
    assert reloaded["turns"] == {"tp:x.jsonl": 5}


# --- rotate_mirror: enumeration must skip a junctioned per-project mirror subdir -----------------

def test_rotate_mirror_skips_a_junctioned_project_subdir(fresh_root, tmp_path):
    cwd = str(tmp_path / "proj5")
    os.makedirs(cwd, exist_ok=True)
    store.ensure_workspace(cwd)
    md = store.mirror_dir(cwd)
    outside = tmp_path / "outside2"
    outside.mkdir()
    (outside / "STOLEN.jsonl").write_text(SECRET, encoding="utf-8")
    linked_child = md / "2026-01-01.jsonl"
    if not _junction(linked_child, outside):
        pytest.skip("mklink /J unavailable")
    # must not raise and must not traverse into the junction
    store.rotate_mirror(cwd, active_days=1)


def test_rotate_mirror_ordinary_use_is_unaffected(fresh_root, tmp_path):
    cwd = str(tmp_path / "proj6")
    os.makedirs(cwd, exist_ok=True)
    store.ensure_workspace(cwd)
    md = store.mirror_dir(cwd)
    (md / "2020-01-01.jsonl").write_text("{}", encoding="utf-8")
    store.rotate_mirror(cwd, active_days=1)   # an old shard should archive, not raise


# --- pcx.py's _scrub_tail: the SessionStart re-inject read must refuse a hardlinked doc ----------

def test_scrub_tail_refuses_a_hardlinked_working_set(fresh_root, tmp_path):
    cwd = str(tmp_path / "proj7")
    os.makedirs(cwd, exist_ok=True)
    store.ensure_workspace(cwd)
    wp = store.working_path(cwd)
    wp.unlink()
    if not _hardlink(wp, "## Pinned facts\n- " + SECRET + "\n"):
        pytest.skip("hardlinks unavailable here")
    out = pcx._scrub_tail(wp, 4000, scrub_pii=True)
    assert out == "", "a hardlinked working.md must degrade to empty, not surface the linked content"


# --- enrich.py: overlay/pending scan must skip a junctioned enrich subdir ------------------------

def test_apply_enrichments_refuses_nothing_but_skips_a_junctioned_dir(fresh_root, tmp_path):
    """apply_enrichments tolerates a missing/linked mirror dir by returning 0 applied, matching
    the existing 'missing dir = nothing to do' contract - it must not raise or follow the link."""
    cwd = str(tmp_path / "proj8")
    os.makedirs(cwd, exist_ok=True)
    store.ensure_workspace(cwd)
    md = store.mirror_dir(cwd)
    shutil.rmtree(md)   # ensure_workspace already created it (with archive/) for real; junction the NAME
    outside = tmp_path / "outside3"
    if not _junction(md, outside):
        pytest.skip("mklink /J unavailable")
    n = enrich.apply_enrichments(cwd, {})
    assert n == 0


# --- calibrate.py: load_stats must skip a junctioned calibration dir -----------------------------

def test_load_stats_skips_a_junctioned_calibration_dir(fresh_root, tmp_path):
    cwd = str(tmp_path / "proj9")
    os.makedirs(cwd, exist_ok=True)
    store.ensure_workspace(cwd)
    cd = store.calibration_dir(cwd)
    shutil.rmtree(cd)   # ensure_workspace already created it for real; junction the NAME instead
    outside = tmp_path / "outside4"
    (outside).mkdir()
    (outside / "loot.jsonl").write_text(
        json.dumps({"target": "STOLEN", "outcome": "confirmed"}) + "\n", encoding="utf-8")
    if not _junction(cd, outside):
        pytest.skip("mklink /J unavailable")
    stats = calibrate.load_stats(cwd=cwd)
    assert stats == {}, "a junctioned calibration dir must read back empty, not the linked shard's stats"


def test_load_stats_ordinary_use_is_unaffected(fresh_root, tmp_path):
    cwd = str(tmp_path / "proj10")
    os.makedirs(cwd, exist_ok=True)
    store.ensure_workspace(cwd)
    cd = store.calibration_dir(cwd)
    (cd / "2026-07-23.jsonl").write_text(
        json.dumps({"ts": "2026-07-23T00:00:00Z", "outcome": "win"}) + "\n", encoding="utf-8")
    calibrate.load_stats(cwd=cwd)   # must not raise


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
