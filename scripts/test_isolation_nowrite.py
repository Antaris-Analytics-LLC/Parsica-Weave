#!/usr/bin/env python3
"""End-to-end isolation smoke test: the REAL CLIs, run as subprocesses under PARSICA_ISOLATED=1
against a FRESH store root, must create ZERO files on disk.

This is Pro's required proof that "isolated read/use paths create no files": not a unit test of a
single guard, but the whole standalone surface exercised the way a user launches it - a subprocess
per CLI, each inheriting PARSICA_ISOLATED=1 and a throwaway PARSICA_WEAVE_ROOT. After EVERY CLI we
walk the root and assert the file count is still 0 (a mid-run leak is pinned to the CLI that caused
it, not just to the end state).

CLIs covered (each its own process, the literal command a launch profile would run):
  * pcx.py Stop / SessionStart / UserPromptSubmit / PreCompact / PostToolUse - the five hooks, fed a
    tiny fake transcript payload on stdin (the hook contract). Under isolation they read + inject but
    persist nothing.
  * query.py --grep x / query.py --recent 3 - pure read surfaces (federation degrades with no minted
    id; no trace under isolation).
  * calibrate.py --target t --event confirmed - refuses (exit 3), records nothing.
  * enrich.py --apply-stdin / enrich_apply.py / enrich_prep.py - the enrichment pipeline, each
    refuses before touching disk.

The refuse-CLIs exit 3 by contract; that is expected. The property under test is the no-write
guarantee, asserted regardless of exit code (exit codes are recorded for the report, not asserted).

Written RED against the pre-fix code, where a federated/booting isolated session minted
workspace.json (+ its sidecar lock) and the writers had no isolation refusal. Run: python
test_isolation_nowrite.py
"""
import json
import os
import subprocess
import sys
import tempfile

SCRIPTS = os.path.dirname(os.path.abspath(__file__))

fails = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        fails.append(msg)


def _count_files(root):
    """Every regular file anywhere under the store root - the byte-footprint the property forbids."""
    return sum(len(files) for _r, _d, files in os.walk(root))


def _child_env(root):
    """A clean environment for each CLI: isolation ON, the throwaway root, UTF-8 stdio (Windows
    defaults cp1252). PARSICA_CHANNEL is cleared so no run resolves a channel that could mint the
    registry; PARSICA_WEAVE stays default-on so the hooks actually fire (an 'off' switch would make
    the no-write result vacuous)."""
    env = dict(os.environ)
    env["PARSICA_ISOLATED"] = "1"
    env["PARSICA_WEAVE_ROOT"] = str(root)
    env["PYTHONIOENCODING"] = "utf-8"
    for k in ("PARSICA_CHANNEL",):
        env.pop(k, None)
    env.pop("PARSICA_WEAVE", None)   # ensure the global kill switch is not accidentally off
    return env


def _run(root, proj, argv, stdin_text=None):
    """Run one CLI as a subprocess in the project cwd, returning the CompletedProcess."""
    return subprocess.run(
        [sys.executable] + argv,
        cwd=proj,
        env=_child_env(root),
        input=stdin_text,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )


def run():
    root = tempfile.mkdtemp(prefix="iso_nowrite_root_")
    proj = tempfile.mkdtemp(prefix="iso_nowrite_proj_")     # a project cwd OUTSIDE the store root
    inbox = tempfile.mkdtemp(prefix="iso_nowrite_inbox_")   # a dedicated inbox OUTSIDE the store root

    # a tiny fake transcript (the Stop/PreCompact/PostToolUse hook payload points at this). One
    # user turn is enough; under isolation the flush paths short-circuit before reading it, but the
    # payload must carry a real path to be faithful to the hook contract. Lives outside the root.
    tpath = os.path.join(proj, "transcript.jsonl")
    with open(tpath, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "user", "uuid": "u-1",
                             "timestamp": "2026-01-01T00:00:00.000Z",
                             "sessionId": "isosess1",
                             "message": {"role": "user", "content": "an isolated turn"}}) + "\n")

    # the base hook payload (a hook reads only the keys it needs; extra keys are harmless).
    payload = {
        "cwd": proj,
        "session_id": "isosess1",
        "transcript_path": tpath,
    }
    grep_payload = dict(payload, tool_name="Grep", tool_input={"pattern": "authwidget"})

    # (label, argv, stdin) - the literal commands, in launch order.
    cases = [
        ("pcx.py Stop",              ["pcx.py", "Stop"],              json.dumps(payload)),
        ("pcx.py SessionStart",      ["pcx.py", "SessionStart"],      json.dumps(payload)),
        ("pcx.py UserPromptSubmit",  ["pcx.py", "UserPromptSubmit"],  json.dumps(payload)),
        ("pcx.py PreCompact",        ["pcx.py", "PreCompact"],        json.dumps(payload)),
        ("pcx.py PostToolUse",       ["pcx.py", "PostToolUse"],       json.dumps(grep_payload)),
        ("query.py --grep x",        ["query.py", "--grep", "x"],     None),
        ("query.py --recent 3",      ["query.py", "--recent", "3"],   None),
        ("calibrate.py record",      ["calibrate.py", "--cwd", proj,
                                      "--target", "t", "--event", "confirmed"], None),
        ("enrich.py --apply-stdin",  ["enrich.py", "--apply-stdin", "--cwd", proj],
                                     '{"c1": {"summary": "x"}}'),
        ("enrich_apply.py",          ["enrich_apply.py", "--cwd", proj, "--inbox", inbox], None),
        ("enrich_prep.py",           ["enrich_prep.py", "--cwd", proj, "--inbox", inbox], None),
    ]

    argv_map = {label: argv for label, argv, _ in cases}
    results = []   # (label, returncode, files_after)
    for label, argv, stdin_text in cases:
        argv = [os.path.join(SCRIPTS, argv[0])] + argv[1:]
        try:
            cp = _run(root, proj, argv, stdin_text)
            rc = cp.returncode
        except subprocess.TimeoutExpired:
            rc = "TIMEOUT"
        after = _count_files(root)
        results.append((label, rc, after))
        check(after == 0,
              "NO-WRITE: %s left %d file(s) under the root (exit %s)" % (label, after, rc))

    total = _count_files(root)
    check(total == 0, "NO-WRITE: the store root holds ZERO files after every isolated CLI (got %d)"
          % total)
    # the dedicated enrich inbox stays untouched too: the pipeline refuses BEFORE staging anything.
    inbox_after = _count_files(inbox)
    check(inbox_after == 0,
          "NO-WRITE: enrich_prep/apply staged nothing to the inbox under isolation (got %d)"
          % inbox_after)

    print("\nfiles-after-count per CLI (root=%s):" % root)
    for label, rc, after in results:
        print("  %-24s exit=%-7s files_after=%d" % (label, rc, after))

    print("\n" + ("ALL ISOLATION NO-WRITE CHECKS PASSED (green)" if not fails
                  else "RED: %d failing -> %s" % (len(fails), " | ".join(fails))))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(run())
