#!/usr/bin/env python3
"""Tests for the Parsica Weave hook engine (pcx.py). Run: python test_engine.py

Locks the capture-layer invariants: Stop flushes complete
turns and advances the cursor; replay/resume never duplicates; secrets are scrubbed at
capture; PreCompact holds an in-progress trailing turn (no partial write, no cursor
advance) and the next Stop captures it exactly once; the recent-mirror index surfaces the
NEWEST chunks in chronological order; SessionStart re-inject is scrubbed."""
import os
import sys
import io
import json
import tempfile
import shutil
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _u(uid, ts, sess, text):
    return {"type": "user", "uuid": uid, "timestamp": ts, "sessionId": sess,
            "message": {"role": "user", "content": text}}


def _a(uid, ts, sess, text, tool=None, fp=None, cmd=None):
    blocks = [{"type": "text", "text": text}]
    if tool:
        inp = {}
        if fp:
            inp["file_path"] = fp
        if cmd:
            inp["command"] = cmd
        blocks.append({"type": "tool_use", "name": tool, "input": inp})
    return {"type": "assistant", "uuid": uid, "timestamp": ts, "sessionId": sess,
            "message": {"role": "assistant", "content": blocks}}


def _write(path, recs):
    path.write_text("\n".join(json.dumps(r) for r in recs) + "\n", encoding="utf-8")


def _day_files(md, date):
    """A date's mirror files: legacy day-file + per-window shards (the flush writes shards)."""
    return sorted(md.glob(date + ".jsonl")) + sorted(md.glob(date + ".*.jsonl"))


def _day_rows(md, date):
    rows = []
    for f in _day_files(md, date):
        rows += [json.loads(l) for l in f.read_text(encoding="utf-8").splitlines() if l.strip()]
    return rows


def _ids(md, date):
    return [r["id"] for r in _day_rows(md, date)]


def _day_text(md, date):
    return "".join(f.read_text(encoding="utf-8") for f in _day_files(md, date))


def run():
    tmp = tempfile.mkdtemp()
    os.environ["PARSICA_WEAVE_ROOT"] = tmp
    import store
    import pcx
    fails = []

    def ck(c, m):
        print(("ok  " if c else "FAIL ") + m)
        if not c:
            fails.append(m)

    cwd = tmp + "/proj"
    store.ensure_workspace(cwd)
    md = store.mirror_dir(cwd)
    DAY = "2026-06-27"
    tpath = Path(tmp) / "sessionABCD.jsonl"
    data = {"cwd": cwd, "transcript_path": str(tpath), "session_id": "sessionABCD"}

    # one complete turn -> Stop flushes it + advances cursor + bumps the counter
    recs = [_u("u1", "2026-06-27T10:00:00Z", "sessionABCD", "add an Edit to effects.py"),
            _a("a1", "2026-06-27T10:00:01Z", "sessionABCD", "done", tool="Edit", fp="myapp/security/effects.py")]
    _write(tpath, recs)
    pcx.handle_stop(data)
    rows = _day_rows(md, DAY)
    ck(len(rows) == 1 and rows[0]["id"] == pcx._safe_chunk_id("u1", ""), "Stop flushes a complete turn to the mirror")
    ck("security/effects.py" in rows[0].get("files", []), "file tag = last-2 path components")
    ck(store.load_state(cwd).get("turns", {}).get("tp:sessionABCD.jsonl") == 1,
       "Stop bumps THIS session's turn counter (per-session accounting)")

    # re-run Stop on an unchanged transcript -> no duplicate (size short-circuit + id dedup)
    pcx.handle_stop(data)
    ck(_ids(md, DAY) == [pcx._safe_chunk_id("u1", "")], "re-running Stop on unchanged transcript writes no duplicate")

    # append a second turn -> only the new one is captured (cursor advanced)
    recs += [_u("u2", "2026-06-27T10:05:00Z", "sessionABCD", "now run the tests"),
             _a("a2", "2026-06-27T10:05:01Z", "sessionABCD", "green", tool="Bash", cmd="python test_scrub.py")]
    _write(tpath, recs)
    pcx.handle_stop(data)
    rows = _day_rows(md, DAY)
    ck([r["id"] for r in rows] == [pcx._safe_chunk_id("u1", ""), pcx._safe_chunk_id("u2", "")], "Stop appends only the new turn")
    ck("python test_scrub.py" in rows[1].get("cmds", []), "Bash command label captured")

    # a secret pasted into a turn is scrubbed at capture
    recs += [_u("u3", "2026-06-27T10:09:00Z", "sessionABCD", "my key is sk-ant-api03-" + "A" * 24),
             _a("a3", "2026-06-27T10:09:01Z", "sessionABCD", "noted")]
    _write(tpath, recs)
    pcx.handle_stop(data)
    ck("sk-ant-api03-AAAA" not in _day_text(md, DAY),
       "secret in a captured turn is scrubbed before write")

    # PreCompact holds an in-progress trailing turn: no partial write, no cursor advance
    before_cursors = dict(store.load_state(cwd)["cursors"])
    recs2 = recs + [_u("u4", "2026-06-27T10:12:00Z", "sessionABCD", "in-progress prompt, no assistant yet")]
    _write(tpath, recs2)
    pcx.handle_precompact(data)
    ck("u4" not in _ids(md, DAY), "PreCompact does not write an in-progress trailing turn")
    ck(store.load_state(cwd)["cursors"] == before_cursors, "PreCompact does not advance the cursor")

    # the next Stop (turn now complete) captures u4 exactly once - no drop, no dup
    recs2 += [_a("a4", "2026-06-27T10:12:30Z", "sessionABCD", "finished")]
    _write(tpath, recs2)
    pcx.handle_stop(data)
    ck(_ids(md, DAY).count(pcx._safe_chunk_id("u4", "")) == 1, "u4 captured exactly once after PreCompact + Stop")

    # _mirror_index: newest chunks, chronological, when newest day is small
    (md / "2026-06-25.jsonl").write_text(
        "\n".join(json.dumps({"id": f"z{i}", "ts": f"2026-06-25T{10 + i:02d}:00:00Z", "sess": "s", "user": f"older {i}"})
                  for i in range(8)) + "\n", encoding="utf-8")
    idx = pcx._mirror_index(cwd, 5)
    lines = [l for l in idx.splitlines() if l.strip()]
    ck(len(lines) == 5, "mirror index returns exactly n rows")
    ck("older 7" in idx and "older 0" not in idx, "mirror index keeps the most-recent rows, drops the oldest")
    # expected stamps via the SAME display helper - never a hardcoded zone offset
    t_old = store.local_stamp("2026-06-25T17:00:00Z")   # "older 7", the newest 06-25 row
    t_new = store.local_stamp("2026-06-27T10:00:00Z")   # u1, the oldest 06-27 row
    ck(idx.rfind(t_old) < idx.find(t_new), "mirror index is chronological (older before newer)")
    ck(f"- {t_new} [{store.sess_token('sessionA')}] add an Edit" in idx,
       "index rows render LOCAL time via the shared helper (storage stays UTC)")

    # SessionStart re-inject scrubs a secret an agent wrote into the working set (defense-in-depth)
    store.working_path(cwd).write_text(
        "# Pinned\n- prod token is sk-ant-api03-" + "B" * 24 + "\n- open loop: ship it\n", encoding="utf-8")
    cap, old = io.StringIO(), sys.stdout
    sys.stdout = cap
    try:
        pcx.handle_session_start(data)
    finally:
        sys.stdout = old
    out = cap.getvalue()
    ck("sk-ant-api03-BBBB" not in out, "SessionStart re-inject scrubs a secret in working.md")
    ck("ship it" in out, "SessionStart re-injects the working set")
    ck(store.local_zone_note() in out,
       "mirror-index header carries the local-zone note (once per surface, not per row)")

    # --- a non-string (int epoch) ts must not wedge the whole flush ---
    tp2 = Path(tmp) / "sessionWEDGE.jsonl"
    data2 = {"cwd": cwd, "transcript_path": str(tp2), "session_id": "sessionWEDGE"}
    _write(tp2, [_u("w0", "2026-06-27T09:00:00Z", "sessionWEDGE", "clean turn before poison"),
                 _a("w0a", "2026-06-27T09:00:01Z", "sessionWEDGE", "ok"),
                 _u("w1", 1719481200, "sessionWEDGE", "poison int-epoch ts"),        # non-string ts
                 _a("w1a", "2026-06-27T09:05:01Z", "sessionWEDGE", "ok2")])
    pcx.handle_stop(data2)        # must not raise, must not drop the clean turn
    allids = [c.get("id") for f in md.glob("*.jsonl")
              for c in (json.loads(l) for l in f.read_text(encoding="utf-8").splitlines() if l.strip())]
    ck(pcx._safe_chunk_id("w0", "") in allids, "non-string ts: the clean turn in the same flush is still captured (no wedge)")
    ck(store.load_state(cwd)["cursors"].get("tp:sessionWEDGE.jsonl"), "non-string ts: cursor still advances")

    # --- a ts=None chunk must not crash _mirror_index / suppress the re-inject ---
    (md / "2026-06-24.jsonl").write_text(
        json.dumps({"id": "nullts", "ts": None, "sess": "s", "user": "null-ts chunk"}) + "\n", encoding="utf-8")
    try:
        pcx._mirror_index(cwd, 5)
        ck(True, "ts=None chunk does not crash _mirror_index")
    except Exception as ex:
        ck(False, "ts=None crashed _mirror_index: " + repr(ex))

    # --- a secret in a tool name / skill attribution is scrubbed at capture ---
    tp3 = Path(tmp) / "sessionTOOL.jsonl"
    data3 = {"cwd": cwd, "transcript_path": str(tp3), "session_id": "sessionTOOL"}
    arec = _a("t1a", "2026-06-27T08:00:01Z", "sessionTOOL", "ran a tool")
    arec["attributionSkill"] = "skill-sk-ant-api03-" + "Z" * 24
    arec["message"]["content"].append({"type": "tool_use", "name": "Tool-sk-ant-api03-" + "Z" * 24, "input": {}})
    _write(tp3, [_u("t1", "2026-06-27T08:00:00Z", "sessionTOOL", "use the tool"), arec])
    pcx.handle_stop(data3)
    alltext = "".join(f.read_text(encoding="utf-8") for f in md.glob("*.jsonl"))
    ck("sk-ant-api03-ZZZZ" not in alltext, "secret in a tool name / skill attribution is scrubbed at capture")

    # --- a uuid-less genuine prompt is captured exactly once (synthetic-id dedup) ---
    tp4 = Path(tmp) / "sessionNOUUID.jsonl"
    data4 = {"cwd": cwd, "transcript_path": str(tp4), "session_id": "sessionNOUUID"}
    u_no = _u("x", "2026-06-27T07:00:00Z", "sessionNOUUID", "no-uuid prompt please")
    del u_no["uuid"]
    recs4 = [u_no, _a("n1a", "2026-06-27T07:00:01Z", "sessionNOUUID", "reply to no-uuid"),
             _u("nB", "2026-06-27T07:05:00Z", "sessionNOUUID", "trailing in-progress")]
    _write(tp4, recs4)
    pcx.handle_precompact(data4)
    recs4 += [_a("nBa", "2026-06-27T07:05:30Z", "sessionNOUUID", "done")]
    _write(tp4, recs4)
    pcx.handle_stop(data4)
    nouuid = [c for f in md.glob("*.jsonl")
              for c in (json.loads(l) for l in f.read_text(encoding="utf-8").splitlines() if l.strip())
              if c.get("user") == "no-uuid prompt please"]
    ck(len(nouuid) == 1, "uuid-less turn captured exactly once across PreCompact + Stop (synthetic id)")

    # --- a non-dict `message` record must not wedge the flush ---
    tp5 = Path(tmp) / "sessionMSG.jsonl"
    data5 = {"cwd": cwd, "transcript_path": str(tp5), "session_id": "sessionMSG"}
    _write(tp5, [_u("m0", "2026-06-27T06:00:00Z", "sessionMSG", "clean before non-dict message"),
                 _a("m0a", "2026-06-27T06:00:01Z", "sessionMSG", "ok"),
                 {"type": "user", "uuid": "m1", "timestamp": "2026-06-27T06:01:00Z",
                  "sessionId": "sessionMSG", "message": None}])     # message present but None
    pcx.handle_stop(data5)         # must not raise / wedge
    allids2 = [c.get("id") for f in md.glob("*.jsonl")
               for c in (json.loads(l) for l in f.read_text(encoding="utf-8").splitlines() if l.strip())]
    ck(pcx._safe_chunk_id("m0", "") in allids2, "non-dict message: the clean turn is still captured (no wedge)")

    # --- a uuid-less turn whose asst text GROWS between flushes -> captured once ---
    tp6 = Path(tmp) / "sessionGROW.jsonl"
    data6 = {"cwd": cwd, "transcript_path": str(tp6), "session_id": "sessionGROW"}
    g_u = _u("g", "2026-06-27T05:00:00Z", "sessionGROW", "grow turn prompt")
    del g_u["uuid"]
    g_a = {"type": "assistant", "uuid": "ga", "timestamp": "2026-06-27T05:00:01Z", "sessionId": "sessionGROW",
           "message": {"role": "assistant", "content": [{"type": "tool_use", "name": "Read", "input": {"file_path": "a.py"}}]}}
    recs6 = [g_u, g_a, _u("gB", "2026-06-27T05:05:00Z", "sessionGROW", "trailing")]
    _write(tp6, recs6)
    pcx.handle_precompact(data6)                                   # flush with asst_text == ''
    g_a["message"]["content"].append({"type": "text", "text": "here is the final summary"})   # asst grows
    recs6 += [_a("gBa", "2026-06-27T05:05:30Z", "sessionGROW", "done")]
    _write(tp6, recs6)
    pcx.handle_stop(data6)
    grow = [c for f in md.glob("*.jsonl")
            for c in (json.loads(l) for l in f.read_text(encoding="utf-8").splitlines() if l.strip())
            if c.get("user") == "grow turn prompt"]
    ck(len(grow) == 1, "uuid-less turn captured once even when asst text grows between PreCompact and Stop")

    # --- two uuid-less turns with DISTINCT content get distinct ids (no drop) ---
    tp7 = Path(tmp) / "sessionDUP.jsonl"
    data7 = {"cwd": cwd, "transcript_path": str(tp7), "session_id": "sessionDUP"}
    d1 = _u("d", "2026-06-27T04:00:00Z", "sessionDUP", "first distinct prompt")
    d2 = _u("d", "2026-06-27T04:00:00Z", "sessionDUP", "second distinct prompt")
    del d1["uuid"]
    del d2["uuid"]
    _write(tp7, [d1, _a("da1", "2026-06-27T04:00:01Z", "sessionDUP", "Understood."),
                 d2, _a("da2", "2026-06-27T04:00:02Z", "sessionDUP", "Understood.")])
    pcx.handle_stop(data7)
    dup = [c for f in md.glob("*.jsonl")
           for c in (json.loads(l) for l in f.read_text(encoding="utf-8").splitlines() if l.strip())
           if c.get("sess") == store.sess_token("sessionD") and (c.get("user") or "").endswith("distinct prompt")]
    ck(len(dup) == 2, "two uuid-less turns with distinct content get distinct ids (content-signature)")

    # --- a uuid-less turn re-read at a SHIFTED line position (compaction prepends a
    # summary) is captured ONCE - the synthetic id is content-based, not absolute line position ---
    tpo = Path(tmp) / "sessOLD.jsonl"
    uL = _u("x", "2026-06-27T03:00:00Z", "sessSHIFT", "uuid-less decision")
    del uL["uuid"]
    _write(tpo, [uL, _a("xa", "2026-06-27T03:00:01Z", "sessSHIFT", "ok")])
    pcx.handle_stop({"cwd": cwd, "transcript_path": str(tpo), "session_id": "sessSHIFT"})
    tpn = Path(tmp) / "sessNEW.jsonl"          # resume: a compact-summary line prepended above the turn
    summ = {"type": "user", "isCompactSummary": True, "timestamp": "2026-06-27T03:30:00Z",
            "sessionId": "sessSHIFT", "message": {"role": "user", "content": "summary"}}
    _write(tpn, [summ, uL, _a("xa", "2026-06-27T03:00:01Z", "sessSHIFT", "ok")])
    pcx.handle_stop({"cwd": cwd, "transcript_path": str(tpn), "session_id": "sessSHIFT"})
    shift = [c for f in md.glob("*.jsonl")
             for c in (json.loads(l) for l in f.read_text(encoding="utf-8").splitlines() if l.strip())
             if c.get("user") == "uuid-less decision"]
    ck(len(shift) == 1, "uuid-less turn captured once even when its line position shifts on resume/compaction")

    # --- a non-dict tool input / non-str command must not DROP the turn ---
    tp8 = Path(tmp) / "sessionTOOLIN.jsonl"
    a8 = {"type": "assistant", "uuid": "p1a", "timestamp": "2026-06-27T02:00:01Z", "sessionId": "sessionTOOLIN",
          "message": {"role": "assistant", "content": [{"type": "tool_use", "name": "Bash", "input": {"command": ["git", "status"]}}]}}
    _write(tp8, [_u("p1", "2026-06-27T02:00:00Z", "sessionTOOLIN", "non-dict-input turn"), a8])
    pcx.handle_stop({"cwd": cwd, "transcript_path": str(tp8), "session_id": "sessionTOOLIN"})
    kept = [c for f in md.glob("*.jsonl")
            for c in (json.loads(l) for l in f.read_text(encoding="utf-8").splitlines() if l.strip())
            if c.get("user") == "non-dict-input turn"]
    ck(len(kept) == 1, "non-dict tool input / non-str command does not drop the turn")

    # --- a uuid-less turn whose tool files/cmds REORDER between flushes is captured
    # once - the content signature excludes files/cmds (streaming-order-derived, not invariant) ---
    tp9 = Path(tmp) / "sessREORD.jsonl"
    ru = _u("r", "2026-06-27T01:00:00Z", "sessREORD", "reorder turn")
    del ru["uuid"]
    ra1 = {"type": "assistant", "uuid": "ra", "timestamp": "2026-06-27T01:00:01Z", "sessionId": "sessREORD",
           "message": {"role": "assistant", "content": [
               {"type": "tool_use", "name": "Read", "input": {"file_path": "store.py"}},
               {"type": "tool_use", "name": "Edit", "input": {"file_path": "calibrate.py"}}]}}
    _write(tp9, [ru, ra1])
    pcx.handle_stop({"cwd": cwd, "transcript_path": str(tp9), "session_id": "sessREORD"})
    tp9b = Path(tmp) / "sessREORDb.jsonl"      # SAME turn, tool blocks SWAPPED, new file -> full re-scan
    ra2 = {"type": "assistant", "uuid": "ra", "timestamp": "2026-06-27T01:00:01Z", "sessionId": "sessREORD",
           "message": {"role": "assistant", "content": [
               {"type": "tool_use", "name": "Edit", "input": {"file_path": "calibrate.py"}},
               {"type": "tool_use", "name": "Read", "input": {"file_path": "store.py"}}]}}
    _write(tp9b, [ru, ra2])
    pcx.handle_stop({"cwd": cwd, "transcript_path": str(tp9b), "session_id": "sessREORD"})
    reord = [c for f in md.glob("*.jsonl")
             for c in (json.loads(l) for l in f.read_text(encoding="utf-8").splitlines() if l.strip())
             if c.get("user") == "reorder turn"]
    ck(len(reord) == 1, "uuid-less turn with reordered tool files/cmds captured once (content-sig excludes them)")

    shutil.rmtree(tmp, ignore_errors=True)
    if fails:
        print(f"\nFAIL - {len(fails)} check(s) failed")
        return 1
    print("\nOK - all engine checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(run())
