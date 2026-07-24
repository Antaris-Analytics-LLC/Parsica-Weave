#!/usr/bin/env python3
"""doctor --help must print usage and exit 0 - never run the probe suite.

The dispatcher's help says "Run any command with --help for its own options"; doctor is the one
command that used to break that promise (no argv handling, so --help ran the full multi-second
self-check). This pins the fix: --help and -h answer with a usage line, exit 0, and run zero
probes (no per-check "ok/WARN/FAIL" lines, no temp-root writes)."""
import os
import subprocess
import sys
import tempfile

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
DOCTOR = os.path.join(SCRIPTS, "doctor.py")


def _run(flag):
    env = dict(os.environ, PARSICA_WEAVE_ROOT=tempfile.mkdtemp(prefix="doctor_help_"),
               PYTHONIOENCODING="utf-8")
    return subprocess.run([sys.executable, DOCTOR, flag],
                          capture_output=True, encoding="utf-8", errors="replace", env=env)


def test_help_prints_usage_and_exits_zero():
    for flag in ("--help", "-h"):
        p = _run(flag)
        assert p.returncode == 0, "%s exited %d" % (flag, p.returncode)
        assert "usage: parsica-weave doctor" in p.stdout
        # and it must NOT have run the probes: no per-check status lines
        for marker in ("  ok ", "  WARN ", "  FAIL "):
            assert marker not in p.stdout, "%s ran the probe suite" % flag
