#!/usr/bin/env python3
"""The fan-out is gone and the stage that fed it is capped. REVIEW-2026-10.md, pile one, class (f).

At e480bbd scripts/enrich-swarm.workflow.js started one model worker per pending chunk with no
cap (83-94), hard-coded the model (6, 92), dropped a failed worker with .catch(() => null) (93) and
took its applied count from a model's relay of a command's output (99-118); the ~20-turn hook text
told the agent to spawn it. The workflow is removed. What remains are two operator-run stages whose
counts come from the process.

  1. No workflow script ships (nothing under the repository matches *.workflow.js), and no shipped
     document or production script tells the agent to spawn a swarm or call the Workflow tool.
  2. enrich_prep.py stages at most 50 chunks per run: 60 pending -> 50 staged (BLOCKED); --limit 5
     stages 5 (ALLOWED). --limit 0 does not lift the cap: the pending listing the stage reads
     (enrich.py --list-pending) returns at most 50 per run, which is what bounded the old fan-out
     at e480bbd, by an unrelated default rather than by design.
  3. The stage handshake contract lives in scripts/enrich_handshake.schema.json and is strict for
     both stages, with the safety fields required.

Runs under pytest and standalone (python test_fan_out_capped.py).
"""
import glob
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("PARSICA_WEAVE_ROOT", tempfile.mkdtemp(prefix="fanout_boot_"))
SCRIPTS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(SCRIPTS)
sys.path.insert(0, SCRIPTS)
import store  # noqa: E402

SHIPPED_DOCS = ["README.md", "ONBOARDING.md", "AUTONOMOUS_ENRICHMENT.md", "SECURITY.md",
                os.path.join("skills", "weave", "SKILL.md")]
SPAWN_PHRASES = ("workflow(", "spawn the background enrichment swarm", "fans out one cheap worker",
                 "one haiku worker per chunk", "spawn it when the directive asks")


def test_no_workflow_script_ships_and_nothing_tells_the_agent_to_spawn_one():
    found = [p for p in glob.glob(os.path.join(ROOT, "**", "*.workflow.js"), recursive=True)
             if os.sep + ".git" + os.sep not in p]
    assert not found, "a workflow script still ships: %r" % found
    paths = [os.path.join(ROOT, d) for d in SHIPPED_DOCS]
    paths += glob.glob(os.path.join(ROOT, "commands", "*.md"))
    paths += [os.path.join(SCRIPTS, fn) for fn in os.listdir(SCRIPTS)
              if fn.endswith(".py") and not fn.startswith("test_")]
    checked = 0
    for p in paths:
        if not os.path.isfile(p):
            continue
        checked += 1
        text = open(p, encoding="utf-8", errors="replace").read().lower()
        for phrase in SPAWN_PHRASES:
            assert phrase not in text, "%s still tells the agent to spawn a swarm: %r" % (
                os.path.relpath(p, ROOT), phrase)
    assert checked >= 8, "discovery broke: too few files checked (%d)" % checked


def _fresh_project():
    tmp = tempfile.mkdtemp(prefix="fanout_root_")
    store.STORE_ROOT = Path(tmp)
    store.GLOBAL_CONFIG = store.STORE_ROOT / "config.json"
    cwd = tempfile.mkdtemp(prefix="fanout_proj_")
    store.ensure_workspace(cwd)
    return cwd


def _stage(cwd, extra_argv):
    """Run the scan stage as the operator does, into a fresh private inbox; return (stdout JSON,
    staged .txt names)."""
    inbox = os.path.join(tempfile.mkdtemp(prefix="fanout_inbox_"), "in")
    env = dict(os.environ, PARSICA_WEAVE_ROOT=str(store.STORE_ROOT), PYTHONIOENCODING="utf-8")
    env.pop("PARSICA_ISOLATED", None)
    r = subprocess.run([sys.executable, os.path.join(SCRIPTS, "enrich_prep.py"), "--cwd", cwd,
                        "--inbox", inbox] + extra_argv,
                       env=env, capture_output=True, text=True, timeout=180)
    assert r.returncode == 0, (r.returncode, r.stdout[-400:], r.stderr[-400:])
    data = json.loads(r.stdout)
    staged = [f for f in os.listdir(inbox) if f.endswith(".txt")] if os.path.isdir(inbox) else []
    return data, staged


def test_prep_stages_at_most_fifty_per_run_and_a_smaller_limit_stages_fewer():
    cwd = _fresh_project()
    cfg = store.load_config(cwd)
    shard = store.mirror_dir(cwd) / "2026-07-01.1.jsonl"
    for i in range(60):
        store.append_record(shard, {
            "id": "cid-%020x" % i, "ts": "2026-07-01T00:%02d:00Z" % i, "sess": "s-" + "a" * 16,
            "user": "question number %d about the retry backoff policy and its limits" % i,
            "asst": "answer number %d: it doubles per attempt and caps at sixty seconds" % i}, cfg)
    data, staged = _stage(cwd, [])
    assert data["count"] == 50 and len(staged) == 50, (data["count"], len(staged))   # BLOCKED: the cap
    data, staged = _stage(cwd, ["--limit", "5"])
    assert data["count"] == 5 and len(staged) == 5                                    # ALLOWED: fewer
    data, staged = _stage(cwd, ["--limit", "0"])
    assert data["count"] == 50 and len(staged) == 50, "the listing's own per-run cap of 50 holds"


def test_handshake_schema_is_strict_for_both_stages():
    with open(os.path.join(SCRIPTS, "enrich_handshake.schema.json"), encoding="utf-8") as fh:
        schema = json.load(fh)
    for stage in ("scan", "apply"):
        assert schema[stage]["additionalProperties"] is False, stage
        assert set(schema[stage]["required"]) <= set(schema[stage]["properties"]), stage
    assert {"inbox_withheld", "skipped_fenced"} <= set(schema["scan"]["required"])
    assert {"applied", "malformed"} <= set(schema["apply"]["required"])
    assert "swept" in schema["apply"]["properties"]


def _run():
    fns = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for name, fn in fns:
        fn()
        print("  ok  %s" % name)
    print("[test_fan_out_capped] %d/%d passed" % (len(fns), len(fns)))


if __name__ == "__main__":
    _run()
