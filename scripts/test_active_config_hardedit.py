#!/usr/bin/env python3
"""RC5L-G W2-E (Fable's named addition): a hand-edited ACTIVE config with a scalar refusal field must
behave per NORMALIZED policy on the live note and query surfaces - no char-split, no verbatim storage
of the malformed value, no durable corruption - with the valid multi-term twin still fencing.

RC5L-E routed _legacy_cfg, load_config and import_metadata_json through the one normalize_config
sanitizer, and test_config_normalizer pins the note path. But the OUTPUT fence has two more raw
callers of the union that a seat implementing only the migration doc could leave untouched:
`store.workspace_refusal_terms()` feeds the QUERY sink at query.py:522 and fence_output at
store.py, and query is where a stored note is re-emitted to a human. This drives BOTH public commands
against an operator who hand-edited their live project config.json to `{"refusal": "a"}`:

  * store time (note): the letter 'a' is not redacted out of the stored working set - a scalar policy
    is NO policy, so the note lands verbatim and nothing on disk is corrupted;
  * query time: the same note re-emerges intact, because the query union normalized the scalar to no
    terms rather than iterating it into ['a'] and redacting that letter everywhere it appears.

The valid twin - `{"refusal": ["bandana"]}` - must still fence 'bandana' at both surfaces while
leaving its neighbours 'banana' and 'carbonara' alone, so the guard is proven in both directions.
"""
import io
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("PARSICA_WEAVE_ROOT", tempfile.mkdtemp(prefix="w2e_boot_"))
_HERE = Path(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, str(_HERE))
import store   # noqa: E402
import pcx     # noqa: E402

CONTENT = "## Pinned\n- banana bandana carbonara on the launch pad\n"


def _fresh():
    tmp = Path(tempfile.mkdtemp(prefix="w2e_root_"))
    store.STORE_ROOT = tmp
    store.GLOBAL_CONFIG = tmp / "config.json"
    store._refusal_union_memo = {"sig": None, "terms": []}   # no cross-test memo bleed
    return tmp


def _cwd(tmp):
    cwd = str(tmp / "proj")
    os.makedirs(cwd, exist_ok=True)
    (Path(cwd) / "README.md").write_text("p\n", encoding="utf-8")
    return cwd


def _hand_edit_active_config(cwd, policy):
    """Write the operator's own edit into the ALREADY-ACTIVE project config.json."""
    pdir = store.project_dir(cwd)
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "config.json").write_text(json.dumps(policy), encoding="utf-8")


def _seed_mirror(cwd, text):
    """A mirror row captured BEFORE the hand-edit, written RAW past the store-time gateway - so the
    ONLY thing standing between its content and the terminal is query's output fence (_RF). This is
    the realistic case: content is already on disk when the operator edits the policy."""
    md = store.mirror_dir(cwd)
    md.mkdir(parents=True, exist_ok=True)
    (md / "2026-07-20.4242.jsonl").write_text(
        json.dumps({"id": "w2e-1", "ts": "2026-07-20T10:00:00Z", "sess": "s1",
                    "user": text, "asst": ""}) + "\n", encoding="utf-8")


def _note(cwd, text):
    old_in, old_out = sys.stdin, sys.stdout
    sys.stdin = io.TextIOWrapper(io.BytesIO(text.encode("utf-8")))
    sys.stdout = io.StringIO()
    try:
        pcx.handle_note(["note", "--kind", "working", "--cwd", str(cwd)])
    finally:
        sys.stdin, sys.stdout = old_in, old_out


def _query(tmp, *args):
    """The PUBLIC query command, as a subprocess reading STORE_ROOT from the environment - the exact
    path a user runs, and the one that hits query.py:522's `_RF = store.workspace_refusal_terms()`."""
    env = dict(os.environ, PARSICA_WEAVE_ROOT=str(tmp), PYTHONIOENCODING="utf-8")
    env.pop("PARSICA_ISOLATED", None)
    p = subprocess.run([sys.executable, str(_HERE / "query.py"), "--workspace"] + list(args),
                       capture_output=True, encoding="utf-8", errors="replace", env=env)
    return (p.stdout or "") + (p.stderr or "")


def test_scalar_refusal_in_active_config_does_not_char_redact_note_or_query(tmp_path):
    tmp = _fresh()
    cwd = _cwd(tmp)
    _hand_edit_active_config(cwd, {"refusal": "a"})     # the operator's own hand-edit, a SCALAR
    _seed_mirror(cwd, "banana bandana carbonara on the launch pad")

    _note(cwd, CONTENT)

    # 1. store time (note): no durable corruption. The working set on disk is verbatim, the letter
    #    'a' and all three words intact - a scalar policy fenced nothing, which is correct.
    work = store.working_path(cwd).read_text(encoding="utf-8")
    assert "banana bandana carbonara" in work, "a scalar refusal char-redacted the stored note: %r" % work

    # 2. no verbatim storage of the malformed value as a live term
    assert store.workspace_refusal_terms() == [], \
        "the scalar refusal was stored as a live term: %r" % store.workspace_refusal_terms()

    # 3. query time: the seeded row re-emerges intact through the public command - the output fence
    #    normalized the scalar to NO terms rather than iterating it into ['a'] and eating the letter.
    out = _query(tmp, "--grep", "carbonara")
    assert "banana" in out and "carbonara" in out, \
        "the query output fence char-redacted a scalar-refusal workspace: %r" % out


def test_valid_multi_term_active_config_still_fences_note_and_query(tmp_path):
    """The both-directions twin: a well-formed list must fence 'bandana' at store AND query time,
    without over-matching its neighbours."""
    tmp = _fresh()
    cwd = _cwd(tmp)
    _hand_edit_active_config(cwd, {"refusal": ["bandana"]})
    _seed_mirror(cwd, "banana bandana carbonara on the launch pad")

    _note(cwd, CONTENT)

    work = store.working_path(cwd).read_text(encoding="utf-8")
    assert "bandana" not in work, "a valid refusal term was not fenced in the stored note: %r" % work
    assert "banana" in work and "carbonara" in work, "the term over-matched its neighbours: %r" % work
    assert store.workspace_refusal_terms() == ["bandana"]

    # query output fence: the raw-seeded row still carries 'bandana'; the fence must eat it at DISPLAY
    # time (proving the query call site honours the normalized union) without touching its neighbours.
    out = _query(tmp, "--grep", "carbonara")
    assert "bandana" not in out, "the query output fence let a configured term through: %r" % out
    assert "carbonara" in out, "the query fence redacted an innocent neighbour: %r" % out


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
