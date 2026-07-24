#!/usr/bin/env python3
"""W-D1: the working.md migration verdict must reach the completion marker.

_migrate_working's result was discarded, so a REFUSED or half-completed working.md still let
`.migrated-to-workspace` be written. Because the marker short-circuits every later run, the silo's
ONLY working-set source became permanently unreachable through the command. Separately, the moves
inside _migrate_working could raise WinError 32 (source held open) straight out to main() and abort
the ENTIRE run - every remaining silo included - on a raw traceback.

The verdict is three-way, and the twins pin all three:
  * human-only fence  -> policy decision, source left unindexed, silo IS complete, marker WRITTEN
  * containment/read  -> failure, NO marker, silo still discovered on the next run
  * move-after-write  -> failure reported (never a traceback), NO marker, and the NEXT silo migrates
"""
import io
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("PARSICA_WEAVE_ROOT", tempfile.mkdtemp(prefix="wd1_boot_"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store   # noqa: E402
import pcx     # noqa: E402


def _fresh():
    tmp = tempfile.mkdtemp(prefix="wd1_root_")
    store.STORE_ROOT = Path(tmp)
    store.GLOBAL_CONFIG = store.STORE_ROOT / "config.json"
    # pcx caches STORE_ROOT-derived paths lazily through store, so re-pointing store is enough.
    return Path(tmp)


def _silo(root, key, *, working="## Pinned facts\n- the only copy of the working set\n",
          config=None):
    s = root / key
    s.mkdir(parents=True, exist_ok=True)
    (s / "working.md").write_text(working, encoding="utf-8")
    if config is not None:
        (s / "config.json").write_text(json.dumps(config), encoding="utf-8")
    return s


def _migrate():
    old = sys.stdout
    sys.stdout = io.StringIO()
    try:
        pcx.handle_migrate(["migrate"])
        return sys.stdout.getvalue()
    finally:
        sys.stdout = old


def _marker_present(silo):
    return (silo / store.MIGRATED_MARKER).exists()


# --- write this one FIRST: the human-only fence is a completed policy decision --------------

def test_human_only_working_md_still_marks_the_silo():
    root = _fresh()
    silo = _silo(root, "fenced-abcdef0123",
                 working="## Pinned facts\n- about FORBIDDENTOPIC internals\n",
                 config={"human_only_terms": ["FORBIDDENTOPIC"]})
    out = _migrate()
    assert "human-only fence" in out, out
    assert (silo / "working.md").exists(), "the fenced source must be left in place"
    assert _marker_present(silo), "a human-only fence is a policy decision - the silo IS migrated"
    assert "done: 1 migrated" in out
    # and it does not get re-migrated on a second run
    assert "0 migrated, 1 skipped" in _migrate()


# --- containment / read refusal: no marker, still re-runnable -------------------------------

def test_refused_working_md_writes_no_marker_and_stays_rerunnable(monkeypatch):
    root = _fresh()
    silo = _silo(root, "refused-0123456789")

    real = store.assert_contained_file

    def _refuse_working(path, base):
        if os.path.basename(str(path)) == "working.md":
            raise store.MigrationRefused("working.md is a link out of the silo")
        return real(path, base)

    monkeypatch.setattr(store, "assert_contained_file", _refuse_working)
    out = _migrate()
    assert "REFUSED" in out and "working.md" in out, out
    assert not _marker_present(silo), "a refused working.md must not stamp the silo complete"
    assert "NO MARKER" in out
    assert (silo / "working.md").exists(), "source must be preserved for the re-run"

    # cause cleared -> the silo is STILL discovered and now migrates (the advertised recovery)
    monkeypatch.setattr(store, "assert_contained_file", real)
    out2 = _migrate()
    assert "1 migrated" in out2, "the silo was stranded - not rediscovered after the cause cleared"
    assert _marker_present(silo)


# --- move-after-write failure: no traceback, no marker, NEXT silo still migrates ------------

def test_move_failure_does_not_abort_the_run_or_strand_later_silos(monkeypatch):
    root = _fresh()
    # two silos; the FIRST one's working.md consume-rename fails mid-run.
    silo_a = _silo(root, "aaa-0123456789")
    silo_b = _silo(root, "zzz-0123456789")

    real_move = store.migrate_move

    def _fail_on_a_working(src, dst, *a, **k):
        if "aaa-0123456789" in str(src) and os.path.basename(str(src)) == "working.md":
            raise OSError(32, "The process cannot access the file")
        return real_move(src, dst, *a, **k)

    monkeypatch.setattr(store, "migrate_move", _fail_on_a_working)
    out = _migrate()   # must NOT raise

    assert "Traceback" not in out
    assert not _marker_present(silo_a), "content written but source unconsumed - must not mark done"
    assert _marker_present(silo_b), "a failure in silo aaa aborted the run before silo zzz"
    assert "1 migrated" in out and "1 skipped" in out, out


# --- baseline twin: a clean working.md still migrates and marks -----------------------------

def test_clean_working_md_migrates_and_marks():
    root = _fresh()
    silo = _silo(root, "clean-0123456789")
    out = _migrate()
    assert "merged" in out and "working.md" in out
    assert _marker_present(silo)
    assert (store.STORE_ROOT / "working").is_dir(), "no working set was activated"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
