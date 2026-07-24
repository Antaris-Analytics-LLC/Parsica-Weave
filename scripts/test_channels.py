"""Channels: immutable-id WORKSPACE registry, chunk tagging, isolated-mode boot, sync digest.

A channel is a named lane of sessions with an IMMUTABLE assigned id (the name is a
renameable human label; history keys on the id). The registry is WORKSPACE-level
(channels.json at the store root) - one id per lane, spanning every project. Capture tags
chunks from the PARSICA_CHANNEL env var; PARSICA_ISOLATED=1 is real isolation (reads
everything, writes nothing durable); the `sync` verb is the deliberate whole-workspace
catch-up, labeled [<project-key> · <channel-or-sess>].
"""
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone

import pytest   # this suite uses fixtures (monkeypatch/tmp_path/capsys); pytest is required

# set BEFORE the store import so even a first-import (pytest collection order, standalone
# run) can never bind STORE_ROOT to the real HOME store - the fixture re-points per test.
os.environ.setdefault("PARSICA_WEAVE_ROOT", tempfile.mkdtemp(prefix="chan_boot_"))

import store
import pcx


@pytest.fixture
def ws(tmp_path, monkeypatch):
    """Isolated store root + a project cwd; PARSICA_CHANNEL/PARSICA_ISOLATED start unset."""
    root = tmp_path / "weave-root"
    monkeypatch.setattr(store, "STORE_ROOT", root)
    monkeypatch.setattr(store, "GLOBAL_CONFIG", root / "config.json")
    monkeypatch.setenv("PARSICA_WEAVE_ROOT", str(root))   # subprocesses, if any, stay in the temp root
    monkeypatch.delenv("PARSICA_CHANNEL", raising=False)
    monkeypatch.delenv("PARSICA_ISOLATED", raising=False)
    cwd = str(tmp_path / "proj")
    os.makedirs(cwd, exist_ok=True)
    return cwd


def _ts(hours_ago=0.0):
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)
            ).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _write_chunks(cwd, chunks):
    md = store.mirror_dir(cwd)
    md.mkdir(parents=True, exist_ok=True)
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    p = md / f"{day}.{os.getpid()}.jsonl"
    with p.open("a", encoding="utf-8") as fh:
        for c in chunks:
            fh.write(json.dumps(c) + "\n")
    return p


def test_registry_mints_once_and_rename_keeps_id(ws):
    a = store.channel_id_for("substrate")
    assert a and a.startswith("ch-")
    assert store.channel_id_for("substrate") == a              # same name -> same id
    b = store.channel_id_for("release")
    assert b != a
    assert store.rename_channel("substrate", "cognition") is True
    assert store.channel_id_for("cognition", create=False) == a   # id survived rename
    assert store.channel_id_for("substrate", create=False) == ""  # old name gone
    assert store.rename_channel("cognition", "release") is False  # name collision refused
    assert store.channel_name(a) == "cognition"
    # the registry is WORKSPACE-level: one file at the store root, no per-project copy
    assert store.channels_path() == store.STORE_ROOT / "channels.json"
    assert store.channels_path().exists()


def test_current_channel_from_env(ws, monkeypatch):
    assert store.current_channel(create=False) == ""           # unset -> untagged
    monkeypatch.setenv("PARSICA_CHANNEL", "substrate")
    cid = store.current_channel()
    assert cid == store.channel_id_for("substrate", create=False)


def _fake_turn(text="hello channel world"):
    prompt = {"type": "user", "timestamp": _ts(), "sessionId": "sessAAAA1111",
              "uuid": "u-1", "message": {"role": "user", "content": text}}
    asst = {"type": "assistant", "timestamp": _ts(), "sessionId": "sessAAAA1111",
            "uuid": "u-2", "message": {"role": "assistant", "content": "the response"}}
    return {"prompt": prompt, "recs": [asst]}


def test_build_chunk_stamps_chan(ws):
    cfg = dict(store.DEFAULT_CONFIG)
    tagged = pcx._build_chunk(_fake_turn(), cfg, chan="ch-abc12345")
    assert tagged["chan"] == "ch-abc12345"
    untagged = pcx._build_chunk(_fake_turn(), cfg)
    assert "chan" not in untagged                              # legacy shape unchanged


def test_chan_survives_the_write_gateway_scrub(ws):
    cfg = dict(store.DEFAULT_CONFIG)
    chunk = pcx._build_chunk(_fake_turn("mail me at sam@acme-example.io"), cfg,
                             chan="ch-abc12345")
    p = store.mirror_dir(ws) / "2026-01-01.1.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    store.append_record(p, chunk, cfg)
    rec = json.loads(p.read_text(encoding="utf-8").strip())
    assert rec["chan"] == "ch-abc12345"                        # reserved key rides intact
    assert "sam@acme-example.io" not in json.dumps(rec)        # scrub still bites


def test_mirror_index_shows_every_lane(ws):
    """The boot-filter isolation is REMOVED (superseded by PARSICA_ISOLATED): the index is
    one shared view - every channel's chunks plus untagged, whatever lane is live."""
    a = store.channel_id_for("one")
    b = store.channel_id_for("two")
    _write_chunks(ws, [
        {"ts": _ts(1), "sess": "s1", "id": "c1", "chan": a, "user": "alpha work item"},
        {"ts": _ts(1), "sess": "s2", "id": "c2", "chan": b, "user": "beta work item"},
        {"ts": _ts(1), "sess": "s3", "id": "c3", "user": "legacy untagged item"},
    ])
    everyone = pcx._mirror_index(ws, 10)
    assert "alpha work" in everyone and "beta work" in everyone
    assert "legacy untagged" in everyone


def _snap(root):
    """Every file under the store root -> (size, mtime): the byte-footprint fingerprint."""
    out = {}
    for r, _, fs in os.walk(root):
        for f in fs:
            p = os.path.join(r, f)
            st = os.stat(p)
            out[p] = (st.st_size, st.st_mtime_ns)
    return out


def test_isolated_session_reads_everything_writes_nothing(ws, monkeypatch, capsys):
    """PARSICA_ISOLATED=1: hooks fire and inject normally (loud banner + channel label),
    but the store is byte-identical after, and note/promote refuse with exit 3."""
    store.ensure_workspace(ws)
    a = store.channel_id_for("substrate")
    _write_chunks(ws, [{"ts": _ts(1), "sess": "s1", "id": "c1", "chan": a,
                        "user": "alpha work item"}])
    tpath = os.path.join(ws, "iso.jsonl")
    with open(tpath, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "user", "uuid": "iu1", "timestamp": _ts(),
                             "sessionId": "isosess1", "message":
                             {"role": "user", "content": "an isolated turn"}}) + "\n")
    monkeypatch.setenv("PARSICA_ISOLATED", "1")
    monkeypatch.setenv("PARSICA_CHANNEL", "substrate")
    before = _snap(store.STORE_ROOT)

    pcx.handle_session_start({"cwd": ws, "session_id": "isosess1"})
    out = capsys.readouterr().out
    assert "[isolated session" in out                          # the loud zero-footprint banner
    assert "[channel: substrate]" in out                       # label resolves WITHOUT minting
    assert "alpha work" in out                                 # reads EVERYTHING

    pcx.handle_stop({"cwd": ws, "transcript_path": tpath, "session_id": "isosess1"})
    pcx.handle_precompact({"cwd": ws, "transcript_path": tpath, "session_id": "isosess1"})
    capsys.readouterr()
    assert _snap(store.STORE_ROOT) == before                   # store byte-identical after

    with pytest.raises(SystemExit) as ex:
        pcx.handle_note(["note", "--kind", "lesson", "--cwd", ws])
    assert ex.value.code == 3
    assert "parsica note refused" in capsys.readouterr().out
    with pytest.raises(SystemExit) as ex:
        pcx.handle_promote(["promote", "--cwd", ws])
    assert ex.value.code == 3
    assert "parsica promote refused" in capsys.readouterr().out
    assert _snap(store.STORE_ROOT) == before                   # the refusals wrote nothing

    # the STANDALONE writers refuse too (they share store.is_isolated): calibrate event-recording
    # and the whole enrichment pipeline. Subprocesses inherit PARSICA_ISOLATED=1 + the temp root
    # (both set on the process env by the fixture / this test), so each runs in the same store.
    import subprocess
    import sys as _sys
    scripts = os.path.dirname(os.path.abspath(__file__))

    def _run_writer(script, extra):
        return subprocess.run([_sys.executable, os.path.join(scripts, script), "--cwd", ws] + extra,
                              capture_output=True, text=True, encoding="utf-8")

    r = _run_writer("calibrate.py", ["--target", "dec-1", "--event", "confirmed"])
    assert r.returncode == 3 and "parsica calibrate refused" in r.stdout
    r = _run_writer("enrich.py", ["--apply", '{"c1": {"summary": "x"}}'])
    assert r.returncode == 3 and "parsica enrich refused" in r.stderr
    iso_inbox = os.path.join(ws, "iso_inbox_never")   # isolation refuses BEFORE touching it
    r = _run_writer("enrich_apply.py", ["--inbox", iso_inbox])
    assert r.returncode == 3 and '"refused": "PARSICA_ISOLATED=1"' in r.stdout
    r = _run_writer("enrich_prep.py", ["--inbox", iso_inbox])
    assert r.returncode == 3 and '"refused": "PARSICA_ISOLATED=1"' in r.stdout

    # calibrate --stats and enrich --list-pending are pure READS: still available under isolation
    r = _run_writer("calibrate.py", ["--stats"])
    assert r.returncode == 0
    r = _run_writer("enrich.py", ["--list-pending"])
    assert r.returncode == 0

    assert _snap(store.STORE_ROOT) == before                   # every standalone refusal wrote nothing


def test_recent_elsewhere_off_by_default_on_when_enabled(ws, tmp_path, capsys):
    """Live boot context is project-scoped by DEFAULT: the SessionStart 'Recent elsewhere' block
    is OFF unless boot_recent_elsewhere=true. Flipping the config to true injects it. The on-demand
    --recent capability is unaffected either way (not exercised here)."""
    store.ensure_workspace(ws)
    other = str(tmp_path / "proj2")            # a SECOND project with recent live work
    os.makedirs(other, exist_ok=True)
    store.ensure_workspace(other)
    _write_chunks(other, [{"ts": _ts(1), "sess": "OTHERSESS", "id": "oe1",
                           "user": "cross project recent work"}])

    # default (no config key): no cross-project injection at boot
    pcx.handle_session_start({"cwd": ws, "session_id": "MYSESSAA"})
    assert "Recent elsewhere" not in capsys.readouterr().out

    # opt in on the booting project, then it appears
    cfg = store.project_dir(ws) / "config.json"
    cfg.write_text(json.dumps({"boot_recent_elsewhere": True}), encoding="utf-8")
    pcx.handle_session_start({"cwd": ws, "session_id": "MYSESSAA"})
    out = capsys.readouterr().out
    assert "Recent elsewhere" in out
    assert "cross project recent work" in out


def test_channel_verb_is_workspace_level(ws, capsys):
    """The channel verb takes NO --cwd: the registry is one workspace file."""
    pcx.handle_channel(["channel", "--name", "substrate"])
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] and out["id"].startswith("ch-")
    pcx.handle_channel(["channel", "--list"])
    listed = json.loads(capsys.readouterr().out)
    assert listed["channels"][out["id"]] == "substrate"


def test_sync_digest_crosses_the_workspace(ws, monkeypatch, capsys):
    a = store.channel_id_for("mine")
    b = store.channel_id_for("theirs")
    key = store.project_key(ws)
    _write_chunks(ws, [
        {"ts": _ts(1), "sess": "s1", "id": "c1", "chan": a, "user": "my own recent work"},
        {"ts": _ts(1), "sess": "s2", "id": "c2", "chan": b, "user": "their recent work"},
        {"ts": _ts(30), "sess": "s3", "id": "c3", "chan": b, "user": "their ancient work"},
        {"ts": _ts(1), "sess": "s4", "id": "c4", "user": "untagged recent work"},
    ])
    other = str(os.path.dirname(ws)) + os.sep + "proj-b"
    os.makedirs(other, exist_ok=True)
    okey = store.project_key(other)
    _write_chunks(other, [{"ts": _ts(1), "sess": "s9", "id": "c9", "chan": b,
                           "user": "work from another project"}])
    monkeypatch.setenv("PARSICA_CHANNEL", "mine")
    pcx.handle_sync(["sync", "--hours", "12"])
    out = capsys.readouterr().out
    assert "sync: last 12h across the workspace" in out
    assert store.local_zone_note() in out      # local-time display, zone noted once in the header
    assert "their recent work" in out and f"[{key} · theirs]" in out
    assert "my own recent work" not in out                     # own channel excluded
    assert "their ancient work" not in out                     # outside the window
    assert "untagged recent work" in out and f"[{key} · s4]" in out   # untagged rides, sess-labeled
    assert "work from another project" in out and f"[{okey} · theirs]" in out   # a lane spans projects


if __name__ == "__main__":
    # Run under pytest when invoked as a script (matching the other test_*.py entry points),
    # so this file cannot silently "pass" with zero assertions in the aggregate suite runner.
    import sys
    try:
        import pytest as _pt
    except ImportError:
        sys.stderr.write("test_channels.py requires pytest (pip install pytest)\n")
        sys.exit(1)   # fail LOUDLY - never look green without running
    sys.exit(_pt.main([__file__, "-q"]))
