#!/usr/bin/env python3
"""Concurrency tests for the two durability items deferred from the gateway pass.

  * MIRROR (windows race): K worker PROCESSES on ONE project - each a simulated Claude Code
    window with its own transcript - fire handle_stop simultaneously. Every captured chunk
    must survive, and every production reader (dedup ids, SessionStart index, query) must see
    them; retention must ROTATE per-window shards by DATE (to archive/, never unlink).
    Processes, not threads: the fix is
    per-PID sharding, and two threads share a pid (two real windows never do - a live pid is
    unique, and same-pid hook processes are sequential by construction).
  * STATE (turns race): N THREADS fire handle_stop for ONE session (no transcript)
    simultaneously. Every increment must land on that session's own counter - max() merge
    only guards regression, not a dropped increment - and the checkpoint cadence must fire
    on the true per-session count.

Written RED against the pre-fix code (probe on this box: ~15-20% of concurrent same-file
appends silently lost, no exception, no corrupt lines). The fix turns these green without
touching the assertions.

Run: python test_concurrency.py            (spawns its own --worker subprocesses)
"""
import io
import os
import sys
import json
import time
import tempfile
import threading
import subprocess

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPTS)

WORKERS = 4        # simulated concurrent windows (processes)
TURNS = 60         # complete turns per window, all on one calendar date
STOPS = 24         # concurrent Stop hooks for the turn_count test (threads)
CADENCE = 8        # checkpoint_every_n for the cadence assertion (STOPS % CADENCE == 0)
DATE = "2026-06-27"

fails = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        fails.append(msg)


def _transcript(cwd, wid):
    recs = []
    for i in range(TURNS):
        recs.append({"type": "user", "uuid": "w%d-u%d" % (wid, i),
                     "timestamp": DATE + "T10:%02d:00Z" % (i % 60), "sessionId": "sess%d" % wid,
                     "message": {"role": "user", "content": "turn %d from window %d" % (i, wid)}})
        recs.append({"type": "assistant", "uuid": "w%d-a%d" % (wid, i),
                     "timestamp": DATE + "T10:%02d:01Z" % (i % 60), "sessionId": "sess%d" % wid,
                     "message": {"role": "assistant", "content": "reply %d from window %d" % (i, wid)}})
    tpath = os.path.join(cwd, "transcript-%d.jsonl" % wid)
    with open(tpath, "w", encoding="utf-8") as fh:
        fh.write("\n".join(json.dumps(r) for r in recs) + "\n")
    return tpath


def _worker_main(argv):
    """One simulated window: build a transcript, wait at the barrier, fire Stop."""
    root, cwd, wid, go = argv[0], argv[1], int(argv[2]), argv[3]
    os.environ["PARSICA_WEAVE_ROOT"] = root       # before importing store
    import pcx
    tpath = _transcript(cwd, wid)
    while not os.path.exists(go):                   # barrier: all windows fire together
        time.sleep(0.001)
    pcx.handle_stop({"cwd": cwd, "transcript_path": tpath, "session_id": "sess%d" % wid})


def _all_chunk_ids(md):
    ids = []
    for f in sorted(md.glob("*.jsonl")):
        try:
            lines = f.read_text(encoding="utf-8").splitlines()
        except Exception:
            continue
        for ln in lines:
            ln = ln.strip()
            if not ln:
                continue
            try:
                o = json.loads(ln)
            except Exception:
                continue
            if isinstance(o, dict) and o.get("id"):
                ids.append(o["id"])
    return ids


def mirror_windows_race(root):
    import store, pcx, query
    cwd = tempfile.mkdtemp(prefix="cc_mirror_proj_")
    store.ensure_workspace(cwd)
    md = store.mirror_dir(cwd)
    go = os.path.join(cwd, "go")

    procs = [subprocess.Popen([sys.executable, os.path.abspath(__file__), "--worker",
                               root, cwd, str(w), go]) for w in range(WORKERS)]
    time.sleep(0.8)                                  # let every worker reach the barrier
    open(go, "w").close()
    # exit codes matter: a worker that crashes AFTER its mirror flush (e.g. inside the
    # locked save_state - the only cross-PROCESS exercise of the lock in this suite)
    # would otherwise pass every content assertion below vacuously.
    rcs = [p.wait(timeout=180) for p in procs]
    check(all(rc == 0 for rc in rcs), "MIRROR: every worker window exited cleanly (rcs=%r)" % rcs)

    # ids are opaque now: a stored chunk id is cid-<hash of the transcript uuid>, not the raw uuid.
    expected = {pcx._safe_chunk_id("w%d-u%d" % (w, i), "") for w in range(WORKERS) for i in range(TURNS)}
    got = set(_all_chunk_ids(md))
    missing = expected - got
    check(not missing, "MIRROR: no chunk lost under %d concurrent windows (missing %d of %d)"
          % (WORKERS, len(missing), len(expected)))

    # every production reader must see every chunk, across however many files hold them
    seen = pcx._recent_ids(md, 14)
    check(expected <= seen, "MIRROR: dedup reader (_recent_ids) sees every chunk")
    qids = {c.get("id") for c in query._load_chunks(cwd=cwd)}
    check(expected <= qids, "MIRROR: query reader (_load_chunks) sees every chunk")
    idx_rows = [l for l in pcx._mirror_index(cwd, 10).splitlines() if l.strip()]
    check(len(idx_rows) == 10, "MIRROR: SessionStart index returns n rows over the mirror")

    # id-dedup across windows: a resume in a FRESH process replays an already-captured turn
    dup = subprocess.run([sys.executable, os.path.abspath(__file__), "--worker",
                          root, cwd, "0", go])      # same window-0 turns, new transcript read
    check(dup.returncode == 0 and _all_chunk_ids(md).count(pcx._safe_chunk_id("w0-u0", "")) == 1,
          "MIRROR: id-dedup holds across windows (replayed turn not duplicated)")

    # cross-PROCESS turn accounting is PER SESSION: 4 workers + the replay above = 5 Stops
    # on this project, each incrementing ITS OWN transcript's key under the file lock from
    # its own process - worker 0's key sees the replay, every other key exactly one Stop.
    turns = store.load_state(cwd).get("turns", {})
    want = {("tp:transcript-%d.jsonl" % w): (2 if w == 0 else 1) for w in range(WORKERS)}
    check(turns == want,
          "STATE: cross-process per-session turns exact across %d windows + replay (got %r)"
          % (WORKERS, turns))

    # retention ROTATES by DATE - an old date's shards (incl. the legacy day-file) MOVE to
    # archive/, never unlinked; the active day is untouched.
    (md / "2020-01-01.jsonl").write_text('{"id": "old-legacy"}\n', encoding="utf-8")
    (md / "2020-01-01.4242.jsonl").write_text('{"id": "old-shard"}\n', encoding="utf-8")
    store.rotate_mirror(cwd, 1)
    check(not (md / "2020-01-01.jsonl").exists() and not (md / "2020-01-01.4242.jsonl").exists(),
          "MIRROR: retention rotates an old date incl. its per-window shards out of the active set")
    check((md / "archive" / "2020-01-01.jsonl").exists() and (md / "archive" / "2020-01-01.4242.jsonl").exists(),
          "MIRROR: rotated shards land in archive/ (moved, never deleted)")
    check(expected <= set(_all_chunk_ids(md)),
          "MIRROR: retention keeps every chunk of the active day")


def checkpoint_clear_race(root):
    """A due the user just acknowledged must STAY cleared even when another window's Stop -
    which loaded state while the due was still armed - saves afterwards. Guards a bug where the
    merge's else-branch preferred the caller's stale in-memory checkpoint_due over disk,
    silently re-arming an acknowledged checkpoint (duplicate directive next prompt)."""
    import store, pcx
    cwd = tempfile.mkdtemp(prefix="cc_due_proj_")
    store.ensure_workspace(cwd)
    with open(store.project_dir(cwd) / "config.json", "w", encoding="utf-8") as fh:
        json.dump({"checkpoint_every_n": CADENCE}, fh)

    SESS = "seedsess"                               # ONE session: cadence is per-session now
    for _ in range(CADENCE):                        # reach the cadence via the real path
        pcx.handle_stop({"cwd": cwd, "transcript_path": None, "session_id": SESS})
    if store.load_state(cwd).get("checkpoint_due", {}).get(SESS) != CADENCE:
        check(False, "DUE: precondition failed (cadence did not arm checkpoint_due)")
        return

    stale = store.load_state(cwd)                   # window B's Stop loads: due still armed
    cap, old = io.StringIO(), sys.stdout            # window A's prompt acknowledges + clears
    sys.stdout = cap
    try:
        pcx.handle_prompt({"cwd": cwd, "session_id": SESS})
    finally:
        sys.stdout = old
    check("checkpoint" in cap.getvalue()
          and store.load_state(cwd).get("checkpoint_due", {}).get(SESS) is None,
          "DUE: prompt acknowledges and clears the due on disk")

    stale["_increment_turn"] = (SESS, CADENCE)      # window B's Stop lands late
    store.save_state(cwd, stale)
    st = store.load_state(cwd)
    check(st.get("turns", {}).get(SESS) == CADENCE + 1,
          "DUE: the late Stop still increments (got %s)" % st.get("turns", {}).get(SESS))
    check(st.get("checkpoint_due", {}).get(SESS) is None,
          "DUE: an acknowledged due is NOT resurrected by a late Stop's stale state (due=%s)"
          % st.get("checkpoint_due", {}).get(SESS))


def turn_count_race(root):
    import store, pcx
    cwd = tempfile.mkdtemp(prefix="cc_state_proj_")
    store.ensure_workspace(cwd)
    with open(store.project_dir(cwd) / "config.json", "w", encoding="utf-8") as fh:
        json.dump({"checkpoint_every_n": CADENCE}, fh)

    SESS = "racesess"                    # ONE session across every thread: its per-session
    barrier = threading.Barrier(STOPS)   # counter is what must not drop an increment
    errors = []

    def stop(i):
        try:
            barrier.wait()
            pcx.handle_stop({"cwd": cwd, "transcript_path": None, "session_id": SESS})
        except Exception as ex:
            errors.append(repr(ex))

    threads = [threading.Thread(target=stop, args=(i,)) for i in range(STOPS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    st = store.load_state(cwd)
    check(not errors, "STATE: no Stop hook raised (%r)" % errors[:3])
    check(st.get("turns", {}).get(SESS) == STOPS,
          "STATE: no increment lost under %d concurrent Stops (got %s, want %d)"
          % (STOPS, st.get("turns", {}).get(SESS), STOPS))
    check(st.get("checkpoint_due", {}).get(SESS) == STOPS,
          "STATE: checkpoint cadence fires on the true per-session count (due=%s, want %d)"
          % (st.get("checkpoint_due", {}).get(SESS), STOPS))


def run():
    root = tempfile.mkdtemp(prefix="cc_root_")
    os.environ["PARSICA_WEAVE_ROOT"] = root       # before importing store
    mirror_windows_race(root)
    checkpoint_clear_race(root)
    turn_count_race(root)
    print("\n" + ("ALL CONCURRENCY TESTS PASSED (green)" if not fails
                  else "RED: %d failing -> %s" % (len(fails), " | ".join(fails))))
    return 1 if fails else 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--worker":
        _worker_main(sys.argv[2:])
        sys.exit(0)
    sys.exit(run())
