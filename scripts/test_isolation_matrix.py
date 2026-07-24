#!/usr/bin/env python3
"""Regression: EVERY public CLI, run under PARSICA_ISOLATED=1, writes ZERO durable files.

This is the property behind the "zero footprint" promise, tested at the EXECUTABLE surface
(subprocess) because that is where the contract lives. A function-level test misses a verb that
resolves cleanly in-process but writes through a seam the handler forgot to guard - which is
exactly how `channel --name` slipped past: it minted channels.json under isolation while every
in-process isolation test stayed green. After each invocation an initially-empty
PARSICA_WEAVE_ROOT must still contain no files. A teeth test proves a green means something.
"""
import os
import sys
import json
import shutil
import tempfile
import subprocess

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(SCRIPTS)

_HOOK = json.dumps({"session_id": "iso", "transcript_path": "/nonexistent", "cwd": ROOT})

# (label, argv-after-python, stdin) for every public CLI entry point + hook event.
_CASES = [
    ("Stop",             ["pcx.py", "Stop"], _HOOK),
    ("UserPromptSubmit", ["pcx.py", "UserPromptSubmit"], "{}"),
    ("PreCompact",       ["pcx.py", "PreCompact"], _HOOK),
    ("SessionStart",     ["pcx.py", "SessionStart"], "{}"),
    ("PostToolUse",      ["pcx.py", "PostToolUse"], "{}"),
    ("note",             ["pcx.py", "note", "--kind", "journal", "--cwd", ROOT, "--sess", "iso"], "## n\nhi"),
    ("promote",          ["pcx.py", "promote", "--cwd", ROOT], ""),
    ("migrate",          ["pcx.py", "migrate"], ""),
    ("channel --name",   ["pcx.py", "channel", "--name", "lane"], ""),
    ("channel --rename", ["pcx.py", "channel", "--rename", "a", "b"], ""),
    ("channel --list",   ["pcx.py", "channel", "--list"], ""),
    ("sync",             ["pcx.py", "sync", "--hours", "24"], ""),
    ("query",            ["query.py", "--grep", "x"], ""),
    ("calibrate",        ["calibrate.py", "--stats", "--cwd", ROOT], ""),
    ("enrich",           ["enrich.py", "--list-pending", "--cwd", ROOT], ""),
    ("enrich_prep",      ["enrich_prep.py", "--cwd", ROOT], ""),
]


def _run(argv, stdin, root, isolated):
    env = dict(os.environ, PARSICA_WEAVE_ROOT=root, PARSICA_CHANNEL="",
               PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    if isolated:
        env["PARSICA_ISOLATED"] = "1"
    else:
        env.pop("PARSICA_ISOLATED", None)
    subprocess.run([sys.executable, os.path.join(SCRIPTS, argv[0])] + argv[1:],
                   input=stdin, capture_output=True, text=True, env=env, timeout=60)


def _files(root):
    return [os.path.relpath(os.path.join(dp, f), root)
            for dp, _, fs in os.walk(root) for f in fs]


def test_public_clis_write_nothing_under_isolation():
    leaks = []
    for label, argv, stdin in _CASES:
        root = tempfile.mkdtemp(prefix="iso_matrix_")
        try:
            _run(argv, stdin, root, isolated=True)
            left = _files(root)
            if left:
                leaks.append("%s wrote %r" % (label, left))
        finally:
            shutil.rmtree(root, ignore_errors=True)
    assert not leaks, "PARSICA_ISOLATED=1 is not zero-footprint:\n  " + "\n  ".join(leaks)


def test_matrix_has_teeth():
    # Without isolation, channel --name DOES write - so a green matrix above is not vacuous.
    root = tempfile.mkdtemp(prefix="iso_teeth_")
    try:
        _run(["pcx.py", "channel", "--name", "lane"], "", root, isolated=False)
        assert _files(root), "teeth: channel --name must write channels.json WITHOUT isolation"
    finally:
        shutil.rmtree(root, ignore_errors=True)
