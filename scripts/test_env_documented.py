#!/usr/bin/env python3
"""No hidden paths. REVIEW-2026-10.md, pile one, class (d).

  1. enrich.py 607 and 631-635 at e480bbd: a hidden --apply-file argument, enabled by the
     PARSICA_ALLOW_APPLY_FILE environment variable, read any path on the command line as
     enrichment JSON with no containment check. It is gone: passing it exits with argparse's
     usage error and applies nothing, with the old variable set or not (BLOCKED); --apply-stdin
     still applies an overlay (ALLOWED).
  2. Every PARSICA_* environment variable a production script reads is named in README.md.
     Discovered by a regex over the sources under scripts/ and bin/, never hand-listed, so a
     variable added later is caught the day it appears. The two removed switches are asserted
     absent from the sources outright.

Runs under pytest and standalone (python test_env_documented.py).
"""
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("PARSICA_WEAVE_ROOT", tempfile.mkdtemp(prefix="envdoc_boot_"))
SCRIPTS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(SCRIPTS)
sys.path.insert(0, SCRIPTS)
import store  # noqa: E402

ENV_READ = re.compile(r"""os\.environ(?:\.get\(|\[)\s*["'](PARSICA_[A-Z0-9_]+)["']""")
KNOWN = {"PARSICA_WEAVE", "PARSICA_ISOLATED", "PARSICA_ROUTER", "PARSICA_WEAVE_ROOT",
         "PARSICA_SPIRIT_ROOT", "PARSICA_CHANNEL"}
REMOVED = ("PARSICA_ALLOW_APPLY_FILE", "PARSICA_HUMAN_ONLY_CLASSIFIER", "apply_file", "apply-file")


def _production_sources():
    out = [os.path.join(SCRIPTS, fn) for fn in sorted(os.listdir(SCRIPTS))
           if fn.endswith(".py") and not fn.startswith("test_")]
    bindir = os.path.join(ROOT, "bin")
    if os.path.isdir(bindir):
        out += [os.path.join(bindir, fn) for fn in sorted(os.listdir(bindir))
                if os.path.isfile(os.path.join(bindir, fn))]
    return out


def test_every_environment_variable_read_is_documented():
    readme = open(os.path.join(ROOT, "README.md"), encoding="utf-8").read()
    found = {}
    for p in _production_sources():
        src = open(p, encoding="utf-8", errors="replace").read()
        for name in ENV_READ.findall(src):
            found.setdefault(name, set()).add(os.path.basename(p))
    assert found, "discovery broke: no environment reads found under scripts/"
    assert KNOWN <= set(found), "discovery missed a known variable: %r" % sorted(KNOWN - set(found))
    missing = sorted(n for n in found if n not in readme)
    assert not missing, ("environment variables read by the scripts but not named in README.md: %r "
                         "(read in %r)" % (missing, {n: sorted(found[n]) for n in missing}))


def test_removed_switches_are_not_read_anywhere():
    for p in _production_sources():
        src = open(p, encoding="utf-8", errors="replace").read()
        for gone in REMOVED:
            assert gone not in src, "%s still carries %s" % (os.path.basename(p), gone)


def _fresh_project():
    tmp = tempfile.mkdtemp(prefix="envdoc_root_")
    store.STORE_ROOT = Path(tmp)
    store.GLOBAL_CONFIG = store.STORE_ROOT / "config.json"
    cwd = tempfile.mkdtemp(prefix="envdoc_proj_")
    store.ensure_workspace(cwd)
    return cwd


def _enrich(cwd, argv, stdin_text=None, extra_env=None):
    env = dict(os.environ, PARSICA_WEAVE_ROOT=str(store.STORE_ROOT), PYTHONIOENCODING="utf-8")
    env.pop("PARSICA_ISOLATED", None)
    env.update(extra_env or {})
    return subprocess.run([sys.executable, os.path.join(SCRIPTS, "enrich.py"), "--cwd", cwd] + argv,
                          env=env, input=stdin_text, capture_output=True, text=True, timeout=120)


def _overlay_text(cwd):
    d = store.enrich_dir(cwd)
    if not d.is_dir():
        return ""
    return "\n".join(f.read_text(encoding="utf-8") for f in d.iterdir() if f.is_file())


def test_apply_file_is_gone_even_when_the_old_variable_is_set():
    cwd = _fresh_project()
    payload = os.path.join(tempfile.mkdtemp(prefix="envdoc_payload_"), "overlay.json")
    with open(payload, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"cid-" + "a" * 20: {"topic": "t", "summary": "from a file path"}}))
    for extra in ({}, {"PARSICA_ALLOW_APPLY_FILE": "1"}):
        r = _enrich(cwd, ["--apply-file", payload], extra_env=extra)
        assert r.returncode == 2 and "unrecognized arguments" in r.stderr, (r.returncode, r.stderr[-300:])
        assert "from a file path" not in _overlay_text(cwd), "a path argument must never be applied"


def test_apply_stdin_still_applies_an_overlay():
    cwd = _fresh_project()
    cfg = store.load_config(cwd)
    cid = "cid-" + "b" * 20
    store.append_record(store.mirror_dir(cwd) / "2026-07-01.1.jsonl",
                        {"id": cid, "ts": "2026-07-01T00:00:00Z", "sess": "s-" + "c" * 16,
                         "user": "how does the retry backoff work", "asst": "it doubles per attempt"}, cfg)
    r = _enrich(cwd, ["--apply-stdin"],
                stdin_text=json.dumps({cid: {"topic": "retries", "summary": "backoff doubles per attempt"}}))
    assert r.returncode == 0 and "enriched 1 chunk" in r.stdout, (r.returncode, r.stdout, r.stderr[-300:])
    assert "backoff doubles per attempt" in _overlay_text(cwd)


def _run():
    fns = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for name, fn in fns:
        fn()
        print("  ok  %s" % name)
    print("[test_env_documented] %d/%d passed" % (len(fns), len(fns)))


if __name__ == "__main__":
    _run()
