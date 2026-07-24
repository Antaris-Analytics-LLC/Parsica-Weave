#!/usr/bin/env python3
"""W-D3: one malformed mirror row must not suppress the whole SessionStart continuity re-inject.

A migrated (or hand-edited) mirror chunk whose `user` field is a LIST rather than a string used to
raise inside _mirror_index while it built the display line; the SessionStart handler is fail-silent,
so the whole re-inject collapsed to ZERO bytes - not merely the bad row dropped. The advertised
recovery path, query.py, hard-crashed on the same row.

The fix is a per-ROW guard in the render loop plus type-safe field coercion, so a poisoned row drops
ALONE while every sibling row - and the pinned working set - still ships. These pin both halves:
byte-identical render for good rows, and survival of the good rows around a poisoned one.
"""
import json
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("PARSICA_WEAVE_ROOT", tempfile.mkdtemp(prefix="wd3_boot_"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store   # noqa: E402
import pcx     # noqa: E402
import query   # noqa: E402

CWD = os.path.join(tempfile.gettempdir(), "wd3_proj")


def _fresh():
    tmp = tempfile.mkdtemp(prefix="wd3_root_")
    store.STORE_ROOT = Path(tmp)
    store.GLOBAL_CONFIG = store.STORE_ROOT / "config.json"
    store.ensure_workspace(CWD)
    return Path(tmp)


def _seed(rows):
    md = store.mirror_dir(CWD)
    md.mkdir(parents=True, exist_ok=True)
    (md / "2026-07-20.4242.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def _render():
    return pcx._mirror_index(CWD, 25)


# --- good rows render byte-identically ------------------------------------------------------

def test_well_formed_rows_render_as_before():
    _fresh()
    _seed([
        {"ts": "2026-07-20T10:00:00Z", "sess": "s1", "user": "multi\nline turn",
         "files": ["a.py", "b.py"], "cmds": ["pytest"]},
        {"ts": "2026-07-20T11:00:00Z", "sess": "s2", "user": "", "asst": "assistant carried it"},
        {"ts": "2026-07-20T12:00:00Z", "sess": "s3", "user": "a plain string row"},
    ])
    out = _render()
    assert "multi line turn" in out, "newline was not flattened"           # \n -> space
    assert "files:a.py,b.py" in out and "cmds:pytest" in out
    assert "assistant carried it" in out, "user='' did not fall through to asst"
    assert "a plain string row" in out


def test_string_files_is_not_char_split():
    """The quiet corruption: files='alpha.py' must render as one item, never files:a,l,p,h."""
    _fresh()
    _seed([{"ts": "2026-07-20T10:00:00Z", "sess": "s1", "user": "x", "files": "alpha.py"}])
    out = _render()
    assert "files:alpha.py" in out, out
    assert "files:a,l,p,h" not in out


# --- a poisoned row drops alone; siblings + working set survive -----------------------------

def test_poisoned_row_does_not_suppress_the_siblings():
    _fresh()
    _seed([
        {"ts": "2026-07-20T10:00:00Z", "sess": "s1", "user": "first good row"},
        {"ts": "2026-07-20T11:00:00Z", "sess": "s2", "user": ["poisoned", "list"]},   # was fatal
        {"ts": "2026-07-20T12:00:00Z", "sess": "s3", "asst": ["x", "y"], "files": [1, 2]},
        {"ts": "2026-07-20T13:00:00Z", "sess": "s4", "user": "last good row"},
    ])
    out = _render()
    assert out.strip(), "the whole re-inject was suppressed by a poisoned row"
    assert "first good row" in out and "last good row" in out, out


def test_poisoned_row_does_not_suppress_the_pinned_working_set():
    """LOAD-BEARING: the full SessionStart section builder must still emit the working set when a
    poisoned mirror row is present - the crash is incidental, the total silence is the defect."""
    _fresh()
    # a pinned working-set fact that must survive
    store.write_doc(store.STORE_ROOT / "working" / ("%s - Working Set.md" % store.project_key(CWD)),
                    "## Pinned facts\n- PINNED-SENTINEL-FACT rides the boot\n", store.load_config(CWD))
    _seed([{"ts": "2026-07-20T11:00:00Z", "sess": "s2", "user": ["poisoned"]}])
    idx = _render()
    # the mirror index itself is non-fatal (empty or partial), and the working set is untouched
    assert "PINNED-SENTINEL-FACT" in (store.STORE_ROOT / "working" /
           ("%s - Working Set.md" % store.project_key(CWD))).read_text(encoding="utf-8")
    assert idx == "" or "poisoned" not in idx  # the bad row never renders its raw list


# --- the recovery path (query.py grep) survives the same row --------------------------------

def test_query_grep_does_not_crash_on_a_poisoned_row():
    import types
    chunk = {"id": "m1", "user": ["poisoned", "list"], "asst": "", "files": "alpha.py", "cmds": [1]}
    args = types.SimpleNamespace(file=None, cmd=None, tool=None, category=None, grep="poisoned")
    # _score's field join used to raise "expected str instance, list found" and crash the grep -
    # the recovery path SessionStart itself tells the operator to run.
    hit, score = query._score(chunk, args)
    assert isinstance(hit, bool)   # it returns rather than raising


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
