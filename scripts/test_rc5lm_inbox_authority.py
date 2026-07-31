"""RC5L-M: the enrichment inbox is an untrusted CHILD, not its own trust root.

Three related gaps, closed together:

  A. An unmarked directory containing only *.txt/*.overlay.json names was treated as "ours to
     clean" purely by filename suffix - an operator's real files with those extensions were
     silently deleted through a successful prep run.
  B. Once _prepare_inbox() had proven the inbox once, later writes (the ownership marker, staged
     chunks) passed the inbox itself as the containment ROOT for operations happening INSIDE it -
     which makes the inbox's own name exempt from being re-checked, so a directory-name swap after
     the proof went undetected.
  C. Apply (a separate process from prep) had the same problem: it re-proves the inbox once near
     the top of main(), then enumerates and reads through the inbox as its own root.

Portability: the swap in B/C is a DIRECTORY redirect, which needs no elevated privilege on either
platform - an NTFS junction on Windows (`mklink /J`), a directory symlink on POSIX
(`os.symlink(..., target_is_directory=True)`). The helper picks the right primitive per platform;
a `require_redirect` fixture probes once up front and SKIPS the whole test where neither is
available, so a platform that cannot build the redirect never reaches the assertion vacuously.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store  # noqa: E402
import enrich  # noqa: E402
import enrich_prep  # noqa: E402
import enrich_apply  # noqa: E402


def _redirect_dir(link, target):
    """Point directory `link` at `target` without elevated privilege. Windows: NTFS junction.
    POSIX: directory symlink. Returns True on success, False if the primitive is unavailable."""
    if os.name == "nt":
        r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                           capture_output=True, text=True)
        return r.returncode == 0 and os.path.isdir(str(link))
    try:
        os.symlink(str(target), str(link), target_is_directory=True)
        return True
    except (OSError, NotImplementedError):
        return False


def _can_redirect(base):
    base.mkdir(parents=True, exist_ok=True)
    target = base / "t"
    target.mkdir()
    return _redirect_dir(base / "l", target)


@pytest.fixture()
def require_redirect(tmp_path):
    if not _can_redirect(tmp_path / "_redirect_probe"):
        pytest.skip("directory redirects unavailable on this platform/account")


@pytest.fixture()
def env(tmp_path, monkeypatch):
    root = tmp_path / "store"
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(store, "STORE_ROOT", root)
    monkeypatch.setattr(store, "GLOBAL_CONFIG", root / "config.json")
    monkeypatch.setattr(store, "is_isolated", lambda: False)
    monkeypatch.delenv("PARSICA_ISOLATED", raising=False)
    return root, project


# --- A: an unmarked non-empty inbox is refused, never adopted or deleted -----------------------

def test_unmarked_nonempty_inbox_is_refused_not_deleted(env, tmp_path):
    root, _ = env
    inbox = tmp_path / "operator_dir"
    inbox.mkdir()
    (inbox / "important.txt").write_text("operator content\n", encoding="utf-8")
    (inbox / "notes.overlay.json").write_text('{"real":"operator data"}', encoding="utf-8")
    with pytest.raises(SystemExit) as ex:
        enrich_prep._prepare_inbox(str(inbox), os.path.realpath(str(root)))
    assert ex.value.code == 2
    assert (inbox / "important.txt").read_text(encoding="utf-8") == "operator content\n"
    assert (inbox / "notes.overlay.json").read_text(encoding="utf-8") == '{"real":"operator data"}'
    assert not (inbox / ".parsica-weave-inbox").exists()


def test_empty_inbox_is_adopted_and_a_marked_inbox_still_clears_only_its_own_staging(env, tmp_path):
    """False-positive companion: the fix must not break the legitimate cases."""
    root, _ = env
    inbox = tmp_path / "fresh_inbox"
    inbox.mkdir()
    enrich_prep._prepare_inbox(str(inbox), os.path.realpath(str(root)))
    assert (inbox / ".parsica-weave-inbox").exists()
    (inbox / "stale.txt").write_text("old staging\n", encoding="utf-8")
    enrich_prep._prepare_inbox(str(inbox), os.path.realpath(str(root)))
    assert not (inbox / "stale.txt").exists()
    assert (inbox / ".parsica-weave-inbox").exists()


# --- B: prep must not trust the inbox NAME once it can be swapped after the proof --------------

def test_prep_refuses_to_stage_through_an_inbox_swapped_after_preparation(
        env, require_redirect, tmp_path, monkeypatch):
    root, project = env
    inbox = tmp_path / "inbox"
    outside = tmp_path / "outside"
    outside.mkdir()
    backup = tmp_path / "inbox-original"
    cid = "cid-abcdef1234567890"

    def pending(*_a, **_k):
        os.rename(inbox, backup)
        assert _redirect_dir(inbox, outside), "redirect creation failed after probe passed"
        return [{"id": cid, "user": "private captured conversation", "asst": "ack"}]

    monkeypatch.setattr(enrich_prep, "_pending", pending)
    monkeypatch.setattr(sys, "argv", ["enrich_prep.py", "--cwd", str(project),
                                      "--inbox", str(inbox)])
    with pytest.raises(SystemExit) as ex:
        enrich_prep.main()
    assert ex.value.code != 0
    assert not (outside / (cid + ".txt")).exists()
    assert list(outside.iterdir()) == []


# --- C: apply must not trust the inbox NAME once it can be swapped after marker proof -----------

def test_apply_refuses_to_ingest_through_an_inbox_swapped_after_marker_proof(
        env, require_redirect, tmp_path, monkeypatch, capsys):
    root, project = env
    inbox = tmp_path / "inbox2"
    outside = tmp_path / "outside2"
    outside.mkdir()
    backup = tmp_path / "inbox2-original"
    store.ensure_workspace(str(project))
    cfg = store.load_config(str(project))
    cid = "cid-abcdef1234567890"
    mp = store.mirror_dir(str(project)) / "2026-07-23.123.jsonl"
    store.append_record(mp, {"id": cid, "ts": "2026-07-23T00:00:00.000Z", "sess": "s-12345678",
                              "user": "hello", "asst": "ack"}, cfg)
    enrich_prep._prepare_inbox(str(inbox), os.path.realpath(str(root)))
    (inbox / (cid + ".overlay.json")).write_text('{"summary":"benign overlay"}', encoding="utf-8")
    (outside / (cid + ".overlay.json")).write_text(
        '{"summary":"external attacker overlay"}', encoding="utf-8")

    real_problem = enrich_prep._inbox_problem
    triggered = {"v": False}

    def wrapped_problem(path, store_root, *, allow_missing, require_marker=False):
        result = real_problem(path, store_root, allow_missing=allow_missing,
                              require_marker=require_marker)
        if result is None and require_marker and not triggered["v"]:
            os.rename(inbox, backup)
            assert _redirect_dir(inbox, outside), "redirect creation failed after probe passed"
            triggered["v"] = True
        return result

    monkeypatch.setattr(enrich_prep, "_inbox_problem", wrapped_problem)
    monkeypatch.setattr(sys, "argv", ["enrich_apply.py", "--cwd", str(project),
                                      "--inbox", str(inbox)])
    # The per-file read is refused through _read_text_no_follow's containment check, which
    # enrich_apply's own loop catches and counts as malformed rather than propagating - a clean
    # per-entry refusal, not a crash. What matters is the external content never gets applied.
    try:
        enrich_apply.main()
    except SystemExit:
        pass
    apply_out = json.loads(capsys.readouterr().out)
    assert apply_out.get("applied", 0) == 0
    assert triggered["v"], "the attack never got a chance to fire"
    ed = store.enrich_dir(str(project))
    rows = []
    if ed.exists():
        for f in ed.glob("*.jsonl"):
            for line in f.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    rows.append(json.loads(line))
    assert not any("external attacker overlay" in json.dumps(r) for r in rows)


# --- ordinary use: unswapped prep/apply must still work exactly as before ----------------------

def test_ordinary_prep_and_apply_still_work_with_no_swap(env, tmp_path, monkeypatch, capsys):
    root, project = env
    store.ensure_workspace(str(project))
    cfg = store.load_config(str(project))
    cid = "cid-1234567890abcdef"
    mp = store.mirror_dir(str(project)) / "2026-07-23.124.jsonl"
    store.append_record(mp, {"id": cid, "ts": "2026-07-23T00:01:00.000Z", "sess": "s-abcdefgh",
                              "user": "hi there", "asst": "ack"}, cfg)
    inbox = tmp_path / "ordinary_inbox"

    def pending(*_a, **_k):
        return [{"id": cid, "user": "hi there", "asst": "ack"}]

    monkeypatch.setattr(enrich_prep, "_pending", pending)
    monkeypatch.setattr(sys, "argv", ["enrich_prep.py", "--cwd", str(project),
                                      "--inbox", str(inbox)])
    enrich_prep.main()
    prep_out = json.loads(capsys.readouterr().out)
    assert prep_out["count"] == 1
    assert (inbox / (cid + ".txt")).exists()

    (inbox / (cid + ".overlay.json")).write_text(
        '{"summary":"a real summary"}', encoding="utf-8")

    def apply(_cwd, mapping):
        return len(mapping)

    monkeypatch.setattr(enrich, "apply_enrichments", apply)
    monkeypatch.setattr(sys, "argv", ["enrich_apply.py", "--cwd", str(project),
                                      "--inbox", str(inbox)])
    enrich_apply.main()
    apply_out = json.loads(capsys.readouterr().out)
    # swept=2 is the staged .txt and .overlay.json for the one chunk that applied: once an
    # enrichment lands, its plaintext copy in the system temp dir has done its whole job and no
    # longer outlives the store the README tells a user to delete. This assertion is incidental
    # end-to-end evidence for that - the behaviour itself is pinned in test_enrich_inbox_sweep.py.
    assert apply_out == {"applied": 1, "malformed": 0, "swept": 2}


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
