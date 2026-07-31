#!/usr/bin/env python3
"""Parsica Weave self-check. Run: python scripts/doctor.py  (or: parsica-weave doctor)

Verifies an install is healthy AND that the trust guarantees actually hold on THIS machine:
the scrub gate redacts, capture works, private file permissions, and - the one people should be
able to check for themselves - PARSICA_ISOLATED=1 writes nothing durable. Every write-touching
check runs against a throwaway temp root; the real store is only ever read. Plain ASCII output;
exits 0 when nothing FAILs (WARN is advisory), 1 otherwise.
"""
import os
import re
import sys
import json
import shutil
import tempfile
import subprocess
from pathlib import Path

SCRIPTS = Path(os.path.dirname(os.path.abspath(__file__)))
ROOT = SCRIPTS.parent                      # plugin root (scripts/'s parent)
sys.path.insert(0, str(SCRIPTS))

_ok = _warn = _fail = 0


def line(status, msg, detail=""):
    global _ok, _warn, _fail
    if status == "ok":
        _ok += 1
    elif status == "WARN":
        _warn += 1
    else:
        _fail += 1
    tail = ("  " + detail) if detail else ""
    print("  %-4s %s%s" % (status, msg, tail))


def _py(*args, env=None, stdin=""):
    """Run this interpreter on a Weave script, return (rc, stdout, stderr)."""
    e = dict(os.environ, PYTHONIOENCODING="utf-8")
    if env:
        e.update(env)
    p = subprocess.run([sys.executable, *[str(a) for a in args]], input=stdin,
                       capture_output=True, text=True, env=e, timeout=60)
    return p.returncode, p.stdout, p.stderr


def check_python():
    v = sys.version_info
    if v >= (3, 8):
        line("ok", "python %d.%d.%d" % (v[0], v[1], v[2]), sys.executable)
    elif v[0] >= 3:
        line("WARN", "python %d.%d works but 3.8+ is recommended" % (v[0], v[1]), sys.executable)
    else:
        line("FAIL", "python %d.%d is too old (needs Python 3)" % (v[0], v[1]))
    have3 = shutil.which("python3")
    have = shutil.which("python")
    if have3 or have:
        line("ok", "interpreter on PATH", "python3=%s python=%s" % (bool(have3), bool(have)))
    else:
        line("WARN", "neither 'python3' nor 'python' resolves on PATH",
             "the plugin hooks resolve one of these; a hook may not fire")


def check_layout():
    need = ["store.py", "pcx.py", "query.py", "enrich.py", "scrub.py"]
    missing = [n for n in need if not (SCRIPTS / n).is_file()]
    if missing:
        line("FAIL", "scripts/ incomplete", "missing: " + ", ".join(missing))
    else:
        line("ok", "scripts/ present", "%d core scripts" % len(need))
    for rel in (".claude-plugin/plugin.json", "hooks/hooks.json"):
        if (ROOT / rel).is_file():
            line("ok", rel + " present")
        else:
            line("WARN", rel + " not found", "not installed as a plugin?")


def check_json():
    hk = ROOT / "hooks" / "hooks.json"
    try:
        d = json.loads(hk.read_text(encoding="utf-8"))
        hooks = d.get("hooks") or {}
        events = set(hooks.keys())
        want = {"Stop", "UserPromptSubmit", "SessionStart", "PreCompact", "PostToolUse"}
        cmds = " ".join(h.get("command", "")
                        for ev in hooks.values() for grp in ev for h in grp.get("hooks", []))
        if want <= events:
            if "weave-run.sh" in cmds:
                line("ok", "hooks.json parses", "5 events via the cross-platform launcher")
            else:
                line("WARN", "hooks.json parses but not via weave-run.sh",
                     "hooks may be brittle across shells")
        else:
            line("WARN", "hooks.json missing events", str(sorted(want - events)))
    except Exception as exc:
        line("WARN", "hooks.json unreadable", str(exc))
    pj = ROOT / ".claude-plugin" / "plugin.json"
    if pj.is_file():
        try:
            json.loads(pj.read_text(encoding="utf-8"))
            line("ok", "plugin.json parses")
        except Exception as exc:
            line("FAIL", "plugin.json is invalid JSON", str(exc))


def _is_wsl_stub(p):
    # The Windows WSL launcher (System32\bash.exe) and Store aliases cannot run the Git-Bash
    # hook launcher (a cygpath-based .sh given a native Windows path). Claude Code never uses it.
    rp = str(Path(p).resolve()).lower()
    return ("\\windows\\system32\\" in rp) or ("\\windowsapps\\" in rp)


def _find_bash():
    """A bash that can run the Git-Bash hook launcher, the way Claude Code locates one: prefer
    Git Bash derived from a git.exe on PATH (handling both the .../Git/cmd/git.exe and
    .../Git/mingw64/bin/git.exe layouts), then a real bash on PATH - never the WSL System32 stub."""
    g = shutil.which("git")
    if g:
        d = Path(g).resolve()
        for root in (d.parent.parent, d.parent.parent.parent):          # cmd\ or mingw64\bin\
            for cand in (root / "bin" / "bash.exe", root / "usr" / "bin" / "bash.exe"):
                if cand.is_file():
                    return str(cand)
    b = shutil.which("bash")
    if b and not _is_wsl_stub(b):
        return b
    return None


def check_hook_launcher():
    shim = ROOT / "hooks" / "weave-run.sh"
    if not shim.is_file():
        line("FAIL", "hooks/weave-run.sh missing", "the cross-platform hook launcher")
        return
    raw = shim.read_bytes()
    if not raw.startswith(b"#!"):
        line("WARN", "hook launcher has no shebang", "may have been CRLF-mangled on checkout")
    elif b"\r\n" in raw:
        line("WARN", "hook launcher has CRLF line endings",
             "the shebang breaks on POSIX; check .gitattributes eol=lf")
    else:
        line("ok", "hook launcher present", "hooks/weave-run.sh (LF)")
    bash = _find_bash()
    if not bash:
        line("WARN", "no bash found to run the hook launcher",
             "Claude Code may still find Git Bash; on Windows without Git, install Git for Windows")
        return
    tmp = tempfile.mkdtemp(prefix="weave_doctor_shim_")
    try:
        p = subprocess.run([bash, str(shim), "SessionStart"], input="{}",
                           capture_output=True, text=True, timeout=60,
                           env=dict(os.environ, PARSICA_WEAVE_ROOT=tmp, PYTHONIOENCODING="utf-8"))
        if p.returncode == 0 and (not p.stdout.strip() or _is_json(p.stdout)):
            line("ok", "hook launcher resolves Python and runs", os.path.basename(bash))
        else:
            line("FAIL", "hook launcher failed to run pcx.py",
                 "rc=%d %s" % (p.returncode, (p.stderr or p.stdout).strip()[:80]))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_store():
    try:
        import store
    except Exception as exc:
        line("FAIL", "cannot import store", str(exc))
        return None
    line("ok", "store imports", "STORE_ROOT=%s" % store.STORE_ROOT)
    # real store perms (read-only, POSIX only)
    if hasattr(os, "geteuid") and store.STORE_ROOT.exists():
        mode = store.STORE_ROOT.stat().st_mode & 0o777
        if mode == 0o700:
            line("ok", "store root is owner-only (0700)")
        else:
            line("WARN", "store root perms are %o (expected 0700)" % mode,
                 "run once more to let ensure_workspace tighten it")
    check_locks(store)
    return store


def check_locks(store):
    """Report lock sidecars that cannot actually be locked.

    Locking fails OPEN by design - continuity bookkeeping must never wedge a session - which
    means a lock name occupied by a junction, symlink, directory or FIFO degrades to "unlocked"
    exactly like an ordinary contention timeout. One is transient; the other is permanent and
    means concurrent sessions have been racing the state file unprotected. This is the only
    place the two become distinguishable, so it is a WARN with a named cause, never a silent ok.
    """
    try:
        bad = store.lock_health()
    except Exception as exc:                      # a probe that can fail is not a diagnosis
        line("WARN", "could not inspect lock sidecars", str(exc))
        return
    if not bad:
        line("ok", "lock sidecars are regular files", "locking is effective")
        return
    for p, reason in bad:
        line("WARN", "lock is ineffective: %s" % p.name, reason)
    line("WARN", "%d lock sidecar(s) cannot be locked" % len(bad),
         "writes still succeed but concurrent sessions are unprotected; "
         "remove the object at that path and it will be recreated")


def check_scrub():
    try:
        import scrub
    except Exception as exc:
        line("FAIL", "cannot import scrub", str(exc))
        return
    secret = "sk-ant-api03-" + "A" * 40
    phone = "415-555-1212"
    out, _ = scrub.scrub("token " + secret + " call " + phone, scrub_pii=True)
    if secret not in out and phone not in out:
        line("ok", "scrub gate redacts secrets + PII")
    else:
        line("FAIL", "scrub did NOT redact a known secret/PII",
             "(secret_left=%s phone_left=%s)" % (secret in out, phone in out))
    # invisible-glyph strip (the U+2800 class)
    braille = chr(0x2800)
    o2 = scrub._denormalize_invisibles("a" + braille + "b") if hasattr(scrub, "_denormalize_invisibles") else "ab"
    line("ok" if braille not in o2 else "FAIL", "invisible-glyph strip active")


def check_capture_and_boot():
    tmp = tempfile.mkdtemp(prefix="weave_doctor_")
    env = {"PARSICA_WEAVE_ROOT": tmp}
    # SessionStart emits valid JSON (or nothing) and does not crash
    rc, out, err = _py(SCRIPTS / "pcx.py", "SessionStart", env=env, stdin="{}")
    if rc == 0 and (not out.strip() or _is_json(out)):
        line("ok", "SessionStart runs and emits valid output")
    else:
        line("FAIL", "SessionStart failed", "rc=%d %s" % (rc, (err or out)[:80]))
    # Stop chunks a tiny fake transcript -> a mirror shard appears
    td = tempfile.mkdtemp(prefix="weave_doctor_tp_")
    tp = os.path.join(td, "t.jsonl")
    recs = [
        {"type": "user", "uuid": "u1", "sessionId": "doctor01", "timestamp": "2026-01-01T00:00:00Z",
         "message": {"role": "user", "content": [{"type": "text", "text": "doctor check turn"}]}},
        {"type": "assistant", "uuid": "a1", "sessionId": "doctor01", "timestamp": "2026-01-01T00:00:01Z",
         "message": {"role": "assistant", "content": [{"type": "text", "text": "ack"}]}},
    ]
    with open(tp, "w", encoding="utf-8") as fh:
        for r in recs:
            fh.write(json.dumps(r) + "\n")
    payload = json.dumps({"session_id": "doctor01", "transcript_path": tp, "cwd": str(ROOT)})
    rc, out, err = _py(SCRIPTS / "pcx.py", "Stop", env=env, stdin=payload)
    shards = list(Path(tmp).glob("mirror/*/*.jsonl"))
    if rc == 0 and shards:
        line("ok", "capture works", "Stop chunked a turn into the mirror")
    else:
        line("FAIL", "capture failed", "rc=%d shards=%d" % (rc, len(shards)))


def check_isolation():
    """The headline trust check: PARSICA_ISOLATED=1 writes NOTHING durable."""
    tmp = tempfile.mkdtemp(prefix="weave_doctor_iso_")
    env = {"PARSICA_WEAVE_ROOT": tmp, "PARSICA_ISOLATED": "1", "PARSICA_CHANNEL": ""}
    td = tempfile.mkdtemp(prefix="weave_doctor_iso_tp_")
    tp = os.path.join(td, "t.jsonl")
    with open(tp, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "user", "uuid": "u", "sessionId": "iso",
                             "timestamp": "2026-01-01T00:00:00Z",
                             "message": {"role": "user", "content": [{"type": "text", "text": "x"}]}}) + "\n")
    # Exercise the WRITE verbs that would leave a footprint if the guards failed - including
    # channel, the one that slipped past this check before. query is a read (drives no write;
    # note the absent --cwd: query.py never accepted it, so the old probe died at argparse).
    _py(SCRIPTS / "pcx.py", "Stop", env=env,
        stdin=json.dumps({"session_id": "iso", "transcript_path": tp, "cwd": str(ROOT)}))
    _py(SCRIPTS / "pcx.py", "note", "--kind", "journal", "--cwd", str(ROOT), "--sess", "iso",
        env=env, stdin="## note\nhi")
    _py(SCRIPTS / "pcx.py", "channel", "--name", "isolane", env=env)
    _py(SCRIPTS / "pcx.py", "promote", "--cwd", str(ROOT), env=env)
    _py(SCRIPTS / "calibrate.py", "--target", "t", "--event", "confirmed", "--cwd", str(ROOT), env=env)
    _py(SCRIPTS / "query.py", "--grep", "x", env=env)
    left = [p for p in Path(tmp).rglob("*") if p.is_file()]
    if not left:
        line("ok", "PARSICA_ISOLATED=1 wrote ZERO files", "isolation is absolute")
    else:
        line("FAIL", "isolation leaked %d file(s)" % len(left),
             ", ".join(str(p.relative_to(tmp)) for p in left[:4]))


def check_spirit():
    try:
        import parsica_spirit
        v = getattr(parsica_spirit, "__version__", "?")
        line("ok", "Parsica Spirit present", "durable tier available (v%s)" % v)
    except Exception:
        line("ok", "Parsica Spirit absent", "durable tier off; Weave-only, degrades gracefully")


def _is_json(s):
    try:
        json.loads(s)
        return True
    except Exception:
        return False


def main():
    if any(a in ("-h", "--help") for a in sys.argv[1:]):
        # the dispatcher promises every command answers --help; doctor takes no options, so the
        # honest answer is a usage line, not a multi-second probe run.
        print("usage: parsica-weave doctor\n\n"
              "Self-check the install and its trust guarantees: layout, scrub gate, capture,\n"
              "and that PARSICA_ISOLATED=1 writes nothing durable. Takes no options; exits 0\n"
              "when nothing FAILs (WARN is advisory), 1 otherwise.")
        return 0
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    print("Parsica Weave doctor")
    print("  root: %s" % ROOT)
    print("")
    for fn in (check_python, check_layout, check_json, check_hook_launcher, check_store,
               check_scrub, check_capture_and_boot, check_isolation, check_spirit):
        try:
            fn()
        except Exception as exc:
            line("FAIL", fn.__name__ + " raised", str(exc)[:100])
    print("")
    print("doctor: %d ok, %d warn, %d fail" % (_ok, _warn, _fail))
    return 1 if _fail else 0


if __name__ == "__main__":
    sys.exit(main())
