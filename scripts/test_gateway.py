#!/usr/bin/env python3
"""Enforcer tests for the single-choke-point store gateway.

  * OUTCOME (does the boundary HOLD): plant a secret + refusal terms (including a term GLUED into a
    camelCase word, a substring-vs-token match the scrubber must still catch), drive EVERY real write hook (mirror capture, note
    working/lesson/journal, PreCompact marker, enrichment, calibration), read the whole store dir
    back and assert none appear. Also asserts store's own .state.json stays metadata-only. A
    negative control keeps it from passing vacuously.
  * STRUCTURAL (is the boundary the ONLY door): AST-scan every content-producer module, discovered
    RECURSIVELY (not hand-listed), for any raw file-WRITE primitive; assert none. A SCANNER self-test
    proves the scan recognizes the realistic bypass mechanisms. A static denylist is a BACKSTOP; the
    OUTCOME test is the property-level proof.

Run: python test_gateway.py
"""
import os, sys, io, ast, json, tempfile

os.environ["PARSICA_WEAVE_ROOT"] = tempfile.mkdtemp(prefix="gw_root_")
SCRIPTS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPTS)
import store, scrub, pcx, enrich, calibrate

fails = []
def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        fails.append(msg)

SECRET = "sk-ant-AAAAAAAAAAAAAAAAAAAAAAAA"
TERM = "ZZQCROWNJEWELZZ"
GLUED = "codenamed" + TERM + "Client"     # the welded / camelCase case: term fused into an identifier


def _store_content(cwd):
    # the WHOLE workspace tree: content lands at several levels now (mirror/<key>/, the
    # workspace docs at the root, projects/<key>/enrich + calibration) - a leak anywhere
    # must be seen, so walk STORE_ROOT, not one project dir.
    out = []
    for root, _, files in os.walk(store.STORE_ROOT):
        for fn in files:
            if fn in ("config.json", ".state.json"):
                continue                     # settings/metadata, not captured content
            try:
                out.append(open(os.path.join(root, fn), encoding="utf-8", errors="replace").read())
            except Exception:
                pass
    return "\n".join(out)


def _note(cwd, kind, body):
    old = sys.stdin
    sys.stdin = type("S", (), {"buffer": io.BytesIO(body.encode("utf-8"))})()
    try:
        pcx.handle_note(["note", "--kind", kind, "--cwd", cwd])
    finally:
        sys.stdin = old


def outcome():
    cwd = tempfile.mkdtemp(prefix="gw_proj_")
    store.ensure_workspace(cwd)
    json.dump({"refusal": [TERM], "scrub_pii": True},
              open(store.project_dir(cwd) / "config.json", "w"))

    # 1) mirror via handle_stop - secret + term, spaced AND glued
    tpath = os.path.join(cwd, "transcript.jsonl")
    recs = [
        {"type": "user", "uuid": "u1", "timestamp": "2026-07-01T00:00:00Z", "sessionId": "sess1234",
         "message": {"role": "user", "content": "deploy %s and %s and key %s" % (TERM, GLUED, SECRET)}},
        {"type": "assistant", "uuid": "a1", "timestamp": "2026-07-01T00:00:01Z", "sessionId": "sess1234",
         "message": {"role": "assistant", "content": "ok %s / %s / %s" % (TERM, GLUED, SECRET)}},
    ]
    with open(tpath, "w", encoding="utf-8") as fh:
        fh.write("\n".join(json.dumps(r) for r in recs) + "\n")
    pcx.handle_stop({"cwd": cwd, "transcript_path": tpath, "session_id": "sess1234"})

    # 2) note paths: working (write_doc) + lesson + journal (append_doc)
    _note(cwd, "working", "## Pinned\n- key %s term %s glued %s\n" % (SECRET, TERM, GLUED))
    _note(cwd, "lesson",  "## scar\n- %s and %s and %s\n" % (SECRET, TERM, GLUED))
    _note(cwd, "journal", "## day\n- %s / %s / %s\n" % (SECRET, TERM, GLUED))

    # 3) PreCompact marker (append_doc)
    pcx.handle_precompact({"cwd": cwd, "transcript_path": tpath, "session_id": "sess1234"})

    # 4) enrichment (append_record) on a real captured chunk
    cid = None
    for f in store.mirror_dir(cwd).glob("*.jsonl"):
        for ln in f.read_text(encoding="utf-8").splitlines():
            if ln.strip():
                cid = json.loads(ln).get("id"); break
        if cid:
            break
    if cid:
        enrich.apply_enrichments(cwd, {cid: {"summary": "about %s %s" % (TERM, GLUED),
                                             "facts": ["key %s" % SECRET], "keywords": [TERM, GLUED]}})

    # 5) calibration source (append_record via calibrate.record)
    calibrate.record(cwd, "d0decafbad12", "confirmed", source="vs %s %s %s" % (TERM, GLUED, SECRET))

    blob = _store_content(cwd)
    check(SECRET not in blob, "OUTCOME: the secret never reaches the store")
    check(TERM not in blob, "OUTCOME: the refusal term never reaches the store (incl. glued/camelCase)")

    stp = store.project_dir(cwd) / ".state.json"
    stext = stp.read_text(encoding="utf-8") if stp.exists() else ""
    check(SECRET not in stext and TERM not in stext, "OUTCOME: .state.json stays metadata-only (no content)")

    leak = store.mirror_dir(cwd) / "_leak_control.jsonl"
    leak.write_text("leaked %s here" % TERM, encoding="utf-8")
    check(TERM in _store_content(cwd), "OUTCOME control: an un-gated raw write leaks (scan has teeth)")
    leak.unlink()


def reserved_subtree():
    """A reserved key must spare only a SCALAR id, never a whole content subtree."""
    cfg = {"scrub_pii": True, "refusal": [TERM]}
    out = store._scrub_record({"categories": ["leak %s %s" % (SECRET, TERM)],
                               "summary": "leak %s %s" % (SECRET, TERM)}, cfg)
    joined = json.dumps(out)
    check(SECRET not in joined and TERM not in joined,
          "RESERVED: content under a reserved key (categories) is still scrubbed (got %r)" % out.get("categories"))


# ---- STRUCTURAL scan + its self-test ----------------------------------------
def _write_ops_in_source(src):
    """Every raw file-WRITE primitive in a source string, widened well past the four naive spellings
    so the denylist is a real backstop: builtin/Path open in a write or UNKNOWN mode, write_text/
    write_bytes/writelines, os.open/fdopen/write/replace/rename, io.open, shutil move/copy*, a
    dump()-to-file, and print(file=...). str.replace / read-mode open are NOT flagged."""
    tree = ast.parse(src)
    hits = []
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call):
            continue
        f = n.func
        nm = f.attr if isinstance(f, ast.Attribute) else (f.id if isinstance(f, ast.Name) else "")
        base = f.value.id if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) else ""
        if nm == "open" and base not in ("os", "io"):
            idx = 0 if isinstance(f, ast.Attribute) else 1
            marg = n.args[idx] if len(n.args) > idx else None
            mode = marg.value if isinstance(marg, ast.Constant) else None
            for kw in n.keywords:
                if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
                    mode = kw.value.value
            # write mode, OR a mode arg we cannot read at all (a variable) -> flag conservatively
            if (isinstance(mode, str) and any(c in mode for c in "wax+")) or (marg is not None and mode is None):
                hits.append("%d:open(%r)" % (n.lineno, mode))
        elif nm in ("write_text", "write_bytes", "writelines"):
            hits.append("%d:%s" % (n.lineno, nm))
        elif base == "os" and nm in ("open", "fdopen", "write", "replace", "rename"):
            hits.append("%d:os.%s" % (n.lineno, nm))
        elif base == "io" and nm == "open":
            hits.append("%d:io.open" % n.lineno)
        elif base == "shutil" and nm in ("move", "copy", "copy2", "copyfile", "copytree"):
            hits.append("%d:shutil.%s" % (n.lineno, nm))
        elif nm == "dump":     # json/pickle/yaml dump-to-file (dumps returns a string; never flagged)
            hits.append("%d:%s.dump" % (n.lineno, base or "?"))
        elif nm == "print" and any(kw.arg == "file" for kw in n.keywords):
            hits.append("%d:print(file=)" % n.lineno)
    return hits


def _raw_write_ops(path):
    return _write_ops_in_source(open(path, encoding="utf-8").read())


_BYPASS = {
    "os.replace":         "import os\nos.replace(t, str(store.mirror_dir(c)/'x'))",
    "io.open":            "import io\nio.open(d, 'w').write(r)",
    "os.open+os.write":   "import os\nfd=os.open(p, os.O_WRONLY)\nos.write(fd, b'x')",
    "os.fdopen":          "import os\nos.fdopen(fd,'w').write(x)",
    "shutil.copyfile":    "import shutil\nshutil.copyfile(s, d)",
    "json.dump":          "import json\njson.dump(o, fh)",
    "print(file=)":       "print(row, file=fh)",
    "writelines":         "fh.writelines(ls)",
    "variable-mode open": "open(p, m).write(x)",
}


def scanner_selftest():
    for name, snip in _BYPASS.items():
        check(bool(_write_ops_in_source(snip)), "SCANNER recognizes %s as a write" % name)
    check(not _write_ops_in_source("open(p).read()\nopen(p,'r').read()\nx.replace('a','b')"),
          "SCANNER does not false-positive on reads / str.replace")


def enrich_prep_exemption():
    """enrich_prep.py is exempt from the structural write scan: it stages store READS as
    plain files in a temp inbox for the enrichment swarm - writes outside the store by
    design. This outcome test is what backs that exemption: the store stays byte-untouched,
    every write lands in the inbox, and an inbox pointed INSIDE the store is refused
    fail-closed before anything is created (the guard lives in enrich_prep itself)."""
    import subprocess
    cwd = tempfile.mkdtemp(prefix="gw_prep_")
    store.ensure_workspace(cwd)
    tpath = os.path.join(cwd, "t.jsonl")
    recs = [
        {"type": "user", "uuid": "pu1", "timestamp": "2026-07-01T00:00:00Z", "sessionId": "prep1234",
         "message": {"role": "user", "content": "how does the retry backoff work?"}},
        {"type": "assistant", "uuid": "pa1", "timestamp": "2026-07-01T00:00:01Z", "sessionId": "prep1234",
         "message": {"role": "assistant", "content": "it doubles per attempt, capped at 60s"}},
    ]
    with open(tpath, "w", encoding="utf-8") as fh:
        fh.write("\n".join(json.dumps(r) for r in recs) + "\n")
    pcx.handle_stop({"cwd": cwd, "transcript_path": tpath, "session_id": "prep1234"})

    root = os.environ["PARSICA_WEAVE_ROOT"]
    def snap():
        out = {}
        for r, _, fs in os.walk(root):
            for f in fs:
                p = os.path.join(r, f)
                st = os.stat(p)
                out[p] = (st.st_size, st.st_mtime_ns)
        return out

    inbox = os.path.join(tempfile.mkdtemp(prefix="gw_inbox_"), "in")
    before = snap()
    r = subprocess.run([sys.executable, os.path.join(SCRIPTS, "enrich_prep.py"),
                        "--cwd", cwd, "--inbox", inbox], capture_output=True, text=True)
    try:
        data = json.loads(r.stdout)
    except Exception:
        data = {}
    check(r.returncode == 0 and data.get("count", 0) >= 1,
          "PREP: stages at least one pending chunk (got %r)" % (r.stdout[:120],))
    check(snap() == before, "OUTCOME: enrich_prep leaves the store byte-untouched")
    staged = [f for f in os.listdir(inbox) if f.endswith(".txt")] if os.path.isdir(inbox) else []
    check(len(staged) == data.get("count"), "OUTCOME: every staged file lands in the inbox")

    evil = os.path.join(root, "evil_inbox")
    r2 = subprocess.run([sys.executable, os.path.join(SCRIPTS, "enrich_prep.py"),
                         "--cwd", cwd, "--inbox", evil], capture_output=True, text=True)
    check(r2.returncode != 0, "GUARD: an inbox inside the store is refused")
    check(not os.path.exists(evil), "GUARD: refusal creates nothing in the store")


def doctor_exemption():
    """doctor.py is exempt from the structural write scan: its only writes are throwaway
    temp files (fake transcripts it feeds to its own capture / isolation checks, always
    under tempfile roots), never the store. This outcome test backs that exemption - run
    the full self-check against a populated store and assert the store is left
    byte-untouched, which is doctor's own contract ("the real store is only ever read")."""
    import subprocess
    root = store.STORE_ROOT

    def snap():
        out = {}
        for r, _, fs in os.walk(root):
            for f in fs:
                p = os.path.join(r, f)
                try:
                    st = os.stat(p)
                    out[p] = (st.st_size, st.st_mtime_ns)
                except OSError:
                    pass
        return out

    before = snap()
    r = subprocess.run([sys.executable, os.path.join(SCRIPTS, "doctor.py")],
                       capture_output=True, text=True,
                       env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    check(r.returncode in (0, 1), "DOCTOR: self-check runs to completion (rc=%d)" % r.returncode)
    check(snap() == before, "OUTCOME: doctor leaves the store byte-untouched")


def structural():
    # enrich_prep.py and doctor.py are exempt, each backed by an outcome test above
    # (byte-untouched store) plus - for enrich_prep - an in-file fail-closed guard, per
    # the discover-don't-list rule. Neither is a content producer; both only ever read
    # the store and write to throwaway temp roots.
    skip = {"store.py", "enrich_prep.py", "doctor.py"}
    mods = []
    for root, _, files in os.walk(SCRIPTS):
        for fn in files:
            if fn.endswith(".py") and not fn.startswith("test_") and fn not in skip:
                mods.append(os.path.join(root, fn))
    for m in sorted(mods):
        ops = _raw_write_ops(m)
        check(not ops, "STRUCTURAL: %s does no raw store write (found %r)" % (os.path.relpath(m, SCRIPTS), ops))


if __name__ == "__main__":
    outcome()
    reserved_subtree()
    scanner_selftest()
    enrich_prep_exemption()
    doctor_exemption()
    structural()
    print("\n" + ("ALL GATEWAY TESTS PASSED (green)"
                  if not fails else "RED: %d failing -> %s" % (len(fails), " | ".join(fails))))
    sys.exit(1 if fails else 0)
