#!/usr/bin/env python3
"""Enforcer tests for the `pcx.py promote` verb (the Weave side of the router).

  * LAW 1 (purity): the pure-Weave install must be untouched - pcx.py's module top and
    hook paths import nothing new; `promote` on a Spirit-less interpreter exits non-zero
    with a clear message and writes NOTHING.
  * E2E (the boundary HOLDS): with Spirit present, real Weave writes (note verbs, real
    templates) promote through the real planner; dry-run writes nothing; promote is
    idempotent; a planted secret + refusal term NEVER reach the durable floor.

Run: python test_router_verb.py
"""
import importlib.util
import os, sys, json, subprocess, tempfile

SCRIPTS = os.path.dirname(os.path.abspath(__file__))

# Where to find a SOURCE checkout of Parsica Spirit, for the with-Spirit scenarios.
# Portable, never a machine path: point PARSICA_SPIRIT_PKGS at the src dir of a source
# checkout (Spirit is one package; os.pathsep-joined if you ever need several roots);
# leave it unset if Spirit is pip-installed (subprocesses inherit it) or absent (the
# with-Spirit scenarios then SKIP). Example:
#   PARSICA_SPIRIT_PKGS="/path/parsica-spirit/src"
_PKGS = os.environ.get("PARSICA_SPIRIT_PKGS", "")
SPIRIT_PATH = os.pathsep.join(p for p in _PKGS.split(os.pathsep) if p.strip())


def _spirit_available():
    """True when the with-Spirit scenarios can run: an explicit source checkout via
    PARSICA_SPIRIT_PKGS, or a pip-installed parsica_spirit on the current path."""
    return bool(SPIRIT_PATH) or importlib.util.find_spec("parsica_spirit") is not None


def _spirit_workspace_aware():
    """True when the installed Spirit speaks the WORKSPACE contract: collect_candidates
    grew `source_session` AND `workspace_root` (the same features pcx.promote detects) -
    the build that collects the workspace-level docs when told where they live. A
    pre-workspace Spirit degrades pcx gracefully but cannot see lessons/journal at the
    workspace level, so the with-Spirit scenarios SKIP against it rather than fail -
    the Spirit-side build owns that half."""
    if not _spirit_available():
        return False
    try:
        import inspect
        if SPIRIT_PATH:
            sys.path[:0] = SPIRIT_PATH.split(os.pathsep)
        from parsica_spirit.promote import collect_candidates
        params = inspect.signature(collect_candidates).parameters
        return "source_session" in params and "workspace_root" in params
    except Exception:
        return False


fails = []
def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        fails.append(msg)


def _shim_absent_dir():
    """A dir whose parsica_spirit SHADOWS any site-installed one and raises
    ImportError - deterministic Spirit-absence even on a machine where the package
    is pip-installed (this dev box is)."""
    d = tempfile.mkdtemp(prefix="rv_shim_")
    with open(os.path.join(d, "parsica_spirit.py"), "w", encoding="utf-8") as fh:
        fh.write("raise ImportError('simulated: parsica_spirit not installed')\n")
    return d


def _env(weave_root, spirit_root, with_spirit):
    env = dict(os.environ)
    env["PARSICA_WEAVE_ROOT"] = weave_root
    env["PARSICA_SPIRIT_ROOT"] = spirit_root
    if with_spirit:
        # explicit source checkout wins; otherwise inherit the current path so a
        # pip-installed parsica_spirit stays visible to the subprocess
        env["PYTHONPATH"] = os.pathsep.join(
            p for p in (SPIRIT_PATH, env.get("PYTHONPATH", "")) if p)
    else:
        env["PYTHONPATH"] = _shim_absent_dir()
    return env


def _run_script(script, args, env, stdin_text=None, cwd=None):
    # encoding pinned: text=True alone uses the locale codec (cp1252 on Windows) and
    # would mangle piped UTF-8 (an em dash became U+FFFD and shifted a content hash).
    # timeout: a hung child (a wedged lock, a deadlock) reports SHARPLY as a failing
    # check instead of masquerading as a stuck suite.
    try:
        return subprocess.run([sys.executable, os.path.join(SCRIPTS, script)] + args,
                              env=env, input=stdin_text, capture_output=True, text=True,
                              encoding="utf-8", cwd=cwd, timeout=90)
    except subprocess.TimeoutExpired as e:
        import types
        return types.SimpleNamespace(returncode=124, stdout=(e.stdout or ""),
                                     stderr="TIMEOUT: %s did not exit within 90s" % script)


def _run(args, env, stdin_text=None):
    return _run_script("pcx.py", args, env, stdin_text)


SECRET = "sk-ant-AAAAAAAAAAAAAAAAAAAAAAAA"
TERM = "ZZQCROWNJEWELZZ"


def note_is_loud():
    """Regression: `note` is the agent's EXPLICIT continuity write, not a
    passive hook - a failed write must exit nonzero with a stderr message, never report
    silent success over a dropped lesson. (The four hook handlers stay fail-silent.)"""
    parent = tempfile.mkdtemp(prefix="rv_badroot_")
    bad_root = os.path.join(parent, "flatfile")
    with open(bad_root, "w", encoding="utf-8") as fh:
        fh.write("a FILE where the store root must be a directory")
    env = dict(os.environ)
    env["PARSICA_WEAVE_ROOT"] = bad_root
    env.pop("PYTHONPATH", None)
    r = _run(["note", "--kind", "lesson", "--cwd", tempfile.mkdtemp()], env, "## x\nbody\n")
    check(r.returncode != 0, "NOTE: a failed write exits nonzero (got %d)" % r.returncode)
    check("FAILED" in r.stderr, "NOTE: the failure lands on stderr (%r)" % r.stderr[:120])
    # negative control: a good root still succeeds quietly
    env["PARSICA_WEAVE_ROOT"] = tempfile.mkdtemp(prefix="rv_goodroot_")
    r2 = _run(["note", "--kind", "lesson", "--cwd", tempfile.mkdtemp()], env, "## x\nbody\n")
    check(r2.returncode == 0 and '"ok": true' in r2.stdout,
          "NOTE control: a good root still succeeds (%r)" % r2.stdout[:80])


def spirit_absent():
    weave = tempfile.mkdtemp(prefix="rv_weave_")
    spirit = tempfile.mkdtemp(prefix="rv_spirit_")
    cwd = tempfile.mkdtemp(prefix="rv_proj_")
    env = _env(weave, spirit, with_spirit=False)
    r = _run(["promote", "--cwd", cwd], env)
    check(r.returncode == 2, "ABSENT: promote exits 2 on the pure-Weave install (got %d)" % r.returncode)
    check("not installed" in r.stdout, "ABSENT: the message says Spirit is not installed")
    check(not os.listdir(spirit), "ABSENT: nothing was written to the Spirit root")
    # module purity: importing pcx must not import parsica_spirit at module top
    probe = ("import sys; sys.path.insert(0, %r); import pcx; "
             "sys.exit(1 if any(m.startswith('parsica_spirit') for m in sys.modules) else 0)"
             % SCRIPTS)
    r2 = subprocess.run([sys.executable, "-c", probe], env=env, capture_output=True, text=True)
    check(r2.returncode == 0, "LAW1: importing pcx pulls in no parsica_spirit module")


def spirit_present():
    weave = tempfile.mkdtemp(prefix="rv_weave_")
    spirit = tempfile.mkdtemp(prefix="rv_spirit_")
    cwd = tempfile.mkdtemp(prefix="rv_proj_")
    env = _env(weave, spirit, with_spirit=True)

    # real Weave writes through the real verbs (template + notes + a refusal config)
    sys.path.insert(0, SCRIPTS)
    import store as _st
    prev = os.environ.get("PARSICA_WEAVE_ROOT")
    os.environ["PARSICA_WEAVE_ROOT"] = weave
    import importlib
    importlib.reload(_st)
    pdir = _st.ensure_workspace(cwd)
    json.dump({"refusal": [TERM]}, open(pdir / "config.json", "w"))
    if prev is not None:
        os.environ["PARSICA_WEAVE_ROOT"] = prev
    else:
        os.environ.pop("PARSICA_WEAVE_ROOT", None)

    _run(["note", "--kind", "lesson", "--cwd", cwd], env,
         "## 2026-07-02 - the gateway lesson\nkey %s and term %s never persist.\n" % (SECRET, TERM))
    _run(["note", "--kind", "journal", "--cwd", cwd], env,
         "## Build day\nRouter v1 landed; promote is explicit.\n")
    _run(["note", "--kind", "working", "--cwd", cwd], env,
         "# Working set\n\n## Pinned facts\n- promote stays out of every hook\n\n## Open loops\n- never promote me\n")

    r = _run(["promote", "--cwd", cwd, "--dry-run"], env)
    check(r.returncode == 0, "DRY: dry-run exits 0 (stderr: %s)" % r.stderr[-200:])
    check("store: 3" in r.stdout, "DRY: plan sees lesson+journal+pinned (got: %r)" % r.stdout)
    check("No writes performed." in r.stdout, "DRY: says no writes performed")
    check(not os.listdir(spirit), "DRY: Spirit root untouched by dry-run")

    r = _run(["promote", "--cwd", cwd], env)
    check(r.returncode == 0, "APPLY: promote exits 0 (stderr: %s)" % r.stderr[-200:])
    check('"store": 3' in r.stdout, "APPLY: three records stored")

    blob = ""
    for root, _dirs, files in os.walk(spirit):
        for fn in files:
            try:
                blob += open(os.path.join(root, fn), encoding="utf-8", errors="replace").read()
            except OSError:
                pass
    check("promote stays out of every hook" in blob, "APPLY: the pinned fact reached the floor")
    check("never promote me" not in blob, "APPLY: open loops never promote")
    check(SECRET not in blob, "SCRUB: the secret never reaches the durable floor")
    check(TERM not in blob, "REFUSE: the refusal term never reaches the durable floor")

    r = _run(["promote", "--cwd", cwd], env)
    check('"store": 0' in r.stdout and '"skip": 3' in r.stdout,
          "IDEMPOTENT: re-promote stores nothing, skips 3 (got: %r)" % r.stdout)

    r = _run(["promote", "--cwd", cwd, "--verify"], env)
    check(r.returncode == 0 and "verify: clean" in r.stdout, "VERIFY: ledger reconciles clean")

    # post-hoc refusal: a term fenced AFTER capture must still never
    # reach the durable floor - collection applies the CURRENT list
    TERM2 = "QQPOSTHOCFENCEQQ"
    _run(["note", "--kind", "lesson", "--cwd", cwd], env,
         "## 2026-07-02 - the late fence\nthe %s subsystem name predates the fence.\n" % TERM2)
    json.dump({"refusal": [TERM, TERM2]}, open(pdir / "config.json", "w"))
    r = _run(["promote", "--cwd", cwd], env)
    check(r.returncode == 0 and '"store": 1' in r.stdout, "POSTHOC: the new lesson promoted")
    blob = ""
    for root, _dirs, files in os.walk(spirit):
        for fn in files:
            try:
                blob += open(os.path.join(root, fn), encoding="utf-8", errors="replace").read()
            except OSError:
                pass
    check(TERM2 not in blob, "POSTHOC: a term fenced after capture never reaches the floor")
    check("<redacted>" in blob, "POSTHOC: the fence left its marker, not silence")


def federation():
    weave = tempfile.mkdtemp(prefix="rf_weave_")
    spirit = tempfile.mkdtemp(prefix="rf_spirit_")
    cwd = tempfile.mkdtemp(prefix="rf_proj_")
    env = _env(weave, spirit, with_spirit=True)

    # a REAL mirror chunk through the Stop hook, then a REAL decisions overlay
    tpath = os.path.join(cwd, "transcript.jsonl")
    recs = [
        {"type": "user", "uuid": "u1", "timestamp": "2026-07-02T00:00:00Z",
         "sessionId": "sessfed1",
         "message": {"role": "user", "content": "how does the retry backoff work"}},
        {"type": "assistant", "uuid": "a1", "timestamp": "2026-07-02T00:00:05Z",
         "sessionId": "sessfed1",
         "message": {"role": "assistant", "content": "the retry backoff doubles the delay each attempt"}},
    ]
    with open(tpath, "w", encoding="utf-8") as fh:
        fh.write("\n".join(json.dumps(r) for r in recs) + "\n")
    _run(["Stop"], env, json.dumps({"cwd": cwd, "transcript_path": tpath,
                                    "session_id": "sessfed1"}))
    cid = None
    for root, _d, files in os.walk(os.path.join(weave)):
        for fn in files:
            # workspace layout: active shards live at mirror/<project-key>/<date>.<pid>.jsonl
            if os.path.basename(os.path.dirname(root)) == "mirror" and fn.endswith(".jsonl"):
                for ln in open(os.path.join(root, fn), encoding="utf-8"):
                    if ln.strip():
                        cid = json.loads(ln)["id"]
    check(bool(cid), "FED setup: the Stop hook captured a chunk (cid=%r)" % cid)
    overlay = {cid: {"summary": "retry backoff decision", "topic": ["router"],
                     "keywords": ["backoff"], "search_queries": ["retry backoff behavior"],
                     "facts": ["the retry backoff doubles the delay each attempt"],
                     "decisions": [{"text": "double the delay on each retry",
                                    "conviction": {"value": 0.8}}]}}
    r = _run_script("enrich.py", ["--cwd", cwd, "--apply-stdin"], env, json.dumps(overlay))
    check("enriched 1" in (r.stdout + r.stderr), "FED setup: overlay applied (%r)" % r.stdout)
    _run(["note", "--kind", "lesson", "--cwd", cwd], env,
         "## 2026-07-02 - the gateway lesson\nfail closed on missing scope, always.\n")
    r = _run(["promote", "--cwd", cwd], env)
    check(r.returncode == 0 and '"store": 2' in r.stdout,
          "FED setup: lesson + enriched turn promoted (%r)" % r.stdout)

    # (a) durable-only hit: no chunk matches, the Spirit lesson surfaces after the
    #     weave section (default order = weave first)
    r = _run_script("query.py", ["--grep", "gateway"], env, cwd=cwd)
    check("[durable] lesson" in r.stdout and "fail closed on missing scope" in r.stdout,
          "FED: a promoted lesson surfaces as [durable] (%r)" % r.stdout)
    check(r.stdout.index("(no matching chunks)") < r.stdout.index("[durable]"),
          "FED: default order is weave-first")

    # (b) [both]: the chunk AND its promoted enrichment match - warm copy wears the
    #     tag, the durable section does not repeat it
    r = _run_script("query.py", ["--grep", "backoff"], env, cwd=cwd)
    check("[both]" in r.stdout, "BOTH: the warm copy wears the [both] tag (%r)" % r.stdout)
    check("[durable] enriched_turn" not in r.stdout,
          "BOTH: the durable copy is not repeated below")

    # (c) long-horizon nudge: durable section leads
    r = _run_script("query.py", ["--grep", "what do you remember about the gateway"],
                    env, cwd=cwd)
    check("[durable]" in r.stdout and
          r.stdout.index("[durable]") < r.stdout.index("(no matching chunks)"),
          "NUDGE: long-horizon queries lead with the durable floor (%r)" % r.stdout)

    # (d) degraded-mode equivalence: shim-absent == kill-switch == store-missing,
    #     byte-identical, no router markers
    outs = []
    env_shim = _env(weave, spirit, with_spirit=False)
    outs.append(_run_script("query.py", ["--grep", "backoff"], env_shim, cwd=cwd).stdout)
    env_off = dict(env); env_off["PARSICA_ROUTER"] = "off"
    outs.append(_run_script("query.py", ["--grep", "backoff"], env_off, cwd=cwd).stdout)
    env_nostore = dict(env); env_nostore["PARSICA_SPIRIT_ROOT"] = tempfile.mkdtemp(prefix="rf_empty_")
    outs.append(_run_script("query.py", ["--grep", "backoff"], env_nostore, cwd=cwd).stdout)
    check(outs[0] == outs[1] == outs[2],
          "DEGRADE: absent / kill-switch / no-store outputs are byte-identical")
    check("[durable]" not in outs[0] and "[both]" not in outs[0],
          "DEGRADE: no router markers in pure-Weave output")

    # (e) the passive sensor: traces exist for promote + federated reads, hash-only,
    #     no raw text; degraded runs above emitted NOTHING new
    tdir, blob = None, ""
    for root, dirs, files in os.walk(spirit):
        if root.endswith("trace"):
            tdir = root
            for fn in files:
                blob += open(os.path.join(root, fn), encoding="utf-8").read()
    check(bool(tdir) and '"route_mode": "promote"' in blob,
          "TRACE: promotion emitted a trace event")
    check('"route_mode": "federated"' in blob, "TRACE: federated reads emitted traces")
    for raw in ("backoff", "gateway", "retry backoff", "fail closed on missing scope"):
        check(raw not in blob, "TRACE hygiene: raw text %r never lands" % raw)
    check('"deduped": 1' in blob, "TRACE: the [both] dedup was counted")
    # pin the content_hash VALUE end-to-end (promote -> federate -> disk). The lesson
    # body Spirit hashes is heading + entry INCLUDING the trailing provenance stamp
    # (workspace law #2: every shared-doc append is stamped) - rebuild it from the
    # workspace lessons.md, the independent source of truth for what the note landed.
    import hashlib as _hl
    lessons_text = open(os.path.join(weave, "lessons.md"), encoding="utf-8").read()
    stamp = [ln for ln in lessons_text.splitlines() if ln.startswith("*(")][-1]
    expected = _hl.sha256(("2026-07-02 - the gateway lesson\n\n"
                           "fail closed on missing scope, always.\n"
                           + stamp).encode("utf-8")).hexdigest()[:16]
    rows = [json.loads(ln) for ln in blob.splitlines() if ln.strip()]
    durable = [r2 for r2 in rows if r2.get("kind") == "exposure"
               and r2.get("layer") == "durable" and r2.get("source_kind") == "lesson"]
    check(any(r2.get("content_hash") == expected for r2 in durable),
          "TRACE: the durable exposure carries the ORIGINAL body hash end-to-end")
    n_before = blob.count('"kind": "trace"')
    _run_script("query.py", ["--grep", "backoff"], env_shim, cwd=cwd)
    _run_script("query.py", ["--grep", "backoff"], env_off, cwd=cwd)
    blob2 = ""
    for root, _d, files in os.walk(spirit):
        if root.endswith("trace"):
            for fn in files:
                blob2 += open(os.path.join(root, fn), encoding="utf-8").read()
    check(blob2.count('"kind": "trace"') == n_before,
          "TRACE: degraded modes emit no trace at all")

    # (f) Spirit present but BROKEN: a corrupted store degrades to Weave-only on
    #     stdout with the note on stderr - the failure never propagates
    sroot2 = tempfile.mkdtemp(prefix="rf_corrupt_")
    env_bad = dict(env); env_bad["PARSICA_SPIRIT_ROOT"] = sroot2
    sys.path.insert(0, SCRIPTS)
    import importlib, store as _st2
    prev2 = os.environ.get("PARSICA_WEAVE_ROOT")
    os.environ["PARSICA_WEAVE_ROOT"] = weave
    importlib.reload(_st2)
    os.makedirs(os.path.join(sroot2, _st2.workspace_id(), "records.jsonl"))
    if prev2 is not None:
        os.environ["PARSICA_WEAVE_ROOT"] = prev2
    else:
        os.environ.pop("PARSICA_WEAVE_ROOT", None)
    r = _run_script("query.py", ["--grep", "backoff"], env_bad, cwd=cwd)
    check(r.returncode == 0 and "[durable]" not in r.stdout and "[both]" not in r.stdout,
          "BROKEN: a corrupted Spirit store degrades to Weave-only stdout")
    check("unavailable" in r.stderr, "BROKEN: the degrade note goes to stderr, not stdout")
    return weave, spirit, cwd, env


def recency_federation(weave, spirit, cwd, env):
    """query.py --recent with the durable layer (Spirit federated_recent): recency is
    a retrieval mode over BOTH layers under ONE budget (the K@ rule: durable slots
    come OUT of N, never on top of it), own-session exclusion matches the
    source_session label promote writes, and every degraded mode keeps the warm-only
    surface byte-identical. Reuses federation()'s store: its warm chunks are dated
    2026-07-02 (stale, outside any sane window) while its two promoted records
    entered the pool JUST NOW (store time, the pool's clock) - so the durable layer
    carries the recency surface alone."""
    r = _run_script("query.py", ["--recent", "5"], env, cwd=cwd)
    check("durable (Spirit)" in r.stdout and r.stdout.count("[durable]") == 2,
          "RECENT: freshly promoted records surface in the durable recency section (%r)" % r.stdout)
    import pcx as _pcx, store as _store
    _cid, _sess = _pcx._safe_chunk_id("u1", ""), _store.sess_token("sessfed1")   # ids/sess are opaque now
    check(("enriched:" + _cid) in r.stdout and "lesson:" in r.stdout,
          "RECENT: both pool records inside the window are shown")
    check(_sess in r.stdout, "RECENT: the durable hit renders its session tag")
    # BUDGET (the K@ dogfood assertion): N is ONE budget shared across layers - two
    # in-window durable records may fill a --recent 1 slot, never inflate past it.
    r = _run_script("query.py", ["--recent", "1"], env, cwd=cwd)
    check(r.stdout.count("[durable]") == 1,
          "RECENT budget: durable slots come OUT of N, never on top (1 slot -> 1 row)")
    # own-session exclusion: the enriched turn testified source_session=sessfed1 (its
    # mirror chunk); the lesson is unattributed (stamp sess '-') and NEVER excluded.
    r = _run_script("query.py", ["--recent", "5", "--sess", "sessfed1"], env, cwd=cwd)
    check("enriched:u1" not in r.stdout,
          "RECENT: --sess excludes the caller's own session on the durable layer")
    check("lesson:" in r.stdout, "RECENT: an unattributed record is never excluded")
    # degraded modes: kill-switch / shim-absent stay byte-identical, warm-only
    env_off = dict(env); env_off["PARSICA_ROUTER"] = "off"
    out_off = _run_script("query.py", ["--recent", "5"], env_off, cwd=cwd).stdout
    env_shim = _env(weave, spirit, with_spirit=False)
    out_shim = _run_script("query.py", ["--recent", "5"], env_shim, cwd=cwd).stdout
    check(out_off == out_shim, "RECENT degrade: kill-switch / absent outputs are byte-identical")
    check("durable" not in out_off, "RECENT degrade: no durable section in pure-Weave output")


def fenced_project_name():
    """the review's leak repro, turned into a regression: a refusal-fenced project CODENAME in
    the working directory's BASENAME must never reach Parsica Spirit. The scope id that
    promotion carries into the durable floor is derived by hashing the project key, so
    the raw name cannot appear in records, identity, the sync ledger, traces, or the
    command's own stdout/stderr - even though it sits in the Weave dir path."""
    weave = tempfile.mkdtemp(prefix="fp_weave_")
    spirit = tempfile.mkdtemp(prefix="fp_spirit_")
    parent = tempfile.mkdtemp(prefix="fp_parent_")
    CODENAME = "ZZSECRETCODENAMEZZ"
    cwd = os.path.join(parent, CODENAME)          # the DIR BASENAME is the fenced term
    os.makedirs(cwd)
    env = _env(weave, spirit, with_spirit=True)

    sys.path.insert(0, SCRIPTS)
    import importlib, store as _st3
    prev = os.environ.get("PARSICA_WEAVE_ROOT")
    os.environ["PARSICA_WEAVE_ROOT"] = weave
    importlib.reload(_st3)
    pdir = _st3.ensure_workspace(cwd)
    json.dump({"refusal": [CODENAME]}, open(pdir / "config.json", "w"))
    if prev is not None:
        os.environ["PARSICA_WEAVE_ROOT"] = prev
    else:
        os.environ.pop("PARSICA_WEAVE_ROOT", None)

    # a CLEAN lesson (no codename in the body), then a real promote + a federated query
    _run(["note", "--kind", "lesson", "--cwd", cwd], env,
         "## 2026-07-02 - a clean scar\nnothing sensitive lives in this body.\n")
    r = _run(["promote", "--cwd", cwd], env)
    q = _run_script("query.py", ["--grep", "clean"], env, cwd=cwd)

    blob = r.stdout + r.stderr + q.stdout + q.stderr
    for root, _d, files in os.walk(spirit):       # every byte Spirit persisted
        for fn in files:
            try:
                blob += open(os.path.join(root, fn), encoding="utf-8", errors="replace").read()
            except OSError:
                pass
    check(CODENAME not in blob,
          "FENCE: a fenced codename in the dir name reaches NOTHING Spirit-side "
          "(labels / identity / sync ledger / traces / stdout / stderr)")
    check(r.returncode == 0 and '"store": 1' in r.stdout,
          "FENCE: the clean lesson still promoted normally (%r)" % r.stdout[-120:])


if __name__ == "__main__":
    # spirit_absent needs Spirit ABSENT (the shim guarantees it), so it always runs and
    # proves routing purity even on a standalone Weave install. The with-Spirit scenarios
    # need Parsica Spirit present; when it is not, they SKIP cleanly (exit 0) rather than
    # fail, so the standalone Weave test suite stays green without the optional dependency.
    note_is_loud()
    spirit_absent()
    if _spirit_workspace_aware():
        spirit_present()
        recency_federation(*federation())
        fenced_project_name()
    elif _spirit_available():
        print("SKIP with-Spirit scenarios: the installed Parsica Spirit predates the "
              "workspace contract (collect_candidates has no source_session) - the "
              "a workspace-aware Parsica Spirit release provides that half")
    else:
        print("SKIP with-Spirit scenarios: Parsica Spirit not found "
              "(set PARSICA_SPIRIT_PKGS to a source checkout, or pip install parsica-spirit)")
    print("\n" + ("ALL ROUTER-VERB TESTS PASSED (green)"
                  if not fails else "RED: %d failing -> %s" % (len(fails), " | ".join(fails))))
    sys.exit(1 if fails else 0)
