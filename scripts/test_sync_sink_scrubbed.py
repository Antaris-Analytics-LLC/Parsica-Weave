#!/usr/bin/env python3
"""The sync digest shows no project's raw PII, and the gateway has no bypass.
REVIEW-2026-10.md, follow-up, class (a).

At e480bbd a project could set scrub_pii to false (store.py 86, 755-756) and store its own
turns with email addresses and phone numbers intact; the sync verb (pcx.py 2165) then showed
those turns to every other project through a sink that applied only the refusal fence (RF 9).
And store.append_record took a content_fields_scrubbed flag that let a caller claiming it had
already scrubbed skip the gateway's content scrub (store.py 1798, pcx.py 661; RF 10).

  1. Sync: a chunk stored by a project that opted out of PII scrubbing is shown with [EMAIL]
     and [PHONE] in the digest, never the raw values (BLOCKED); a clean chunk's text is shown
     as stored (ALLOWED); the workspace refusal union still fences the digest.
  2. Gateway: append_record has no bypass parameter, no production script passes one, and a
     record with PII in a content field is scrubbed on the way in whatever the caller did first.

Runs under pytest and standalone (python test_sync_sink_scrubbed.py).
"""
import contextlib
import datetime
import inspect
import io
import json
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("PARSICA_WEAVE_ROOT", tempfile.mkdtemp(prefix="syncsink_boot_"))
os.environ.pop("PARSICA_CHANNEL", None)
SCRIPTS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPTS)
import store  # noqa: E402
import pcx    # noqa: E402

EMAIL = "jane.doe@example.com"
PHONE = "555-123-4567"


def _fresh():
    tmp = tempfile.mkdtemp(prefix="syncsink_root_")
    store.STORE_ROOT = Path(tmp)
    store.GLOBAL_CONFIG = store.STORE_ROOT / "config.json"
    a = tempfile.mkdtemp(prefix="syncsink_a_")
    b = tempfile.mkdtemp(prefix="syncsink_b_")
    store.ensure_workspace(a)
    store.ensure_workspace(b)
    return a, b


def _opt_out_of_pii(cwd):
    """Project `cwd` turns PII scrubbing off for its own store, the supported opt-out."""
    p = store._policy_config_path("project", cwd)
    data = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    data["scrub_pii"] = False
    p.write_text(json.dumps(data), encoding="utf-8")
    cfg = store.load_config(cwd)
    assert cfg["scrub_pii"] is False, "the opt-out did not take; the test would prove nothing"
    return cfg


def _store_chunk(cwd, cfg, text, tag):
    """One mirror chunk through the gateway, stamped now so the sync window includes it."""
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    md = store.mirror_dir(cwd)
    store._verified_dir(md, store.STORE_ROOT, 0o700)
    shard = md / ("%s.%d.jsonl" % (ts[:10], os.getpid()))
    rec = {"id": pcx._safe_chunk_id(tag, tag), "ts": ts, "sess": store.sess_token("sess-" + tag),
           "user": text, "asst": "", "red": 0}
    out = store.append_record(shard, rec, cfg)
    assert isinstance(out, dict) and out.get("stored") is not False, out
    return shard


def _sync(hours=48):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        pcx.handle_sync(["sync", "--hours", str(hours), "--limit", "40"])
    return buf.getvalue()


# --- 1. the sync sink ---------------------------------------------------------------------------------
def test_sync_scrubs_pii_from_a_project_that_opted_out():
    _a, b = _fresh()
    cfg_b = _opt_out_of_pii(b)
    shard = _store_chunk(b, cfg_b, "mail me at %s or call %s about the invoice" % (EMAIL, PHONE), "b1")
    assert EMAIL in shard.read_text(encoding="utf-8")      # B's own store holds it raw, by B's choice
    out = _sync()
    assert "1 chunk(s)" in out, out
    assert EMAIL not in out and PHONE not in out, out
    assert "[EMAIL]" in out and "[PHONE]" in out, out
    assert "about the invoice" in out, out


def test_sync_shows_a_clean_chunk_as_stored_and_still_fences_refusal_terms():
    a, _b = _fresh()
    cfg = store.load_config(a)
    _store_chunk(a, cfg, "we settled on exponential backoff for the retries", "a1")
    out = _sync()
    assert "we settled on exponential backoff for the retries" in out, out
    store.write_policy_list("refusal", "global", ["backoff"], a)
    out = _sync()
    assert "backoff" not in out and "<redacted>" in out, out


# --- 2. the gateway -------------------------------------------------------------------------------------
def test_gateway_has_no_bypass_parameter_and_no_caller_passes_one():
    assert "content_fields_scrubbed" not in inspect.signature(store.append_record).parameters
    for fn in sorted(os.listdir(SCRIPTS)):
        if fn.endswith(".py") and not fn.startswith("test_"):
            src = open(os.path.join(SCRIPTS, fn), encoding="utf-8", errors="replace").read()
            assert "content_fields_scrubbed=" not in src, "%s still passes the bypass" % fn


def test_gateway_scrubs_pii_in_content_whatever_the_caller_did_first():
    a, _b = _fresh()
    cfg = store.load_config(a)
    shard = _store_chunk(a, cfg, "reach me at %s" % EMAIL, "a2")
    text = shard.read_text(encoding="utf-8")
    assert EMAIL not in text and "[EMAIL]" in text, text


def _run():
    fns = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for name, fn in fns:
        fn()
        print("  ok  %s" % name)
    print("[test_sync_sink_scrubbed] %d/%d passed" % (len(fns), len(fns)))


if __name__ == "__main__":
    _run()
