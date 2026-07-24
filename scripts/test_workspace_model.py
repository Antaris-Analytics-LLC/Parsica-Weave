#!/usr/bin/env python3
"""Tests for the WORKSPACE model (the locked 2026-07-10 contract). Run: python test_workspace_model.py

One agent across every project and session: provenance stamps on every shared-doc append,
turn accounting PER SESSION (independent cadence + independent checkpoint consumption),
retention by ROTATION (move to archive/, file count conserved, never unlink), git-root
project keying (subdir cwd == root key; worktree .git-file), the workspace-level channel
registry, cross-project sync, and query's --workspace union + --archive reads."""
import io
import os
import re
import sys
import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _ts(hours_ago=0.0):
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)
            ).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def run():
    tmp = tempfile.mkdtemp()
    os.environ["PARSICA_WEAVE_ROOT"] = tmp
    import store
    import pcx
    import query
    fails = []

    def ck(c, m):
        print(("ok   " if c else "FAIL ") + m)
        if not c:
            fails.append(m)

    def _count():
        return sum(1 for p in Path(tmp).rglob("*") if p.is_file())

    def _note(cwd, kind, text, sess="-"):
        old = sys.stdin
        sys.stdin = type("S", (), {"buffer": io.BytesIO(text.encode("utf-8"))})()
        try:
            pcx.handle_note(["note", "--kind", kind, "--cwd", cwd, "--sess", sess])
        finally:
            sys.stdin = old

    def _cap(fn, *a):
        old = sys.stdout
        sys.stdout = io.StringIO()
        try:
            fn(*a)
            return sys.stdout.getvalue()
        finally:
            sys.stdout = old

    # --- provenance stamps: the exact line every shared-doc append carries (law #2) ------------
    s = store.entry_stamp("sessAAAA1111", "proj-abc")
    ck(re.fullmatch(r"\*\(\d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC · session sessAAAA · proj-abc\)\*", s)
       is not None, "entry_stamp: full markdown-italic provenance line, sess8 + project key")
    ck("session -" in store.entry_stamp(None, "k") and "session -" in store.entry_stamp("", "k"),
       "entry_stamp: unknown session stamps as '-'")

    cwd = tmp + "/proj"
    os.makedirs(cwd, exist_ok=True)
    key = store.project_key(cwd)
    _note(cwd, "lesson", "## 2026-07-10 - a scar\nthe lesson body\n", sess="sessAAAA1111")
    lessons = store.lessons_path().read_text(encoding="utf-8")
    last = [ln for ln in lessons.splitlines() if ln.strip()][-1]
    ck(store.lessons_path() == store.STORE_ROOT / "lessons.md",
       "lessons.md is WORKSPACE-level (one scars doc for every project)")
    ck("the lesson body" in lessons, "note --kind lesson appends the body")
    ck(last.startswith("*(") and "session sessAAAA" in last and key in last,
       "lesson append carries the provenance stamp (sess8 + project key)")

    _note(cwd, "journal", "## day\nthe journal body\n", sess="sessAAAA1111")
    jf = store.journal_dir() / (datetime.now(timezone.utc).date().isoformat() + ".md")
    jtext = jf.read_text(encoding="utf-8")
    jlast = [ln for ln in jtext.splitlines() if ln.strip()][-1]
    ck(store.journal_dir() == store.STORE_ROOT / "journal",
       "journal/ is WORKSPACE-level")
    ck("the journal body" in jtext and jlast.startswith("*(") and "session sessAAAA" in jlast,
       "journal append carries the provenance stamp")

    _note(cwd, "working", "## Pinned facts\n- fact one\n## Open loops\n- loop one\n")
    wtext = store.working_path(cwd).read_text(encoding="utf-8")
    ck(store.working_path(cwd) == store.STORE_ROOT / "working" / f"{key} - Working Set.md",
       "working set lives at working/<key> - Working Set.md")
    ck("fact one" in wtext and "*(" not in wtext,
       "working set replaced in full, positionally attributed (no stamp)")

    # --- per-session turn accounting: two sessions, ONE project, independent cadences ----------
    tp = tmp + "/turns_proj"
    os.makedirs(tp, exist_ok=True)
    store.ensure_workspace(tp)
    with open(store.project_dir(tp) / "config.json", "w", encoding="utf-8") as fh:
        json.dump({"checkpoint_every_n": 2}, fh)
    for _ in range(2):
        pcx.handle_stop({"cwd": tp, "transcript_path": None, "session_id": "sessA"})
    pcx.handle_stop({"cwd": tp, "transcript_path": None, "session_id": "sessB"})
    st = store.load_state(tp)
    ck(st["turns"] == {"sessA": 2, "sessB": 1},
       "each session counts its own turns (got %r)" % st["turns"])
    ck(st["checkpoint_due"] == {"sessA": 2},
       "cadence armed for session A only (got %r)" % st["checkpoint_due"])

    outB = _cap(pcx.handle_prompt, {"cwd": tp, "session_id": "sessB"})
    ck("checkpoint" not in outB.lower(), "session B's prompt sees NO checkpoint (not its due)")
    ck(store.load_state(tp)["checkpoint_due"] == {"sessA": 2},
       "session B's prompt never consumes session A's due (no cross-session consumption)")
    outA = _cap(pcx.handle_prompt, {"cwd": tp, "session_id": "sessA"})
    ck("checkpoint" in outA.lower() and "this session" in outA,
       "session A's prompt fires ITS checkpoint directive")
    ck("--sess sessA" in outA, "the directive embeds --sess so the notes stamp provenance")
    ck(store.load_state(tp)["checkpoint_due"] == {},
       "session A's due consumed exactly once")
    pcx.handle_stop({"cwd": tp, "transcript_path": None, "session_id": "sessB"})
    ck(store.load_state(tp)["checkpoint_due"] == {"sessB": 2},
       "session B hits its own cadence independently")
    ck("checkpoint" not in _cap(pcx.handle_prompt, {"cwd": tp, "session_id": "sessA"}).lower(),
       "session A stays silent while B's due is armed")
    outB2 = _cap(pcx.handle_prompt, {"cwd": tp, "session_id": "sessB"})
    ck("checkpoint" in outB2.lower() and "--sess sessB" in outB2,
       "session B consumes its own due with its own --sess")

    # a legacy scalar turn_count on disk is discarded from the loaded view (dict coercion)
    (store.project_dir(tp) / ".state.json").write_text(
        json.dumps({"cursors": {}, "turns": 7, "checkpoint_due": 3}), encoding="utf-8")
    st2 = store.load_state(tp)
    ck(st2["turns"] == {} and st2["checkpoint_due"] == {},
       "legacy scalar turn state is coerced to the per-session dict shape")

    # --- rotation: retention WITHOUT deletion (file count conserved, moves to archive/) --------
    rp = tmp + "/rot_proj"
    os.makedirs(rp, exist_ok=True)
    store.ensure_workspace(rp)
    md, ed = store.mirror_dir(rp), store.enrich_dir(rp)
    for d in ("2026-01-01", "2026-01-02", "2026-01-03"):
        (md / f"{d}.111.jsonl").write_text(json.dumps({"id": "m" + d, "ts": d + "T10:00:00Z"}) + "\n",
                                           encoding="utf-8")
        (ed / f"{d}.111.jsonl").write_text(json.dumps({"id": "m" + d, "enrich": {"spec": "ctx-enrich/v2"}}) + "\n",
                                           encoding="utf-8")
    n_before = _count()
    moved = store.rotate_mirror(rp, 1)
    ck(moved == 4, "rotate_mirror moves the 2 old dates' mirror + overlay shards (got %d)" % moved)
    ck(sorted(p.name for p in md.glob("*.jsonl")) == ["2026-01-03.111.jsonl"],
       "only the newest active date stays in the active mirror")
    ck(sorted(p.name for p in (md / "archive").glob("*.jsonl"))
       == ["2026-01-01.111.jsonl", "2026-01-02.111.jsonl"],
       "rotated mirror shards land in mirror/<key>/archive/")
    ck(sorted(p.name for p in (ed / "archive").glob("*.jsonl"))
       == ["2026-01-01.111.jsonl", "2026-01-02.111.jsonl"],
       "their enrich overlays follow into enrich/archive/")
    ck(_count() == n_before, "mirror rotation conserves the store's file count (nothing deleted)")

    jd = store.journal_dir()
    for d in ("2026-01-01", "2026-01-02", "2026-01-03"):
        (jd / f"{d}.md").write_text("## old page %s\nbody\n" % d, encoding="utf-8")
    n_before = _count()
    jmoved = store.rotate_journal(1)
    ck(jmoved >= 2 and (jd / "archive" / "2026-01-01.md").exists()
       and (jd / "archive" / "2026-01-02.md").exists(),
       "journal rotation moves old pages to journal/archive/ (moved %d)" % jmoved)
    ck(not (jd / "2026-01-01.md").exists() and _count() == n_before,
       "journal rotation conserves the file count (nothing deleted)")

    # --- git-root keying: any cwd inside a repo keys to the SAME project -----------------------
    repo = Path(tmp) / "repo"
    (repo / ".git").mkdir(parents=True)
    sub = repo / "src" / "deep"
    sub.mkdir(parents=True)
    ck(store.project_key(str(sub)) == store.project_key(str(repo)),
       "a subdirectory cwd keys to the repo root (subdirectory-silo bug dead)")
    wt = Path(tmp) / "wtree"
    wt.mkdir()
    (wt / ".git").write_text("gitdir: ../repo/.git/worktrees/wt\n", encoding="utf-8")
    wsub = wt / "inner"
    wsub.mkdir()
    ck(store.project_key(str(wsub)) == store.project_key(str(wt)),
       "a worktree's .git FILE anchors the key like a .git dir")
    ck(store.project_key(str(wt)) != store.project_key(str(repo)),
       "distinct roots still key distinctly")
    lone = Path(tmp) / "lone"
    lone.mkdir()
    ck(store.project_key(str(lone)).startswith("lone-"),
       "no repo -> the cwd itself anchors the key (fallback)")

    # --- channels: ONE workspace registry, no per-project copies -------------------------------
    cid = store.channel_id_for("lane")
    ck(store.channels_path() == store.STORE_ROOT / "channels.json" and store.channels_path().exists(),
       "channel registry is one workspace file at the store root")
    ck(store.channel_name(cid) == "lane" and store.channel_id_for("lane") == cid,
       "the same lane id resolves everywhere (no cwd in the registry API)")
    ck(not list(Path(tmp).glob("projects/*/channels.json")),
       "no per-project channels.json exists anywhere")

    # --- sync crosses projects: one digest over the whole workspace mirror ---------------------
    pa, pb = tmp + "/sync_a", tmp + "/sync_b"
    for p in (pa, pb):
        os.makedirs(p, exist_ok=True)
        store.ensure_workspace(p)
    now_day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    tsa = _ts(1)
    (store.mirror_dir(pa) / f"{now_day}.1.jsonl").write_text(
        json.dumps({"id": "sa", "ts": tsa, "sess": "sessAAAA", "user": "alpha project work"}) + "\n",
        encoding="utf-8")
    (store.mirror_dir(pb) / f"{now_day}.1.jsonl").write_text(
        json.dumps({"id": "sb", "ts": _ts(1), "sess": "sessBBBB", "user": "beta project work"}) + "\n",
        encoding="utf-8")
    sout = _cap(pcx.handle_sync, ["sync", "--hours", "12"])
    ck("sync: last 12h across the workspace" in sout, "sync header spans the workspace")
    ck(store.local_zone_note() in sout,
       "sync header carries the local-zone note (once per surface, not per row)")
    ck("alpha project work" in sout and "beta project work" in sout,
       "sync digests chunks from BOTH projects")
    ck(("- %s [%s · sessAAAA]" % (store.local_stamp(tsa), store.project_key(pa))) in sout,
       "sync rows are labeled [<project-key> · <sess>] and render LOCAL time")

    # --- query: --workspace union (deduped) + --archive reads ----------------------------------
    wa = query._load_chunks(workspace=True)
    ids = {c.get("id") for c in wa}
    ck({"sa", "sb"} <= ids, "--workspace unions chunks across every project dir")
    # dedup by id: the same chunk id in two projects appears once in the union
    (store.mirror_dir(pb) / f"{now_day}.2.jsonl").write_text(
        json.dumps({"id": "sa", "ts": _ts(1), "sess": "sessBBBB", "user": "dup of sa"}) + "\n",
        encoding="utf-8")
    ck(sum(1 for c in query._load_chunks(workspace=True) if c.get("id") == "sa") == 1,
       "--workspace dedupes by chunk id")
    # archive reads: a rotated shard is invisible by default, visible with archive=True
    arc = store.mirror_archive_dir(pa)
    arc.mkdir(parents=True, exist_ok=True)
    (arc / "2025-12-01.9.jsonl").write_text(
        json.dumps({"id": "old-arc", "ts": "2025-12-01T10:00:00Z", "sess": "s", "user": "archived turn"}) + "\n",
        encoding="utf-8")
    ck("old-arc" not in {c.get("id") for c in query._load_chunks(cwd=pa)},
       "an archived shard stays out of a default query")
    ck("old-arc" in {c.get("id") for c in query._load_chunks(cwd=pa, archive=True)},
       "--archive folds the rotated shards back into the read")
    ck("old-arc" in {c.get("id") for c in query._load_chunks(workspace=True, archive=True)},
       "--workspace --archive unions the archives too")

    # --- query: --recent (cross-session recency retrieval) -------------------------------------
    # semantics = the engine's search_by_recency at the workspace plane: top-n inside the
    # window, newest first, own session excluded unless asked for
    rc_rows = [
        {"id": "rc-new-other", "ts": _ts(1),   "sess": "otherSSS", "user": "birthday is december 19"},
        {"id": "rc-new-mine",  "ts": _ts(2),   "sess": store.sess_token("mineSSSS"), "user": "my own fresh turn"},
        {"id": "rc-old",       "ts": _ts(48),  "sess": "otherSSS", "user": "stale beyond the window"},
        {"id": "rc-bad-ts",    "ts": "garbage", "sess": "x",       "user": "unparseable ts skipped"},
        {"id": "rc-naive",     "ts": _ts(3)[:19], "sess": "naiveSSS", "user": "naive ts treated as UTC"},
    ]
    got = query.recent_chunks(rc_rows, hours=12.0, limit=5, exclude_sess="mineSSSS")
    ids = [c["id"] for c in got]
    ck(ids == ["rc-new-other", "rc-naive"],
       "--recent: window + own-session exclusion + newest-first order (got %s)" % ids)
    ck("rc-old" not in ids and "rc-bad-ts" not in ids,
       "--recent: outside-window and unparseable-ts chunks are skipped")
    got_all = query.recent_chunks(rc_rows, hours=12.0, limit=5, exclude_sess=None)
    ck([c["id"] for c in got_all] == ["rc-new-other", "rc-new-mine", "rc-naive"],
       "--recent --all-sessions keeps the caller's own session")
    ck([c["id"] for c in query.recent_chunks(rc_rows, hours=12.0, limit=1)] == ["rc-new-other"],
       "--recent respects the limit (top-n, not all-in-window)")

    # --- display is LOCAL; storage stays UTC (the timezone-display contract) -------------------
    # expected strings via the SAME helpers - never a hardcoded zone offset (must pass anywhere)
    ts_disp = _ts(2)
    head = query._fmt({"id": "d1", "ts": ts_disp, "sess": "sessDISP"}, False).splitlines()[0]
    ck(head == "[%s · sessDISP]" % store.local_stamp(ts_disp),
       "query chunk head renders LOCAL time via the shared helper")
    ck("UTC" not in head, "the misleading UTC label is gone from the chunk head")
    ck(store.local_zone_note() == "local time",
       "local_zone_note is a clean 'local time' label, no redundant UTC offset")
    ck(store.local_stamp("2026-07-11T11:03:00") == store.local_stamp("2026-07-11T11:03:00Z"),
       "a naive stored ts is treated as UTC before conversion (the storage contract)")
    ck(store.local_stamp("garbage") == "garbage" and store.local_hm(None) == "",
       "unparseable/absent ts renders verbatim, never crashes a display surface")

    print("\nOK - workspace model checks passed" if not fails
          else "\nRED: %d failing -> %s" % (len(fails), " | ".join(fails)))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(run())
