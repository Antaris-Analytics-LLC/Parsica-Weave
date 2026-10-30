"""RC5L-I: the mirror-reader family missed by RC5L-H. That round routed the SessionStart
working-set/lessons/journal read (pcx._scrub_tail) through the safe primitives, but left an
entirely separate family of readers untouched: dedup (_existing_ids/_date_files/_recent_ids),
cross-session awareness at boot (_latest_mirror_chunk, _mirror_index), and deliberate catch-up
(_recent_elsewhere, handle_sync) - plus query.py's own shard listing (_shard_files/_load_chunks),
a distinct copy of the same gap. All of these used raw glob()/read_text() and so, like every
other RC5L-H sink before it was fixed, would read a hardlinked shard file or enumerate into a
junctioned project directory as if it were native content.

Fixed by routing every one of them through three new store.py seam functions
(safe_mirror_projects, safe_mirror_shard_files, read_jsonl_rows) built on the existing
_safe_iterdir/_read_text_no_follow primitives - not by re-deriving the containment logic per
call site, which is exactly how this gap opened in the first place (RC5L-H covered SessionStart
but never noticed this sibling family existed).

Junction tests need no privilege. Hardlink tests skip cleanly where unsupported rather than
passing vacuously - a skip that reads as a pass is how a positive control disappears from a CI
matrix.
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("PARSICA_WEAVE_ROOT", tempfile.mkdtemp(prefix="rc5li_root_"))
os.environ.pop("PARSICA_ISOLATED", None)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store  # noqa: E402
import pcx  # noqa: E402
import query  # noqa: E402

WINDOWS = os.name == "nt"
SECRET = "sk-ant-api03-" + "a" * 40


def _junction(link: Path, target: Path) -> bool:
    if not WINDOWS:
        return False
    target.mkdir(parents=True, exist_ok=True)
    return subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                          capture_output=True, text=True).returncode == 0


def _hardlink(link: Path, content: str) -> bool:
    target = link.with_suffix(link.suffix + ".real")
    target.write_text(content, encoding="utf-8")
    try:
        os.link(str(target), str(link))
        return True
    except (OSError, NotImplementedError):
        return False


@pytest.fixture()
def fresh_root(monkeypatch, tmp_path):
    root = tmp_path / "store_root"
    monkeypatch.setattr(store, "STORE_ROOT", root)
    # GLOBAL_CONFIG moves with the root (a present-but-outside config is refused since October 2026)
    monkeypatch.setattr(store, "GLOBAL_CONFIG", root / "config.json")
    return root


def _today():
    return pcx._now_utc().strftime("%Y-%m-%d")


def _now_ts():
    return pcx._now_utc().strftime("%Y-%m-%dT%H:%M:%S")


# --- dedup (_existing_ids / _date_files / _recent_ids): must not read a hardlinked shard --------

def test_existing_ids_refuses_a_hardlinked_shard(fresh_root, tmp_path):
    cwd = str(tmp_path / "proj1")
    os.makedirs(cwd, exist_ok=True)
    store.ensure_workspace(cwd)
    md = store.mirror_dir(cwd)
    md.mkdir(parents=True, exist_ok=True)
    shard = md / f"{_today()}.jsonl"
    row = json.dumps({"id": "leaked-id", "ts": _now_ts(), "user": SECRET}) + "\n"
    if not _hardlink(shard, row):
        pytest.skip("hardlinks unavailable here")
    assert pcx._existing_ids(shard) == set(), "a hardlinked shard must not surface its id"


def test_existing_ids_ordinary_use_is_unaffected(fresh_root, tmp_path):
    cwd = str(tmp_path / "proj2")
    os.makedirs(cwd, exist_ok=True)
    store.ensure_workspace(cwd)
    md = store.mirror_dir(cwd)
    md.mkdir(parents=True, exist_ok=True)
    shard = md / f"{_today()}.jsonl"
    shard.write_text(json.dumps({"id": "real-id", "ts": _now_ts()}) + "\n", encoding="utf-8")
    assert pcx._existing_ids(shard) == {"real-id"}


# --- cross-project walk (_recent_elsewhere): must not descend into a junctioned project dir -----

def test_recent_elsewhere_skips_a_junctioned_project_dir(fresh_root, tmp_path):
    mirror = fresh_root / "mirror"
    mirror.mkdir(parents=True, exist_ok=True)
    normal = mirror / "normal_proj-aaaaaaaaaa"
    normal.mkdir()
    (normal / f"{_today()}.jsonl").write_text(
        json.dumps({"id": "n1", "ts": _now_ts(), "user": "an ordinary turn"}) + "\n",
        encoding="utf-8")
    outside = tmp_path / "outside_elsewhere"
    outside.mkdir()
    (outside / f"{_today()}.jsonl").write_text(
        json.dumps({"id": "e1", "ts": _now_ts(), "user": SECRET}) + "\n", encoding="utf-8")
    if not _junction(mirror / "evil_proj-bbbbbbbbbb", outside):
        pytest.skip("mklink /J unavailable")
    out = pcx._recent_elsewhere(sess="", hours=12.0, limit=10)
    assert "an ordinary turn" in out
    assert SECRET not in out and "evil_proj" not in out


def test_recent_elsewhere_ordinary_use_is_unaffected(fresh_root, tmp_path):
    mirror = fresh_root / "mirror"
    mirror.mkdir(parents=True, exist_ok=True)
    normal = mirror / "normal_proj2-aaaaaaaaaa"
    normal.mkdir()
    (normal / f"{_today()}.jsonl").write_text(
        json.dumps({"id": "n2", "ts": _now_ts(), "user": "another ordinary turn"}) + "\n",
        encoding="utf-8")
    out = pcx._recent_elsewhere(sess="", hours=12.0, limit=10)
    assert "another ordinary turn" in out


# --- handle_sync: the deliberate catch-up digest must not surface a junctioned project's rows ---

def test_handle_sync_skips_a_junctioned_project_dir(fresh_root, tmp_path, capsys):
    mirror = fresh_root / "mirror"
    mirror.mkdir(parents=True, exist_ok=True)
    normal = mirror / "normal_proj3-aaaaaaaaaa"
    normal.mkdir()
    (normal / f"{_today()}.jsonl").write_text(
        json.dumps({"id": "n3", "ts": _now_ts(), "user": "a syncable turn"}) + "\n",
        encoding="utf-8")
    outside = tmp_path / "outside_sync"
    outside.mkdir()
    (outside / f"{_today()}.jsonl").write_text(
        json.dumps({"id": "e3", "ts": _now_ts(), "user": SECRET}) + "\n", encoding="utf-8")
    if not _junction(mirror / "evil_proj3-bbbbbbbbbb", outside):
        pytest.skip("mklink /J unavailable")
    pcx.handle_sync(["sync"])
    printed = capsys.readouterr().out
    assert "a syncable turn" in printed
    assert SECRET not in printed and "evil_proj3" not in printed


# --- query.py sibling (_shard_files / _load_chunks): the separate copy of the same gap ----------

def test_load_chunks_refuses_a_hardlinked_shard(fresh_root, tmp_path):
    cwd = str(tmp_path / "proj3")
    os.makedirs(cwd, exist_ok=True)
    store.ensure_workspace(cwd)
    md = store.mirror_dir(cwd)
    md.mkdir(parents=True, exist_ok=True)
    shard = md / f"{_today()}.jsonl"
    row = json.dumps({"id": "leaked-id2", "ts": _now_ts(), "user": SECRET}) + "\n"
    if not _hardlink(shard, row):
        pytest.skip("hardlinks unavailable here")
    chunks = query._load_chunks(cwd=cwd)
    assert chunks == [], "a hardlinked shard must not surface as this project's own chunk"


def test_load_chunks_ordinary_use_is_unaffected(fresh_root, tmp_path):
    cwd = str(tmp_path / "proj4")
    os.makedirs(cwd, exist_ok=True)
    store.ensure_workspace(cwd)
    md = store.mirror_dir(cwd)
    md.mkdir(parents=True, exist_ok=True)
    shard = md / f"{_today()}.jsonl"
    shard.write_text(json.dumps({"id": "real-id2", "ts": _now_ts(), "user": "a real turn"}) + "\n",
                     encoding="utf-8")
    chunks = query._load_chunks(cwd=cwd)
    assert len(chunks) == 1 and chunks[0]["id"] == "real-id2"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
