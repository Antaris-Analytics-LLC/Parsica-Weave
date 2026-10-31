#!/usr/bin/env python3
"""Injected context describes; it does not command. REVIEW-2026-10.md, pile one, class (c).

At e480bbd three hook-injected surfaces carried instructions to the agent: the identity header
told it to open every response with the byline (pcx.py 130), the ~20-turn checkpoint told it to
run three shell commands before answering and to spawn an enrichment swarm (pcx.py 729-755), and
the SessionStart promote reminder told it what to tell the user and what not to do (pcx.py
1141-1143); the caveat that frames re-injected memory itself commanded (store.py 1704-1707), and
the mirror-index header told the agent to run a query (pcx.py 1185-1187).

These tests drive every injected surface (identity header, checkpoint note, heartbeat, the
SessionStart body, grep pairing, the promote reminder's text) and assert that none of the old
imperative phrases appear, while the useful facts (the byline signature, the turn count, the note
command, the reference caveat) still do. Run against e480bbd the header and checkpoint tests fail.

Runs under pytest and standalone (python test_injected_context_describes.py).
"""
import inspect
import io
import json
import os
import sys
import tempfile

os.environ.setdefault("PARSICA_WEAVE_ROOT", tempfile.mkdtemp(prefix="describe_boot_"))
SCRIPTS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPTS)
import store  # noqa: E402
import pcx    # noqa: E402

# The phrases the old injections carried. A new injection that says any of these commands the agent.
COMMANDS = (
    "Open every response", "your byline", "BEFORE answering", "do NOT", "Do not", "Spawn",
    "Workflow(", "swarm", "Answer the user", "Tell the user", "go-ahead", "Never treat",
    "Grep the code", "grep the store", "run `", "; run ", "to retrieve full chunks",
)


def _assert_describes(text, where):
    for phrase in COMMANDS:
        assert phrase not in text, "%s commands the agent: %r in %r" % (where, phrase, text[:300])


def _named_cfg():
    cfg = dict(store.DEFAULT_CONFIG)
    cfg["agent_name"] = "ExampleAgent"
    return cfg


def test_identity_header_states_the_byline_and_commands_nothing():
    hdr = pcx._identity_header(_named_cfg(), sess="ab12cd34")
    _assert_describes(hdr, "identity header")
    assert '"[ExampleAgent · ab12cd34]"' in hdr, "the signature is still stated"
    assert "The byline for this session is" in hdr
    assert "query" in hdr.lower(), "the queryable-surfaces line is still there"
    off = dict(_named_cfg())
    off["byline"] = False
    assert "byline" not in pcx._identity_header(off, sess="ab12cd34")


def test_checkpoint_note_names_the_commands_and_orders_none():
    note = pcx._checkpoint_directive("/tmp/some-project", store.DEFAULT_CONFIG, 20, "sess1234")
    _assert_describes(note, "checkpoint note")
    assert "turn 20" in note and "note --kind" in note and "--sess sess1234" in note
    assert "enrich-swarm" not in note and "Haiku" not in note, "the swarm is gone from the note"
    assert note.endswith("Nothing here runs on its own.")


def test_promote_reminder_text_describes():
    src = inspect.getsource(pcx._pending_promote_line)
    _assert_describes(src, "promote reminder source")
    assert "awaiting promote" in src and "promote --dry-run" in src


def test_reference_caveat_describes():
    _assert_describes(store.REFERENCE_CAVEAT, "reference caveat")
    assert "reference context only" in store.REFERENCE_CAVEAT


def _fresh_project():
    tmp = tempfile.mkdtemp(prefix="describe_root_")
    from pathlib import Path
    store.STORE_ROOT = Path(tmp)
    store.GLOBAL_CONFIG = store.STORE_ROOT / "config.json"
    cwd = tempfile.mkdtemp(prefix="describe_proj_")
    store.ensure_workspace(cwd)
    return cwd


def _capture(fn, payload):
    old = sys.stdout
    sys.stdout = io.StringIO()
    try:
        fn(payload)
        return sys.stdout.getvalue()
    finally:
        sys.stdout = old


def test_session_start_and_grep_pairing_injections_describe():
    cwd = _fresh_project()
    store.GLOBAL_CONFIG.write_text(json.dumps({"agent_name": "ExampleAgent"}), encoding="utf-8")
    cfg = store.load_config(cwd)
    # a working set with real content and one captured turn, so every SessionStart section renders
    store.write_doc(store.working_path(cwd), "## Pinned facts\n- the release tag is v1.0\n", cfg)
    md = store.mirror_dir(cwd)
    store.append_record(md / "2026-07-01.1.jsonl",
                        {"id": "cid-" + "a" * 20, "ts": "2026-07-01T00:00:00Z", "sess": "s-" + "b" * 16,
                         "user": "how does the retry backoff work", "asst": "it doubles per attempt"}, cfg)
    out = _capture(pcx.handle_session_start, {"cwd": cwd, "session_id": "sess1234"})
    assert out, "SessionStart emitted an inject"
    body = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    _assert_describes(body, "SessionStart inject")
    assert "[identity]" in body and "reference context only" in body and "Recent chat mirror" in body
    out = _capture(pcx.handle_posttooluse, {"cwd": cwd, "session_id": "sess1234", "tool_name": "Grep",
                                             "tool_input": {"pattern": "backoff"}})
    if out:
        _assert_describes(json.loads(out)["hookSpecificOutput"]["additionalContext"], "grep pairing inject")


def test_heartbeat_and_checkpoint_injections_describe():
    cwd = _fresh_project()
    cfg = store.load_config(cwd)
    store.write_doc(store.working_path(cwd), "## Pinned facts\n- heartbeat probe\n", cfg)
    every = int(cfg.get("heartbeat_every_n") or 30)
    # a due checkpoint is armed by the Stop path's locked increment; for the test, write the state
    # document directly (the same shape test_continuity_layer uses for the checkpoint-only case)
    (store.project_dir(cwd) / ".state.json").write_text(
        json.dumps({"turns": {"tp:hb.jsonl": every}, "checkpoint_due": {"tp:hb.jsonl": every},
                    "cursors": {}}), encoding="utf-8")
    out = _capture(pcx.handle_prompt, {"cwd": cwd, "transcript_path": "hb.jsonl", "session_id": "sess1234"})
    assert out, "the heartbeat and the checkpoint note fired together"
    body = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    _assert_describes(body, "UserPromptSubmit inject")
    assert "[heartbeat]" in body and "Checkpoint note" in body


def test_denylist_has_teeth():
    old_header = 'Open every response with the single line "[A · b]" (your byline). Grep the code, grep the store.'
    old_checkpoint = "As a quick side-action BEFORE answering, PIPE each ... Spawn the background enrichment swarm"
    old_reminder = "Tell the user; they can run 'parsica-weave promote --dry-run'. Do not promote without their go-ahead."
    for old in (old_header, old_checkpoint, old_reminder):
        try:
            _assert_describes(old, "old text")
        except AssertionError:
            continue
        raise AssertionError("the denylist missed the old text: %r" % old)


def _run():
    fns = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for name, fn in fns:
        fn()
        print("  ok  %s" % name)
    print("[test_injected_context_describes] %d/%d passed" % (len(fns), len(fns)))


if __name__ == "__main__":
    _run()
