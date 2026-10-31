#!/usr/bin/env python3
"""Metadata scrub-fence (W2) - a secret or refusal term must NEVER ride a reserved metadata key
(ts, sess, chan, id, at, ...) onto disk unscrubbed.

The store spares reserved-key SCALARS from the text-scrub so a refusal term can't rewrite an id and
break dedup. That is only safe if the value really IS metadata: a hostile transcript can put a secret
in `timestamp`/`sessionId`, and until October 2026 the mirror path skipped the gateway's content scrub
by flag (it scrubbed before the cut and declared the record clean). These tests drive the capture path
(the exact thing GPT 5.6's review attacked) and the gateway with hostile inputs, and prove:
  - a secret/refusal in a reserved key never reaches disk (source canonicalization + gateway validation),
  - genuine metadata (a real ISO ts, a uuid id, a version spec) round-trips verbatim (dedup intact),
  - the validator has teeth (it transforms hostile input, and it is not a scrub-everything hammer).
"""
import json
import os
import sys
import tempfile
from pathlib import Path

# set BEFORE the store import so a first-import can never bind to the real HOME store.
os.environ.setdefault("PARSICA_WEAVE_ROOT", tempfile.mkdtemp(prefix="w2_fence_boot_"))
os.environ.pop("PARSICA_CHANNEL", None)   # untagged capture: keep chan out of the picture (W3 owns it)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store   # noqa: E402
import pcx     # noqa: E402

CWD = os.path.join(tempfile.gettempdir(), "w2_fence_proj")

REFUSAL = "ZZREFUSALSENTINELZZ"       # a configured refusal term -> the content-scrub target
SECRET = "AKIAZZ00SENTINEL0011"       # AWS-access-key SHAPE: a slug-shaped secret -> the secrets-scrub target


def _fresh():
    tmp = tempfile.mkdtemp(prefix="w2_fence_root_")
    store.STORE_ROOT = Path(tmp)
    store.GLOBAL_CONFIG = store.STORE_ROOT / "config.json"
    store.ensure_workspace(CWD)
    cfg = store.load_config(CWD)
    cfg["refusal"] = [REFUSAL]
    cfg["scrub_pii"] = True
    return cfg


def _mirror_blob():
    md = store.mirror_dir(CWD)
    return "\n".join(p.read_text(encoding="utf-8") for p in md.glob("*.jsonl"))


def _mirror_rows():
    md = store.mirror_dir(CWD)
    rows = []
    for p in md.glob("*.jsonl"):
        for ln in p.read_text(encoding="utf-8").splitlines():
            if ln.strip():
                rows.append(json.loads(ln))
    return rows


def _write_transcript(records):
    fd, tpath = tempfile.mkstemp(prefix="w2_tx_", suffix=".jsonl")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")
    return tpath


# --- end-to-end: the hostile transcript ------------------------------------------------------------
def test_hostile_transcript_leaks_nothing():
    cfg = _fresh()
    records = [
        {"type": "user",
         "timestamp": "2026-13-99-" + REFUSAL,        # invalid ts carrying a refusal term -> canon to ""
         "sessionId": "a/b/c/d/",                      # hostile session (slashes) -> neutralized to s-<hash>
         "uuid": SECRET,                               # slug-shaped secret in the id field
         "message": {"content": "hello world benign prompt"}},
        {"type": "assistant",
         "timestamp": "2026-07-12T08:36:01Z", "sessionId": "a/b/c/d/", "uuid": "asst-1",
         "message": {"content": [
             {"type": "text", "text": "working on " + REFUSAL + " now"},          # refusal in asst content
             {"type": "tool_use", "name": "Tool" + REFUSAL,                       # refusal in a tool NAME
              "input": {"file_path": "/x/" + REFUSAL + "/f.py"}},                 # refusal in a PATH
         ]}},
    ]
    tpath = _write_transcript(records)
    pcx._flush_mirror(CWD, tpath, "sesskey", cfg, {"cursors": {}, "sizes": {}})

    blob = _mirror_blob()
    assert blob, "capture wrote nothing - the transcript should have produced one chunk"
    assert "hello world benign prompt" in blob            # capture still works (nothing over-neutralized)
    assert REFUSAL not in blob                             # ts, asst text, tool name and path all cleaned
    assert SECRET not in blob                              # slug-shaped secret in `uuid` stripped at the gateway
    assert "a/b/c/d" not in blob                           # hostile sessionId neutralized

    rows = _mirror_rows()
    assert len(rows) == 1
    r = rows[0]
    assert r.get("ts", "") == ""                           # invalid ts canonicalized away (carried a payload)
    assert r.get("sess", "").startswith("s-")              # hostile sessionId -> deterministic safe tag
    assert SECRET not in json.dumps(r)                     # the id field carries no secret


def test_valid_metadata_survives_capture():
    """The other half: a benign turn's REAL metadata round-trips verbatim, so the fence is a validator,
    not a shredder - dedup, session filtering and sort all still work."""
    cfg = _fresh()
    records = [
        {"type": "user", "timestamp": "2026-07-12T09:00:00Z", "sessionId": "a91c6844",
         "uuid": "u-valid-0001", "message": {"content": "ordinary question about the code"}},
        {"type": "assistant", "timestamp": "2026-07-12T09:00:02Z", "sessionId": "a91c6844",
         "uuid": "asst-2", "message": {"content": [{"type": "text", "text": "a plain answer"}]}},
    ]
    tpath = _write_transcript(records)
    pcx._flush_mirror(CWD, tpath, "sesskey", cfg, {"cursors": {}, "sizes": {}})
    rows = _mirror_rows()
    assert len(rows) == 1
    r = rows[0]
    assert r["ts"] == "2026-07-12T09:00:00.000Z"           # parsed + canonicalized to UTC (milliseconds)
    assert r["sess"] == store.sess_token("a91c6844")       # opaque session token (deterministic)
    assert r["id"] == pcx._safe_chunk_id("u-valid-0001", "")   # opaque chunk id (deterministic, dedup intact)
    assert "ordinary question about the code" in r.get("user", "")


# --- gateway unit tests ----------------------------------------------------------------------------
def test_gateway_has_no_bypass_and_scrubs_content_and_reserved_keys_alike():
    """Until October 2026 content_fields_scrubbed=True (the mirror fast path) skipped the content scrub
    and only validated metadata (REVIEW-2026-10.md, follow-up, class (a), RF 10). The parameter is gone:
    every record takes the one path, where content is scrubbed and reserved keys are validated."""
    import inspect
    assert "content_fields_scrubbed" not in inspect.signature(store.append_record).parameters
    cfg = _fresh()
    shard = store.mirror_dir(CWD) / "2026-07-03.9.jsonl"
    rec = {"id": SECRET,                                   # slug-shaped secret -> secrets-scrub strips it
           "ts": "not-a-timestamp " + REFUSAL,             # non-ISO + refusal -> full scrub
           "at": "also " + REFUSAL + " bad",               # non-ISO -> full scrub
           "user": "content the caller never scrubbed: " + SECRET + " and " + REFUSAL}
    store.append_record(shard, rec, cfg)
    text = shard.read_text(encoding="utf-8")
    assert SECRET not in text
    assert REFUSAL not in text
    assert "content the caller never scrubbed" in text     # the clean part of the content is kept


def test_gateway_full_path_validates_reserved_keys():
    """The non-fast path (_scrub_record) validates reserved keys too - both doors agree."""
    cfg = _fresh()
    shard = store.mirror_dir(CWD) / "2026-07-03.8.jsonl"
    store.append_record(shard, {"id": SECRET, "ts": REFUSAL + " x", "user": "hi " + REFUSAL}, cfg)
    text = shard.read_text(encoding="utf-8")
    assert SECRET not in text and REFUSAL not in text


def test_gateway_spares_genuine_metadata():
    """Teeth (the other direction): genuine metadata is spared verbatim, so a refusal term can never
    rewrite an id and break dedup or a conviction join."""
    cfg = _fresh()
    shard = store.mirror_dir(CWD) / "2026-07-03.7.jsonl"
    rec = {"id": "cid-" + "a" * 20, "sess": "s-" + "b" * 16, "ts": "2026-07-12T08:36:00Z",
           "at": "2026-07-12T08:36:00Z", "spec": "ctx-enrich/v2", "weight": 1.5, "run_id": "deadbeef00"}
    store.append_record(shard, rec, cfg)
    stored = json.loads(shard.read_text(encoding="utf-8").splitlines()[0])
    for k, v in rec.items():
        assert stored[k] == v, "genuine metadata key %s was altered: %r != %r" % (k, stored[k], v)


def test_clean_reserved_scalar_predicate():
    """Direct teeth on the validator: it transforms hostile input and spares genuine metadata."""
    cfg = _fresh()
    c = store._clean_reserved_scalar
    # genuine INTERNAL tokens -> spared verbatim (so a refusal term can never rewrite an id / break dedup)
    assert c("ts", "2026-07-12T08:36:00Z", cfg) == "2026-07-12T08:36:00Z"
    assert c("id", "cid-" + "a" * 20, cfg) == "cid-" + "a" * 20
    assert c("sess", "s-" + "b" * 16, cfg) == "s-" + "b" * 16
    assert c("sess", "", cfg) == ""                                  # an untagged session stays empty
    assert c("spec", "ctx-enrich/v2", cfg) == "ctx-enrich/v2"
    assert c("weight", 1.5, cfg) == 1.5 and c("red", 3, cfg) == 3    # numerics spared
    # GPT B1: a value that is NOT one of our tokens is no longer spared - a refusal term or a plain
    # credential under a reserved key fails the tight schema and gets the full scrub:
    assert REFUSAL not in c("id", REFUSAL, cfg)                      # a slug-shaped refusal term is fenced now
    assert REFUSAL not in c("sess", REFUSAL, cfg)                    # ...under sess too (no longer spared)
    assert SECRET not in c("id", SECRET, cfg)                        # AWS-key-shaped value fenced
    # timestamp is canonicalized at the SOURCE (_canon_ts parses, not just pattern-matches):
    assert pcx._canon_ts("2026-13-99T99:99:99Z") == ""              # impossible date rejected
    assert pcx._canon_ts("2026-07-12 09:00:00") == ""               # timezone-naive rejected (not assumed UTC)
    assert pcx._canon_ts("2026-07-12T09:00:00+05:30") == "2026-07-12T03:30:00.000Z"   # non-UTC -> UTC


def test_build_chunk_neutralizes_hostile_metadata():
    """Source-layer teeth: _build_chunk canonicalizes ts and neutralizes a hostile session id."""
    cfg = _fresh()
    turn = {"prompt": {"type": "user", "timestamp": "13:99 " + REFUSAL, "sessionId": "../../etc",
                       "uuid": "u1", "message": {"content": "hi"}},
            "recs": [{"type": "user", "timestamp": "13:99 " + REFUSAL, "sessionId": "../../etc",
                      "uuid": "u1", "message": {"content": "hi"}}]}
    chunk = pcx._build_chunk(turn, cfg)
    assert chunk["ts"] == ""                                # invalid ts -> ""
    assert chunk["sess"].startswith("s-")                   # hostile sessionId -> deterministic safe tag
    assert REFUSAL not in json.dumps(chunk)


def test_gpt_b1_findings_end_to_end():
    """The three GPT B1 findings, closed at the SOURCE by opaque-ing so they can NEVER reach disk:
    a refusal term / credential in sessionId or uuid becomes an opaque token; a boundary-evaded
    credential in uuid is hashed away; a non-UTC timestamp is converted to UTC (never stored raw)."""
    cfg = _fresh()
    boundary_secret = "x" + SECRET + "x"    # GPT: a credential wrapped in alnum evades the scrubber boundary
    records = [
        {"type": "user",
         "timestamp": "2026-07-12T09:00:00+05:30",     # non-UTC offset -> must be converted to UTC
         "sessionId": REFUSAL,                          # a refusal term as the session id -> opaque token
         "uuid": boundary_secret,                       # boundary-evaded credential as the uuid -> hashed away
         "message": {"content": "a benign prompt body"}},
        {"type": "assistant", "timestamp": "2026-07-12T09:00:01Z", "sessionId": REFUSAL, "uuid": "asst-1",
         "message": {"content": [{"type": "text", "text": "reply"}]}},
    ]
    tpath = _write_transcript(records)
    pcx._flush_mirror(CWD, tpath, "sesskey", cfg, {"cursors": {}, "sizes": {}})
    blob = _mirror_blob()
    rows = _mirror_rows()
    assert rows and "a benign prompt body" in blob            # captured
    assert REFUSAL not in blob                                 # refusal-term sessionId never reaches disk
    assert SECRET not in blob and boundary_secret not in blob  # boundary-evaded credential hashed away
    r = rows[0]
    assert r["sess"].startswith("s-") and r["id"].startswith("cid-")   # opaque tokens, not the raw values
    assert r["ts"] == "2026-07-12T03:30:00.000Z"              # 09:00+05:30 stored in UTC, never the raw string


def test_rc4_w2_output_and_reserved_key_fences():
    """RC4/GPT W2: the output fence is one door and reserved IDENTIFIERS fence the WHOLE workspace.
      * a slug-shaped refusal term under `target` (the reserved-key bypass) is redacted at the store
        door via append_record, but a legitimate hex id round-trips (no over-redaction of joins);
      * calibrate refuses a fenced --target at record time (never writes a redacted ledger key);
      * store.emit_json fences dict KEYS and VALUES before serializing;
      * a term refused in project A is fenced in project B's enrich pending output (cross-project)."""
    import calibrate  # noqa: E402
    import enrich     # noqa: E402
    import scrub      # noqa: E402

    root = Path(tempfile.mkdtemp(prefix="w2_rc4_"))
    store.STORE_ROOT = root
    store.GLOBAL_CONFIG = root / "config.json"
    TERM = "ZZWORKSPACETERMZZ"
    (root / "projects").mkdir(parents=True, exist_ok=True)
    (root / "config.json").write_text(json.dumps({"refusal": [TERM]}), encoding="utf-8")   # global term

    A = os.path.join(tempfile.gettempdir(), "w2rc4_A")
    B = os.path.join(tempfile.gettempdir(), "w2rc4_B")
    for p in (A, B):
        store.ensure_workspace(p)
    TERM_A = "ZZPROJECTATERMZZ"
    (store.project_dir(A) / "config.json").write_text(json.dumps({"refusal": [TERM_A]}), encoding="utf-8")

    # (1) store-door: a fenced term under the reserved `target` key is redacted on disk...
    shard = store.calibration_dir(A) / "door.jsonl"
    store.append_record(shard, {"target": TERM, "event": "confirmed", "weight": 1.0}, store.load_config(A))
    disk = shard.read_text(encoding="utf-8")
    assert TERM not in disk, "reserved-key store-door fence failed: fenced target on disk"
    # ...while a legitimate hex did under `target` round-trips (joins must not be corrupted)
    good_did = "deadbeef0123"
    shard2 = store.calibration_dir(A) / "good.jsonl"
    store.append_record(shard2, {"target": good_did, "event": "confirmed", "weight": 1.0}, store.load_config(A))
    assert good_did in shard2.read_text(encoding="utf-8"), "over-redacted a legitimate reserved id"

    # (2) calibrate refuse-at-record: a fenced --target never writes an event
    assert calibrate.record(A, TERM, "confirmed") is False, "refuse-at-record failed"
    assert calibrate.record(A, good_did, "confirmed") is True, "refuse-at-record rejected a valid target"

    # (3) emit_json fences KEYS and VALUES
    import io
    buf = io.StringIO()
    store.emit_json({TERM: 1, "s": TERM}, stream=buf)
    assert TERM not in buf.getvalue(), "emit_json leaked a fenced key/value"

    # (4) cross-project: A's term fenced in B's enrich pending output
    store.append_record(store.mirror_dir(B) / "2026-07-14.jsonl",
                        {"id": "cid-" + "cd" * 10, "user": "the " + TERM_A + " notes",
                         "asst": "re: " + TERM_A, "ts": "2026-07-14T00:00:00Z"},
                        store.load_config(B))
    assert TERM_A not in json.dumps(enrich.list_pending(B)), \
        "cross-project refusal term leaked in enrich pending output"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
    print("all metadata-fence tests passed")
