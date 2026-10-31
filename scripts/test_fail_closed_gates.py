#!/usr/bin/env python3
"""Fail-closed gates: REVIEW-2026-10.md, pile one, class (a).

Each test names the lines it closes at e480bbd and proves both directions: BLOCKED (the gate
refuses and the failure is recorded) and ALLOWED (ordinary input still passes).

  1. store.py 580-590: a config.json that is present but unreadable raises StoreReadError
     instead of silently emptying the refusal and human-only lists; an absent config still
     yields the defaults; a valid config still loads; a hook that meets the unreadable config
     stops, captures nothing and records hook_error.
  2. store.py 1022-1024: a state lock that cannot be opened or taken raises StateLockError and
     the body under it never runs; an ordinary lock still locks.
  3. pcx.py 2908-2912: a hook that raises exits 0 AND writes one audit line (category, event,
     exception class; no content); under isolation the line goes to stderr only.
  4. pcx.py 440-448 and 661: the human-only fence runs on the RAW turn before any cut or scrub.
     A marker past the 700-character cut, a fenced name that is also a refusal term, and a
     fenced email address are refused and recorded with the chunk id and size, never the text;
     a clean turn in the same transcript is stored. A refusal by the gateway's own check is
     recorded too.
  5. pcx.py 666-667: a turn that raises while being built is recorded (capture_error) and the
     other turns are still stored.

Runs under pytest and standalone (python test_fail_closed_gates.py).
"""
import io
import json
import os
import sys
import tempfile
from pathlib import Path

# set BEFORE the store import so a first import can never bind STORE_ROOT to the real HOME store.
os.environ.setdefault("PARSICA_WEAVE_ROOT", tempfile.mkdtemp(prefix="failclosed_boot_"))
SCRIPTS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPTS)
import store  # noqa: E402
import pcx    # noqa: E402

MARKER = "[[human" + "-only]]"   # assembled, so this file never carries the live marker whole


def _fresh():
    tmp = tempfile.mkdtemp(prefix="failclosed_root_")
    store.STORE_ROOT = Path(tmp)
    store.GLOBAL_CONFIG = store.STORE_ROOT / "config.json"
    cwd = tempfile.mkdtemp(prefix="failclosed_proj_")
    store.ensure_workspace(cwd)
    return cwd


def _audit_records():
    p = store.STORE_ROOT / store.AUDIT_FILE
    if not p.exists():
        return []
    return [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _mirror_text(cwd):
    out = []
    md = store.mirror_dir(cwd)
    if md.is_dir():
        for f in sorted(md.iterdir()):
            if f.is_file():
                out.append(f.read_text(encoding="utf-8"))
    return "\n".join(out)


def _transcript(cwd, turns):
    """turns: a list of (user_text, assistant_text). Writes the transcript JSONL the Stop hook reads."""
    tpath = os.path.join(cwd, "transcript.jsonl")
    recs = []
    for i, (u, a) in enumerate(turns):
        recs.append({"type": "user", "uuid": "u%d" % i, "timestamp": "2026-07-01T00:%02d:00Z" % i,
                     "sessionId": "sess0001", "message": {"role": "user", "content": u}})
        recs.append({"type": "assistant", "uuid": "a%d" % i, "timestamp": "2026-07-01T00:%02d:01Z" % i,
                     "sessionId": "sess0001", "message": {"role": "assistant", "content": a}})
    with open(tpath, "w", encoding="utf-8") as fh:
        fh.write("\n".join(json.dumps(r) for r in recs) + "\n")
    return tpath


def _run_main(event, data):
    """Drive pcx.main() the way the hook launcher does: event on argv, payload on stdin. Returns
    what the hook wrote to stderr."""
    old_argv, old_stdin, old_stderr = sys.argv, sys.stdin, sys.stderr
    sys.argv = ["pcx.py", event]
    sys.stdin = type("S", (), {"buffer": io.BytesIO(json.dumps(data).encode("utf-8"))})()
    sys.stderr = io.StringIO()
    try:
        pcx.main()
        return sys.stderr.getvalue()
    finally:
        sys.argv, sys.stdin, sys.stderr = old_argv, old_stdin, old_stderr


def _set_env(name, value):
    old = os.environ.get(name)
    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = value
    return old


# ---------------------------------------------------------------- 1. config: present but unreadable

def test_unreadable_config_fails_closed_and_absent_config_still_defaults():
    cwd = _fresh()
    # ALLOWED: absent -> defaults; valid -> its values
    cfg = store.load_config(cwd)
    assert cfg["refusal"] == [] and cfg["scrub_pii"] is True
    store.GLOBAL_CONFIG.write_text(json.dumps({"refusal": ["GlobalTerm"], "human_only_terms": ["Nimbus"]}),
                                   encoding="utf-8")
    cfg = store.load_config(cwd)
    assert cfg["refusal"] == ["GlobalTerm"] and cfg["human_only_terms"] == ["Nimbus"]
    # BLOCKED: present but unparseable -> StoreReadError, never empty fences
    store.GLOBAL_CONFIG.write_text('{"refusal": ["GlobalTerm"], "human_only_terms": ["Nimbus"],  TYPO',
                                   encoding="utf-8")
    try:
        raised = None
        try:
            store.load_config(cwd)
        except store.StoreReadError as exc:
            raised = str(exc)
        assert raised is not None, "an unreadable config must refuse, not degrade to empty fences"
        assert "config.json" in raised and "GlobalTerm" not in raised and "Nimbus" not in raised
        # the OUTPUT fence union refuses too: every sink reads it, and a corrupt config anywhere in
        # the workspace cannot silently drop its terms from the union
        raised = False
        try:
            store.workspace_refusal_terms()
        except store.StoreReadError:
            raised = True
        assert raised, "the workspace refusal union must refuse on an unreadable config"
    finally:
        store.GLOBAL_CONFIG.write_text("{}", encoding="utf-8")   # repaired: never leave it corrupt
    # repaired -> everything works again
    assert store.load_config(cwd)["refusal"] == []


def test_unreadable_config_stops_the_hook_captures_nothing_and_is_recorded():
    cwd = _fresh()
    tpath = _transcript(cwd, [("an ordinary question", "an ordinary answer")])
    md = store.mirror_dir(cwd)                 # resolved BEFORE the config is broken (the key needs it)
    store.GLOBAL_CONFIG.write_text("{ not json", encoding="utf-8")
    try:
        err = _run_main("Stop", {"cwd": cwd, "transcript_path": tpath, "session_id": "sess0001"})
    finally:
        store.GLOBAL_CONFIG.write_text("{}", encoding="utf-8")   # repaired: never leave it corrupt
    stored = "\n".join(f.read_text(encoding="utf-8") for f in md.iterdir() if f.is_file())
    assert stored == "", "nothing may be captured while the config cannot be read"
    recs = _audit_records()
    assert recs and recs[-1]["category"] == "hook_error" and recs[-1]["error"] == "StoreReadError"
    assert recs[-1]["event"] == "Stop" and "hook_error" in err


# ---------------------------------------------------------------- 2. the state lock

def test_state_lock_refuses_instead_of_running_unlocked():
    _fresh()
    pdir = store.STORE_ROOT / "projects" / "lock-probe"
    os.makedirs(str(pdir), exist_ok=True)
    p = pdir / ".state.json"
    # ALLOWED: an ordinary sidecar locks
    with store._state_lock(p) as got:
        assert got is True
    # BLOCKED: a directory at the lock name -> StateLockError at entry, body never runs
    os.remove(str(p) + ".lock")                 # the sidecar the first lock created
    os.makedirs(str(p) + ".lock")
    ran = []
    raised = False
    try:
        with store._state_lock(p):
            ran.append(True)
    except store.StateLockError:
        raised = True
    assert raised and not ran, "a lock name that is a directory must refuse, not run unlocked"
    os.rmdir(str(p) + ".lock")
    # BLOCKED: held by another descriptor past the timeout -> StateLockError, body never runs
    fd = os.open(str(p) + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        raised = False
        try:
            with store._state_lock(p, timeout=0.2):
                ran.append(True)
        except store.StateLockError:
            raised = True
        assert raised and not ran, "a contended lock must refuse, not run the merge unlocked"
    finally:
        try:
            if os.name == "nt":
                import msvcrt
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)
    # ALLOWED again once released
    with store._state_lock(p) as got:
        assert got is True


# ---------------------------------------------------------------- 3. hooks record their failures

def test_hook_failure_writes_one_audit_line_and_exits_clean():
    cwd = _fresh()

    def boom(data):
        raise RuntimeError("payload sk-ant-" + "A" * 24 + " must not be recorded")

    orig = pcx.HANDLERS["Stop"]
    pcx.HANDLERS["Stop"] = boom
    try:
        err = _run_main("Stop", {"cwd": cwd})      # must not raise
    finally:
        pcx.HANDLERS["Stop"] = orig
    recs = _audit_records()
    assert len(recs) == 1, recs
    assert recs[0]["category"] == "hook_error" and recs[0]["event"] == "Stop"
    assert recs[0]["error"] == "RuntimeError"
    blob = json.dumps(recs)
    assert "sk-ant-" not in blob and "payload" not in blob, "an audit line never carries content"
    assert "hook_error" in err, "the failure is also said once on stderr"


def test_hook_failure_under_isolation_leaves_zero_footprint():
    cwd = _fresh()
    before = sorted(str(p) for p in store.STORE_ROOT.rglob("*"))

    def boom(data):
        raise RuntimeError("x")

    orig = pcx.HANDLERS["SessionStart"]
    pcx.HANDLERS["SessionStart"] = boom
    old = _set_env("PARSICA_ISOLATED", "1")
    try:
        err = _run_main("SessionStart", {"cwd": cwd})
    finally:
        _set_env("PARSICA_ISOLATED", old)
        pcx.HANDLERS["SessionStart"] = orig
    after = sorted(str(p) for p in store.STORE_ROOT.rglob("*"))
    assert after == before, "isolation means zero footprint, audit line included"
    assert "hook_error" in err, "but the failure is still said on stderr"


# ---------------------------------------------------------------- 4. the fence runs first, on the raw turn

def test_fence_runs_on_the_raw_turn_before_cut_and_scrub():
    cwd = _fresh()
    store.GLOBAL_CONFIG.write_text(json.dumps({
        "refusal": ["Nimbus"],                                  # a name on BOTH lists
        "human_only_terms": ["Nimbus", "ann@example.test"],     # and an email address as a fenced term
    }), encoding="utf-8")
    long_private = ("private line. " * 60) + MARKER             # the marker lands past the 700-char cut
    assert len(long_private) > 700
    turns = [
        (long_private, "noted"),                                 # (e) marker past the cut
        ("about the Nimbus rollout timing", "sure"),             # (f) fenced name that is also a refusal term
        ("mail ann@example.test tonight", "will do"),            # (g) fenced email address
        ("what time is the standup", "nine, same as always"),    # ALLOWED: a clean turn
    ]
    tpath = _transcript(cwd, turns)
    pcx.handle_stop({"cwd": cwd, "transcript_path": tpath, "session_id": "sess0001"})
    stored = _mirror_text(cwd)
    assert "standup" in stored, "the clean turn must still be stored"
    assert "private line" not in stored, "(e): a marker past the cut must refuse the whole turn"
    assert "rollout timing" not in stored and "<redacted>" not in stored, "(f): fence before the refusal scrub"
    assert "tonight" not in stored and "[EMAIL]" not in stored, "(g): fence before the PII scrub"
    recs = [r for r in _audit_records() if r["category"] == "human_only_refused"]
    assert len(recs) == 3, recs
    for r in recs:
        assert r["event"] == "mirror" and str(r["chunk"]).startswith(("cid-", "syn-"))
        assert isinstance(r["size"], int) and r["size"] > 0 and r["date"] == "2026-07-01"
    blob = json.dumps(recs)
    for word in ("private", "Nimbus", "rollout", "ann@", "tonight", "human-only", "[["):
        assert word not in blob, "an audit line never carries content: %s" % word
    # the refused turns are marked seen, so a second Stop over the same transcript records nothing new
    pcx.handle_stop({"cwd": cwd, "transcript_path": tpath, "session_id": "sess0001"})
    assert len([r for r in _audit_records() if r["category"] == "human_only_refused"]) == 3


def test_gateway_refusal_on_the_capture_path_is_recorded():
    cwd = _fresh()
    # a term that matches a FIELD VALUE of every chunk (the id prefix) refuses at the gateway's
    # record-form check, not at the raw-text check; the receipt used to be discarded at pcx.py 661
    store.GLOBAL_CONFIG.write_text(json.dumps({"human_only_terms": ["cid-"]}), encoding="utf-8")
    tpath = _transcript(cwd, [("an ordinary turn", "ok")])
    pcx.handle_stop({"cwd": cwd, "transcript_path": tpath, "session_id": "sess0001"})
    assert _mirror_text(cwd) == ""
    recs = [r for r in _audit_records() if r["category"] == "capture_refused"]
    assert len(recs) == 1 and recs[0]["reason"] == "human_only" and recs[0]["event"] == "mirror"
    assert "ordinary" not in json.dumps(recs)


# ---------------------------------------------------------------- 5. a failed turn is recorded

def test_a_turn_that_fails_to_build_is_recorded_and_the_rest_is_stored():
    cwd = _fresh()
    tpath = _transcript(cwd, [("first ordinary turn", "ok"), ("second ordinary turn", "ok"),
                              ("third ordinary turn", "ok")])
    real = pcx._build_chunk

    def flaky(turn, cfg, chan=""):
        if "second" in json.dumps(turn):
            raise ValueError("boom on the second turn")
        return real(turn, cfg, chan)

    pcx._build_chunk = flaky
    try:
        pcx.handle_stop({"cwd": cwd, "transcript_path": tpath, "session_id": "sess0001"})
    finally:
        pcx._build_chunk = real
    stored = _mirror_text(cwd)
    assert "first ordinary" in stored and "third ordinary" in stored, "the batch survives one bad turn"
    assert "second ordinary" not in stored
    recs = [r for r in _audit_records() if r["category"] == "capture_error"]
    assert len(recs) == 1 and recs[0]["error"] == "ValueError" and "boom" not in json.dumps(recs)


def _run():
    fns = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for name, fn in fns:
        fn()
        print("  ok  %s" % name)
    print("[test_fail_closed_gates] %d/%d passed" % (len(fns), len(fns)))


if __name__ == "__main__":
    _run()
