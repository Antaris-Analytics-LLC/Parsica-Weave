#!/usr/bin/env python3
"""The mirror seen-id index: Stop/PreCompact dedup reads STATE, not shards.

Pins the 2026-07-30 fix for the Stop-hook capture death (option (a) with (b) folded in,
Jake's call): the dedup pre-scan used to enumerate the mirror directory ~16x per Stop and
full-parse every active shard - measured 3.4s at 420 shards and past the 10s hook timeout
at ~1,400, after which capture died permanently and silently. Now `mirror_seen` in
.state.json carries {date: [chunk ids]}; the warm path does ZERO directory enumerations,
the cold path exactly ONE (and seeds the index), and every degradation fails OPEN - a
duplicate row at worst, never lost capture.

Every test drives the real _flush_mirror/save_state pair and counts enumerations by
wrapping store.safe_mirror_shard_files - asserting on the mechanism, not the docstring.
"""
import io
import json
import os
import sys
import tempfile

import pytest

os.environ.setdefault("PARSICA_WEAVE_ROOT", tempfile.mkdtemp(prefix="mirroridx_root_"))
SCRIPTS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPTS)
import pcx    # noqa: E402
import store  # noqa: E402

DATE = "2026-07-30"


def _transcript(cwd, n_turns, start=0, name="t.jsonl"):
    recs = []
    for i in range(start, start + n_turns):
        recs.append({"type": "user", "uuid": "u-%04d" % i,
                     "timestamp": DATE + "T10:%02d:00Z" % (i % 60), "sessionId": "sessA",
                     "message": {"role": "user", "content": "turn %d body" % i}})
        recs.append({"type": "assistant", "uuid": "a-%04d" % i,
                     "timestamp": DATE + "T10:%02d:01Z" % (i % 60), "sessionId": "sessA",
                     "message": {"role": "assistant", "content": "reply %d body" % i}})
    tpath = os.path.join(cwd, name)
    with io.open(tpath, "w", encoding="utf-8") as fh:
        fh.write("\n".join(json.dumps(r) for r in recs) + "\n")
    return tpath


def _project(tag):
    cwd = tempfile.mkdtemp(prefix="mirroridx_%s_" % tag)
    store.ensure_workspace(cwd)
    return cwd


def _rows(md):
    out = []
    for f in sorted(md.glob("*.jsonl")):
        for ln in f.read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if ln:
                out.append(json.loads(ln))
    return out


class _EnumCounter:
    def __init__(self, monkeypatch):
        self.count = 0
        real = store.safe_mirror_shard_files

        def counted(md, root=None):
            self.count += 1
            return real(md, root) if root is not None else real(md)
        monkeypatch.setattr(store, "safe_mirror_shard_files", counted)


def _flush(cwd, tpath, state, **kw):
    cfg = store.load_config(cwd)
    return pcx._flush_mirror(cwd, tpath, "tp:" + os.path.basename(tpath), cfg, state, **kw)


def test_cold_index_scans_exactly_once_and_seeds(monkeypatch):
    cwd = _project("cold")
    tpath = _transcript(cwd, 3)
    state = store.load_state(cwd)
    counter = _EnumCounter(monkeypatch)
    _flush(cwd, tpath, state)
    assert counter.count == 1, \
        "cold path must enumerate the mirror exactly ONCE, measured %d" % counter.count
    store.save_state(cwd, state)
    idx = store.load_state(cwd)["mirror_seen"]
    assert idx.get(DATE), "the cold scan must seed mirror_seen for the next Stop"
    assert len(idx[DATE]) == 3


def test_warm_index_does_zero_enumerations_and_still_appends_fresh_turns(monkeypatch):
    cwd = _project("warm")
    tpath = _transcript(cwd, 3)
    state = store.load_state(cwd)
    _flush(cwd, tpath, state)
    store.save_state(cwd, state)

    tpath2 = _transcript(cwd, 5, name="t2.jsonl")     # 3 known + 2 fresh turns
    state2 = store.load_state(cwd)
    counter = _EnumCounter(monkeypatch)
    _flush(cwd, tpath2, state2)
    assert counter.count == 0, \
        "warm path must not touch the mirror listing at all, measured %d" % counter.count
    md = store.mirror_dir(cwd)
    rows = _rows(md)
    assert len(rows) == 5, "fresh turns must still append (got %d rows)" % len(rows)
    ids = [r["id"] for r in rows]
    assert len(ids) == len(set(ids)), "the index let a duplicate through on the warm path"


def test_cursor_reset_replay_does_not_duplicate(monkeypatch):
    cwd = _project("replay")
    tpath = _transcript(cwd, 4)
    state = store.load_state(cwd)
    _flush(cwd, tpath, state)
    store.save_state(cwd, state)
    before = len(_rows(store.mirror_dir(cwd)))

    fresh = store.load_state(cwd)
    fresh["cursors"] = {}                       # simulated cursor loss: full transcript re-read
    counter = _EnumCounter(monkeypatch)
    _flush(cwd, tpath, fresh)
    assert counter.count == 0
    assert len(_rows(store.mirror_dir(cwd))) == before, \
        "a cursor reset re-appended already-mirrored turns despite the index"


def test_precompact_flush_is_indexed_so_the_next_stop_does_not_duplicate():
    cwd = _project("precompact")
    tpath = _transcript(cwd, 3)
    state = store.load_state(cwd)
    # PreCompact semantics: flush complete turns, do NOT advance the cursor.
    _flush(cwd, tpath, state, hold_trailing=True, advance=False)
    store.save_state(cwd, state)                # handle_precompact saves exactly like this
    flushed = len(_rows(store.mirror_dir(cwd)))
    assert flushed > 0, "PreCompact flushed nothing - fixture broken"

    state2 = store.load_state(cwd)              # next Stop re-reads from the old boundary
    _flush(cwd, tpath, state2)
    assert len(_rows(store.mirror_dir(cwd))) == flushed + 1, \
        "the Stop after a PreCompact must add ONLY the held trailing turn, never re-append"


def test_corrupt_index_degrades_to_one_scan_never_a_crash(monkeypatch):
    cwd = _project("corrupt")
    tpath = _transcript(cwd, 2)
    state = store.load_state(cwd)
    _flush(cwd, tpath, state)
    store.save_state(cwd, state)

    p = store.project_dir(cwd) / ".state.json"
    doc = json.loads(p.read_text(encoding="utf-8"))
    doc["mirror_seen"] = 7                      # hand-corrupted container
    p.write_text(json.dumps(doc), encoding="utf-8")

    state2 = store.load_state(cwd)              # normalize_state coerces to {}
    assert state2["mirror_seen"] == {}
    state2["cursors"] = {}                      # force a real re-read (bypass the size short-circuit)
    counter = _EnumCounter(monkeypatch)
    _flush(cwd, tpath, state2)                  # cold again: one scan, no dupes
    assert counter.count == 1
    rows = _rows(store.mirror_dir(cwd))
    ids = [r["id"] for r in rows]
    assert len(ids) == len(set(ids)) == 2, "corruption fallback duplicated or lost rows"
    store.save_state(cwd, state2)
    assert store.load_state(cwd)["mirror_seen"].get(DATE), "fallback must re-seed the index"


def test_index_caps_are_enforced_in_the_locked_merge(monkeypatch):
    cwd = _project("caps")
    monkeypatch.setattr(store, "MIRROR_SEEN_ID_CAP", 10)
    monkeypatch.setattr(store, "MIRROR_SEEN_DATE_CAP", 4)
    state = store.load_state(cwd)
    state["_mirror_add"] = {"2026-07-%02d" % d: ["id-%d-%d" % (d, i) for i in range(15)]
                            for d in range(1, 7)}      # 6 dates x 15 ids
    store.save_state(cwd, state)
    idx = store.load_state(cwd)["mirror_seen"]
    assert len(idx) == 4, "date cap not enforced: %d dates" % len(idx)
    assert min(idx) == "2026-07-03", "the cap must drop the OLDEST dates first"
    assert all(len(v) == 10 for v in idx.values()), "per-date id cap not enforced"
    assert all(v[-1].endswith("-14") for v in idx.values()), \
        "the id cap must keep the most recently appended ids"


def test_two_windows_union_their_adds_instead_of_clobbering():
    cwd = _project("union")
    s1 = store.load_state(cwd)
    s1["_mirror_add"] = {DATE: ["w1-a", "w1-b"]}
    s2 = store.load_state(cwd)
    s2["_mirror_add"] = {DATE: ["w2-a"]}
    store.save_state(cwd, s1)
    store.save_state(cwd, s2)                   # second writer loaded before the first saved
    idx = store.load_state(cwd)["mirror_seen"]
    assert set(idx[DATE]) == {"w1-a", "w1-b", "w2-a"}, \
        "concurrent windows must UNION mirror ids, not last-writer-wins: %r" % idx[DATE]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
