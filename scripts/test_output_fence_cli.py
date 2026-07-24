"""W4 regression: query.py's early error branches must route through the output fence.

The channel-not-found branch and the invalid --project branch echoed the caller's raw argument
straight to the terminal with a bare print(), bypassing the centralized output sink (_s). A caller
value carrying a recognized secret/PII shape (or a workspace refusal term) was therefore disclosed
in the terminal / terminal log even though the storage path fences everything. These drive the real
CLI as a subprocess and assert the recognized-secret argument never appears raw in the output.
"""
import os
import subprocess
import sys
import tempfile

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
QUERY = os.path.join(SCRIPTS, "query.py")
SECRET = "sk-ant-api03-" + "a" * 40          # a recognized Anthropic key shape the scrub redacts


def _run(*args):
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
           "PARSICA_WEAVE_ROOT": tempfile.mkdtemp(prefix="w4_")}
    env.pop("PARSICA_ISOLATED", None)
    r = subprocess.run([sys.executable, QUERY, *args], capture_output=True, text=True, env=env)
    return (r.stdout or "") + (r.stderr or "")


def test_channel_not_found_branch_fences_the_raw_value():
    out = _run("--channel", SECRET)
    assert "not found" in out, "expected the channel-not-found branch to fire:\n" + out
    assert SECRET not in out, "W4: channel-not-found echoed the raw credential-shaped value:\n" + out
    assert "[REDACTED" in out, "the value was neither present nor redacted (unexpected):\n" + out


def test_invalid_project_branch_fences_the_raw_value():
    # a path-traversal value (fails validate_project_key) that embeds a recognized secret
    out = _run("--project", "../" + SECRET)
    assert "invalid --project" in out, "expected the invalid-project branch to fire:\n" + out
    assert SECRET not in out, "W4: invalid-project echoed the raw credential-shaped value:\n" + out
    assert "[REDACTED" in out, "the value was neither present nor redacted (unexpected):\n" + out
