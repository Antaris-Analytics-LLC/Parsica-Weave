#!/usr/bin/env python3
"""Universal refusal fence (W3) - a refusal term configured in ANY project must fence that content
wherever it surfaces, not just in the session that owns the term.

load_config() unions global + the CURRENT project (right for write-time scrub). But shared surfaces
cross projects: the boot inject reads the workspace-wide lessons + journal; the sync digest quotes every
project's mirror. A term configured in project A must redact A's content when it surfaces while booting
project B - even content captured BEFORE the term was added. These tests prove the OUTPUT sinks fence
with the workspace-wide union, and that channel names are validated (and rejected) at creation.
"""
import contextlib
import datetime
import io
import json
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("PARSICA_WEAVE_ROOT", tempfile.mkdtemp(prefix="w3_fence_boot_"))
os.environ.pop("PARSICA_CHANNEL", None)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store   # noqa: E402
import pcx     # noqa: E402

TMP = tempfile.gettempdir()
PROJ_A = os.path.join(TMP, "w3_proj_a")   # the project that OWNS the refusal term
PROJ_B = os.path.join(TMP, "w3_proj_b")   # the project we BOOT / run sync from
TERM = "FALCONXYZ"                        # project A's private refusal term


def _fresh():
    tmp = tempfile.mkdtemp(prefix="w3_fence_root_")
    store.STORE_ROOT = Path(tmp)
    store.GLOBAL_CONFIG = store.STORE_ROOT / "config.json"
    store.STORE_ROOT.mkdir(parents=True, exist_ok=True)
    store.ensure_workspace(PROJ_B)


def _fence_term_in_project_a():
    ad = store.project_dir(PROJ_A)
    ad.mkdir(parents=True, exist_ok=True)
    (ad / "config.json").write_text(json.dumps({"refusal": [TERM]}), encoding="utf-8")


def _boot_output(cwd, sess="bootsess"):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        pcx.handle_session_start({"cwd": cwd, "session_id": sess})
    return buf.getvalue()


def _sync_output():
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        pcx.handle_sync(["sync"])
    return buf.getvalue()


def test_boot_fences_cross_project_term_in_shared_lessons():
    _fresh()
    _fence_term_in_project_a()
    # a scar in the WORKSPACE-WIDE lessons doc mentions project A's fenced term (written before, or by
    # another project - the lessons doc is shared). Booting project B must still redact it.
    store.lessons_path().write_text(
        "## a scar\nWatch the %s rollout carefully; it bit us once.\n" % TERM, encoding="utf-8")
    out = _boot_output(PROJ_B)
    assert TERM not in out                         # A's term fenced even though we boot in B
    assert "scar" in out                           # the lessons section still surfaces (fenced, not dropped)
    assert store.scrub.REFUSAL_MARKER in out       # ...with the redaction marker in place


def test_sync_fences_cross_project_term():
    _fresh()
    # a chunk captured in project A BEFORE the term was fenced -> raw on disk (the real gap)
    am = store.mirror_dir(PROJ_A)
    am.mkdir(parents=True, exist_ok=True)
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    (am / ("%s.1.jsonl" % ts[:10])).write_text(
        json.dumps({"id": "a1", "ts": ts, "sess": "aaaaaaaa", "user": "the %s plan" % TERM}) + "\n",
        encoding="utf-8")
    _fence_term_in_project_a()                     # NOW project A fences the term
    out = _sync_output()
    assert TERM not in out                         # the digest fences it with the workspace union
    assert "plan" in out                           # the chunk still shows (just fenced)


def test_workspace_refusal_union_and_fence_output():
    _fresh()
    store.GLOBAL_CONFIG.write_text(json.dumps({"refusal": ["GLOBALTERM"]}), encoding="utf-8")
    for proj, term in ((PROJ_A, "ATERM"), (PROJ_B, "BTERM")):
        d = store.project_dir(proj)
        d.mkdir(parents=True, exist_ok=True)
        (d / "config.json").write_text(json.dumps({"refusal": [term]}), encoding="utf-8")
    terms = store.workspace_refusal_terms()
    assert {"GLOBALTERM", "ATERM", "BTERM"} <= set(terms)     # union across global + every project
    fenced = store.fence_output("keep ATERM and BTERM and GLOBALTERM here")
    assert "ATERM" not in fenced and "BTERM" not in fenced and "GLOBALTERM" not in fenced
    assert "keep" in fenced and "here" in fenced             # non-fenced text is untouched
    assert store.fence_output("") == ""                      # empty in, empty out
    assert store.fence_output("nothing fenced here") == "nothing fenced here"   # no terms hit -> verbatim


def test_channel_name_validation():
    _fresh()
    _fence_term_in_project_a()
    # normal, project-style names pass transparently
    assert store.valid_channel_name("parsica-weave")
    assert store.valid_channel_name("my project 2")
    assert store.valid_channel_name("feature/login.v2")
    # unsafe names are rejected
    assert not store.valid_channel_name("")                  # empty
    assert not store.valid_channel_name("   ")               # whitespace-only
    assert not store.valid_channel_name("x" * 65)            # over-length
    assert not store.valid_channel_name("bad\u2800name")     # braille-blank (category So, caught explicitly)
    assert not store.valid_channel_name("line\nbreak")       # newline
    assert not store.valid_channel_name("zero\u200bwidth")   # zero-width space (Cf)
    assert not store.valid_channel_name("ctrl\x07bell")      # control char (Cc)
    assert not store.valid_channel_name("team %s" % TERM)    # carries a fenced term
    # GPT B2(d): the category check alone missed these - now caught explicitly / via scrub-equality
    assert not store.valid_channel_name("hang\u3164ul")      # Hangul filler U+3164 (category Lo, blank)
    assert not store.valid_channel_name("line\u2028sep")     # line separator (Zl)
    assert not store.valid_channel_name("para\u2029sep")     # paragraph separator (Zp)
    assert not store.valid_channel_name("AKIAZZ00SENTINEL0011")   # credential-shaped name (a secret)


def test_channel_mint_and_rename_reject_unsafe_names():
    _fresh()
    assert store.channel_id_for("bad\u2800name", create=True) == ""   # unsafe name mints NOTHING
    assert store.load_channels()["channels"] == {}                    # ...and nothing was written
    cid = store.channel_id_for("parsica-weave", create=True)          # a normal name mints
    assert cid and cid.startswith("ch-")
    assert store.channel_id_for("parsica-weave", create=False) == cid  # resolves back
    assert store.rename_channel("parsica-weave", "line\nbreak") is False   # unsafe rename refused
    assert store.rename_channel("parsica-weave", "renamed-ok") is True     # safe rename allowed
    assert store.channel_name(cid) == "renamed-ok"


def test_emit_fences_every_string_value():
    """GPT B2(a): the single _emit sink fences EVERY string value with the workspace union - so the
    identity header (composed AFTER the per-section body scrub) and any structured field are covered,
    not just the pre-scrubbed body."""
    _fresh()
    _fence_term_in_project_a()                          # project A fences TERM
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        pcx._emit({"hookSpecificOutput": {"additionalContext": "You are %s. [%s]" % (TERM, TERM)}})
    out = buf.getvalue()
    assert out and TERM not in out                      # the value is emitted, TERM redacted at the sink
    assert store.scrub.REFUSAL_MARKER in out


def test_channel_reject_does_not_echo_the_name():
    """GPT B2(c): a rejected channel name must NOT be echoed back - it may carry the very secret/refusal
    term that got it rejected. The response is generic; a nonzero exit signals the rejection."""
    _fresh()
    _fence_term_in_project_a()
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            pcx.handle_channel(["channel", "--name", "team %s" % TERM])
    except SystemExit as e:
        assert e.code == 2
    out = buf.getvalue()
    assert TERM not in out and "rejected" in out        # generic rejection, the input is never echoed


def test_project_key_scrubs_credential_shaped_basename():
    """GPT B2(e): a credential-shaped repo basename must not become the durable, surfaced project slug."""
    _fresh()
    # the mechanism project_key relies on: the scrubber recognizes the credential shape
    cleaned, _ = store.scrub.scrub("AKIAZZ00SENTINEL0011", scrub_pii=True)
    assert cleaned != "AKIAZZ00SENTINEL0011"
    # end-to-end: the credential never appears in a project key (falls back to a neutral slug + hash)
    d = os.path.join(TMP, "AKIAZZ00SENTINEL0011")
    os.makedirs(d, exist_ok=True)
    assert "AKIAZZ00SENTINEL0011" not in store.project_key(d)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
    print("all refusal-fence tests passed")
