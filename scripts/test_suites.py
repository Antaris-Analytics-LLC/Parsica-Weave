#!/usr/bin/env python3
"""Pytest bridge: makes the script suites discoverable by `pytest scripts/`.

Each suite runs in its OWN process (they set env/state at import, so subprocess
isolation is the correct harness), and a nonzero exit fails the pytest run -
CI can never look green while testing nothing.
"""
import os
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
# DISCOVERED, never hand-listed: a suite added later runs without editing this file.
SUITES = sorted(fn for fn in os.listdir(HERE)
                if fn.startswith("test_") and fn.endswith(".py") and fn != "test_suites.py")


def test_suites_discovered():
    assert len(SUITES) >= 6, "suite discovery broke: %r" % (SUITES,)


@pytest.mark.parametrize("script", SUITES)
def test_suite(script):
    p = subprocess.run([sys.executable, os.path.join(HERE, script)],
                       capture_output=True, text=True, timeout=600)
    assert p.returncode == 0, (p.stdout or "") + (p.stderr or "")
