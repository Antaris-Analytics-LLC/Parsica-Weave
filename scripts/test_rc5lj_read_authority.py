"""RC5L-J: the residual read-authority gaps found in Sol's RC5L-I Weave-only follow-up review.

RC5L-I fixed the FIFO/hardlink gate gap and the named mirror-reader family, but left three
manifestations of one underlying issue plus a separate migration gap:

1. Active-store containment checks (assert_contained_file, _safe_iterdir) proved the FINAL
   component only - never walked the lexical parent chain - so an intermediate directory alias
   still under the store root (<root>/working -> <root>/real_working) was silently followed.
2. query.py independently re-read a target project's config.json by raw pathname, after already
   loading the workspace-wide refusal union through the containment-checked seam - a symlinked or
   hardlinked config could alter query output a second, unchecked way.
3. query.py's --list-projects used raw iterdir()/glob() instead of the safe mirror seams.
4. Four migration readers (_migrate_lessons, _migrate_journal, _migrate_channels,
   _migrate_working) proved containment with assert_contained_file/walk_contained (which permit a
   hardlink - a lexically real, in-place file) and then read with a bare read_text(), missing the
   descriptor-level hardlink check only _read_text_no_follow performs.

Each test pairs the attack with an ordinary-use control.
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("PARSICA_WEAVE_ROOT", tempfile.mkdtemp(prefix="rc5lj_root_"))
os.environ.pop("PARSICA_ISOLATED", None)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store  # noqa: E402
import pcx  # noqa: E402
import query  # noqa: E402

WINDOWS = os.name == "nt"
SECRET = "needle rc5lj read-authority payload"


def _dir_alias(link: Path, target: Path) -> bool:
    target.mkdir(parents=True, exist_ok=True)
    if not WINDOWS:
        try:
            link.symlink_to(target, target_is_directory=True)
            return True
        except (OSError, NotImplementedError):
            return False
    return subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                          capture_output=True, text=True).returncode == 0


def _hardlink(link: Path, target: Path) -> bool:
    try:
        os.link(str(target), str(link))
        return True
    except (OSError, NotImplementedError):
        return False


@pytest.fixture()
def fresh_root(monkeypatch, tmp_path):
    root = tmp_path / "store_root"
    monkeypatch.setattr(store, "STORE_ROOT", root)
    store._refusal_union_memo.clear()
    store._refusal_union_memo.update({"sig": None, "terms": []})
    return root


# --- W-I1c: in-root intermediate directory aliases -----------------------------------------------

def test_isolated_sessionstart_refuses_inroot_working_and_journal_aliases(
        fresh_root, tmp_path, monkeypatch, capsys):
    cwd = tmp_path / "project"
    cwd.mkdir()
    key = store.project_key(str(cwd))
    real_working = fresh_root / "real_working"
    real_journal = fresh_root / "real_journal"
    if not (_dir_alias(fresh_root / "working", real_working)
            and _dir_alias(fresh_root / "journal", real_journal)):
        pytest.skip("directory aliases unavailable here")
    (real_working / (key + " - Working Set.md")).write_text(
        "## Pinned facts\n- " + SECRET + "\n", encoding="utf-8")
    (real_journal / "2026-07-23.md").write_text("### entry\n" + SECRET + "\n", encoding="utf-8")
    monkeypatch.setenv("PARSICA_ISOLATED", "1")
    pcx.handle_session_start({"cwd": str(cwd), "session_id": "s1"})
    assert SECRET not in capsys.readouterr().out


def test_isolated_sessionstart_ordinary_docs_still_restore(fresh_root, tmp_path, monkeypatch, capsys):
    cwd = tmp_path / "project2"
    cwd.mkdir()
    store.ensure_workspace(str(cwd))
    cfg = store.load_config(str(cwd))
    store.write_doc(store.working_path(str(cwd)), "## Pinned facts\n- ordinary pinned fact\n", cfg)
    store.append_doc(store.journal_dir() / "2026-07-23.md", "\nordinary journal fact\n", cfg)
    monkeypatch.setenv("PARSICA_ISOLATED", "1")
    pcx.handle_session_start({"cwd": str(cwd), "session_id": "s2"})
    out = capsys.readouterr().out
    assert "ordinary pinned fact" in out and "ordinary journal fact" in out


def test_explicit_query_refuses_an_inroot_mirror_parent_alias(fresh_root, tmp_path):
    key = "aliased-project-abcdef0123"
    real_mirror = fresh_root / "real_mirror"
    if not _dir_alias(fresh_root / "mirror", real_mirror):
        pytest.skip("directory aliases unavailable here")
    md = real_mirror / key
    md.mkdir()
    (md / "2026-07-23.1.jsonl").write_text(
        json.dumps({"id": "evil", "ts": "2026-07-23T10:00:00", "user": SECRET}) + "\n",
        encoding="utf-8")
    assert query._load_chunks(project=key) == []


# --- W-I1a: linked/hardlinked project config must not alter query output -------------------------

@pytest.mark.parametrize("link_kind", ["symlink", "hardlink"])
def test_query_ignores_a_linked_project_config(fresh_root, tmp_path, monkeypatch, capsys, link_kind):
    cwd = tmp_path / ("project-cfg-" + link_kind)
    cwd.mkdir()
    store.ensure_workspace(str(cwd))
    key = store.project_key(str(cwd))
    md = store.mirror_dir(str(cwd))
    (md / "2026-07-23.1.jsonl").write_text(
        json.dumps({"id": "good", "ts": "2026-07-23T10:00:00",
                    "user": "needle valid memory"}) + "\n", encoding="utf-8")
    external = tmp_path / ("external-config-" + link_kind + ".json")
    external.write_text(json.dumps({"refusal": ["needle"]}), encoding="utf-8")
    config = store.project_dir(str(cwd)) / "config.json"
    if link_kind == "symlink":
        try:
            config.symlink_to(external)
        except (OSError, NotImplementedError):
            pytest.skip("file symlinks unavailable here")
    elif not _hardlink(config, external):
        pytest.skip("hardlinks unavailable here")
    monkeypatch.setattr(sys, "argv", ["query.py", "--project", key, "--grep", "needle", "--full"])
    query.main()
    out = capsys.readouterr().out
    assert "needle valid memory" in out
    assert "redacted" not in out.lower()


def test_query_ordinary_project_config_refusal_still_applies(fresh_root, tmp_path, monkeypatch, capsys):
    """False-positive twin: an ORDINARY (unlinked) project config's own refusal term must still
    fence this project's own query output - the fix removes a raw re-read, not the refusal path
    itself, since workspace_refusal_terms() already covers ordinary per-project config."""
    cwd = tmp_path / "project-cfg-ordinary"
    cwd.mkdir()
    store.ensure_workspace(str(cwd))
    key = store.project_key(str(cwd))
    md = store.mirror_dir(str(cwd))
    (md / "2026-07-23.1.jsonl").write_text(
        json.dumps({"id": "good2", "ts": "2026-07-23T10:00:00",
                    "user": "needle valid memory"}) + "\n", encoding="utf-8")
    config = store.project_dir(str(cwd)) / "config.json"
    config.write_text(json.dumps({"refusal": ["needle"]}), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["query.py", "--project", key, "--grep", "needle", "--full"])
    query.main()
    out = capsys.readouterr().out
    assert "needle" not in out


# --- W-I1b: --list-projects must not traverse a linked mirror project -----------------------------

def test_list_projects_skips_a_linked_mirror_project(fresh_root, tmp_path, monkeypatch, capsys):
    mirror = fresh_root / "mirror"
    mirror.mkdir(parents=True)
    normal = mirror / "normal-abcdef0123"
    normal.mkdir()
    (normal / "2026-07-23.1.jsonl").write_text("{}\n", encoding="utf-8")
    outside = tmp_path / "outside-project"
    if not _dir_alias(mirror / "evil-abcdef0123", outside):
        pytest.skip("directory aliases unavailable here")
    (outside / "2042-12-31.jsonl").write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["query.py", "--list-projects"])
    query.main()
    out = capsys.readouterr().out
    assert "normal-abcdef0123" in out
    assert "evil-abcdef0123" not in out and "2042" not in out


# --- W-I2: migration must refuse a hardlinked trusted document ------------------------------------

@pytest.mark.parametrize("dataset", ["working", "lessons", "journal", "channels"])
def test_migrate_refuses_hardlinked_trusted_documents(fresh_root, tmp_path, dataset, capsys):
    silo = fresh_root / ("legacy-%s-abcdef0123" % dataset)
    silo.mkdir(parents=True)
    external = tmp_path / ("external-%s" % dataset)
    if dataset == "working":
        external.write_text("## Pinned facts\n- " + SECRET + "\n", encoding="utf-8")
        link = silo / "working.md"
    elif dataset == "lessons":
        external.write_text("# Lessons\n\n## Scar\n" + SECRET + "\n", encoding="utf-8")
        link = silo / "lessons.md"
    elif dataset == "journal":
        external.write_text("### entry\n" + SECRET + "\n", encoding="utf-8")
        (silo / "journal").mkdir()
        link = silo / "journal" / "2026-07-23.md"
    else:
        external.write_text(json.dumps({"channels": {"ch-abcdef123456": {"name": SECRET}}}),
                            encoding="utf-8")
        link = silo / "channels.json"
    if not _hardlink(link, external):
        pytest.skip("hardlinks unavailable here")
    pcx.handle_migrate(["migrate"])
    capsys.readouterr()
    assert not (silo / store.MIGRATED_MARKER).exists(), \
        "a hardlinked source must leave the silo re-runnable, not marked done"
    if dataset == "working":
        active = fresh_root / "working" / (silo.name + " - Working Set.md")
    elif dataset == "lessons":
        active = fresh_root / "lessons.md"
    elif dataset == "journal":
        active = fresh_root / "journal" / "2026-07-23.md"
    else:
        active = fresh_root / "channels.json"
    assert not active.exists() or SECRET not in active.read_text(encoding="utf-8", errors="replace")
    assert link.exists(), "the refused legacy source must remain in place"


def test_migrate_ordinary_working_set_still_completes(fresh_root, capsys):
    silo = fresh_root / "legacy-normal-abcdef0123"
    silo.mkdir(parents=True)
    (silo / "working.md").write_text("## Pinned facts\n- ordinary migrated fact\n", encoding="utf-8")
    pcx.handle_migrate(["migrate"])
    capsys.readouterr()
    assert (silo / store.MIGRATED_MARKER).is_file()
    active = fresh_root / "working" / (silo.name + " - Working Set.md")
    assert "ordinary migrated fact" in active.read_text(encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
