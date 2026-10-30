#!/usr/bin/env python3
"""No code is loaded from the environment.

REVIEW-2026-10.md, pile one, class (b): store.py 1710-1728 at e480bbd read the
PARSICA_HUMAN_ONLY_CLASSIFIER variable and imported whatever module it named into every write
path, swallowing the import failure. That path is gone. These tests prove three things:

  1. BLOCKED: a module named by PARSICA_HUMAN_ONLY_CLASSIFIER is never imported, even when it is
     importable and the fence is evaluated (a canary module records its own import in a marker
     file). This test fails at e480bbd.
  2. ALLOWED and BLOCKED, with the variable set: the sentinel still refuses, a configured term
     still refuses, and clean text still passes - the variable neither adds nor removes policy.
  3. STRUCTURAL: no production script under scripts/ imports importlib, calls import_module or
     __import__, or reads PARSICA_HUMAN_ONLY_CLASSIFIER. Discovered, not hand-listed, with a
     scanner self-test.

Runs under pytest and standalone (python test_no_environment_loaded_code.py).
"""
import ast
import os
import sys
import tempfile

# set BEFORE the store import so a first import can never bind STORE_ROOT to the real HOME store.
os.environ.setdefault("PARSICA_WEAVE_ROOT", tempfile.mkdtemp(prefix="noenv_boot_"))
SCRIPTS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPTS)
import store  # noqa: E402

CANARY_NAME = "parsica_env_canary_module"
VARIABLE = "PARSICA_HUMAN_ONLY_CLASSIFIER"


def _plant_canary():
    """A real, importable module that records its own import in a marker file. If anything imports
    it, the marker appears. It sits on sys.path, so a lookup by name WOULD succeed."""
    d = tempfile.mkdtemp(prefix="noenv_canary_")
    marker = os.path.join(d, "imported.marker")
    with open(os.path.join(d, CANARY_NAME + ".py"), "w", encoding="utf-8") as fh:
        fh.write(
            "import os\n"
            "open(%r, 'w').close()\n"
            "def is_human_only(text, terms=None, forced=False):\n"
            "    return True, 'canary'\n"
            "def is_human_only_record(record, terms=None, forced=False):\n"
            "    return True, 'canary'\n" % marker)
    if d not in sys.path:
        sys.path.insert(0, d)
    return marker


def _with_env(name, value, fn):
    old = os.environ.get(name)
    os.environ[name] = value
    try:
        return fn()
    finally:
        if old is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = old


def test_named_module_is_never_imported():
    marker = _plant_canary()
    sys.modules.pop(CANARY_NAME, None)
    cfg = dict(store.DEFAULT_CONFIG)
    cfg["human_only_terms"] = ["Nimbus"]

    def _evaluate():
        store.is_human_only_text("an ordinary note", cfg)
        store.is_human_only_record({"id": "i", "user": "an ordinary note"}, cfg)
        store.is_human_only_text("discussing the Nimbus rollout", cfg)
        store.is_human_only_record({"id": "i", "asst": "x [[human-only]] y"}, cfg)

    _with_env(VARIABLE, CANARY_NAME, _evaluate)
    assert not os.path.exists(marker), "the module named by the environment was imported"
    assert CANARY_NAME not in sys.modules, "the module named by the environment was loaded"


def test_variable_neither_adds_nor_removes_policy():
    _plant_canary()
    cfg = dict(store.DEFAULT_CONFIG)
    cfg["human_only_terms"] = ["Nimbus"]

    def _probe():
        return (store.is_human_only_text("a [[human-only]] b", cfg),            # sentinel: blocked
                store.is_human_only_text("discussing the Nimbus rollout", cfg),  # term: blocked
                store.is_human_only_record({"id": "i", "asst": "the NIMBUS plan"}, cfg),
                store.is_human_only_text("nothing sensitive here", cfg),         # clean: allowed
                store.is_human_only_record({"id": "i", "user": "clean"}, cfg))

    with_var = _with_env(VARIABLE, CANARY_NAME, _probe)
    without = _probe()
    assert with_var == without == (True, True, True, False, False)


def _production_scripts():
    return sorted(os.path.join(SCRIPTS, fn) for fn in os.listdir(SCRIPTS)
                  if fn.endswith(".py") and not fn.startswith("test_"))


def _dynamic_import_sites(src):
    tree = ast.parse(src)
    hits = []
    for n in ast.walk(tree):
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in n.names]
            if isinstance(n, ast.ImportFrom) and n.module:
                names.append(n.module)
            if any(nm and nm.split(".")[0] == "importlib" for nm in names):
                hits.append("%d:import importlib" % n.lineno)
        elif isinstance(n, ast.Call):
            f = n.func
            nm = f.attr if isinstance(f, ast.Attribute) else (f.id if isinstance(f, ast.Name) else "")
            if nm in ("import_module", "__import__"):
                hits.append("%d:%s" % (n.lineno, nm))
    return hits


def test_no_dynamic_import_in_production_scripts():
    paths = _production_scripts()
    assert len(paths) >= 8, "script discovery broke: %r" % paths
    for p in paths:
        src = open(p, encoding="utf-8").read()
        hits = _dynamic_import_sites(src)
        assert not hits, "%s loads code dynamically: %r" % (os.path.basename(p), hits)
        assert VARIABLE not in src, "%s reads the removed variable" % os.path.basename(p)


def test_scanner_has_teeth():
    assert _dynamic_import_sites("import importlib\nm = importlib.import_module(x)\n")
    assert _dynamic_import_sites("from importlib import import_module\n")
    assert _dynamic_import_sites("m = __import__(name)\n")
    assert not _dynamic_import_sites("import json\nx = json.loads(s)\n")


def _run():
    fns = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for name, fn in fns:
        fn()
        print("  ok  %s" % name)
    print("[test_no_environment_loaded_code] %d/%d passed" % (len(fns), len(fns)))


if __name__ == "__main__":
    _run()
