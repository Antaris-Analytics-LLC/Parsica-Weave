"""The enrichment inbox is a secondary plaintext boundary, not an exempt temp directory.

A Windows junction reports os.path.islink() False, so RC5L-K accepted a redirected inbox and
staged captured conversation into its external target. The ownership marker and each staged leaf
also used pathname opens that could follow a planted link. Apply runs in a separate process and
must re-prove the inbox before trusting worker overlays.
"""
import contextlib
import importlib
import io
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store  # noqa: E402
import enrich  # noqa: E402
import enrich_prep  # noqa: E402
import enrich_apply  # noqa: E402


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


def test_dangling_ownership_marker_link_cannot_create_external_target(env, tmp_path, monkeypatch):
    """A real file-level symlink needs elevated privilege on this Windows dev machine (plain
    os.symlink() raises WinError 1314 here, same constraint noted throughout this suite), so the
    marker is stood in as an ordinary file and store.is_reparse_point is monkeypatched to report
    it as a link for this one path - exactly the boundary _inbox_problem's require_marker branch
    actually checks, so this is a faithful reproduction, not a weaker stand-in."""
    root, _ = env
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "external-marker"
    marker = inbox / ".parsica-weave-inbox"
    marker.write_text("", encoding="utf-8")
    real_is_reparse = store.is_reparse_point
    monkeypatch.setattr(store, "is_reparse_point",
                        lambda p: True if Path(p) == marker else real_is_reparse(p))
    with pytest.raises(SystemExit) as ex:
        enrich_prep._prepare_inbox(str(inbox), os.path.realpath(str(root)))
    assert ex.value.code == 2
    assert not victim.exists()


def test_windows_junction_shaped_inbox_is_refused_even_when_islink_is_false(
        env, tmp_path, monkeypatch):
    root, _ = env
    outside = tmp_path / "outside2"
    outside.mkdir()
    inbox = tmp_path / "inbox-junction"
    try:
        inbox.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip("cannot construct redirect stand-in: %s" % exc)
    # Windows NTFS junctions have this exact split: islink False, tag-aware lstat says redirect.
    monkeypatch.setattr(enrich_prep.os.path, "islink", lambda _p: False)
    with pytest.raises(SystemExit) as ex:
        enrich_prep._prepare_inbox(str(inbox), os.path.realpath(str(root)))
    assert ex.value.code == 2
    assert list(outside.iterdir()) == []


def test_public_prep_replaces_a_staged_leaf_link_instead_of_writing_through_it(
        env, tmp_path, monkeypatch, capsys):
    _, project = env
    inbox = tmp_path / "inbox3"
    outside = tmp_path / "outside3"
    outside.mkdir()
    victim = outside / "victim.txt"
    victim.write_text("ORIGINAL\n", encoding="utf-8")
    cid = "cid-abcdef1234567890"

    def pending(*_args, **_kwargs):
        # Runs after _prepare_inbox's cleanup, modeling a link planted during the pending scan.
        # This property (os.replace swaps the directory entry, never follows it, regardless of
        # what currently occupies the destination name) has no mockable seam short of the real
        # OS-level replace call, so skip gracefully where a file symlink needs privilege this
        # machine doesn't grant - same fallback the junction test above already uses.
        try:
            (inbox / (cid + ".txt")).symlink_to(victim)
        except (OSError, NotImplementedError) as exc:
            pytest.skip("cannot construct a real symlink stand-in: %s" % exc)
        return [{"id": cid, "user": "private captured conversation", "asst": "ack"}]

    monkeypatch.setattr(enrich_prep, "_pending", pending)
    monkeypatch.setattr(sys, "argv", ["enrich_prep.py", "--cwd", str(project),
                                      "--inbox", str(inbox)])
    enrich_prep.main()
    data = json.loads(capsys.readouterr().out)
    assert data["count"] == 1
    assert victim.read_text(encoding="utf-8") == "ORIGINAL\n"
    staged = inbox / (cid + ".txt")
    assert staged.is_file() and not staged.is_symlink()
    assert "private captured conversation" in staged.read_text(encoding="utf-8")


def test_apply_skips_a_linked_overlay_instead_of_reading_external_json(
        env, tmp_path, monkeypatch, capsys):
    """A real file symlink needs elevated privilege on this Windows dev machine, so the overlay
    leaf is a stand-in ordinary file and store.is_reparse_point is monkeypatched to flag it - the
    exact per-entry check store._safe_iterdir (which enrich_apply's overlay enumeration now goes
    through) performs, so this exercises the same decision point a real link would hit.

    enrich_apply.main() imports `store` INSIDE its own function body, so it always binds to
    whatever sys.modules['store'] holds at CALL time - which can differ from this file's own
    module-level `store` name if an earlier test in the same pytest session (test_project_label_
    fence.py deliberately pops and re-imports store/scrub for its own isolation, by design, and
    never restores the original object) left a different module object cached. Patching through
    sys.modules['store'] targets whatever enrich_apply.main() will actually import, regardless of
    what ran before this test in the same process."""
    root, project = env
    inbox = tmp_path / "inbox4"
    enrich_prep._prepare_inbox(str(inbox), os.path.realpath(str(root)))
    outside = tmp_path / "outside4"
    outside.mkdir()
    victim = outside / "overlay.json"
    victim.write_text('{"summary":"external attacker overlay"}', encoding="utf-8")
    linked_overlay = inbox / "cid-abcdef1234567890.overlay.json"
    linked_overlay.write_text('{"summary":"stand-in"}', encoding="utf-8")
    live_store = importlib.import_module("store")   # store/scrub may have been popped from
    real_is_reparse = live_store.is_reparse_point    # sys.modules entirely by an earlier test
    monkeypatch.setattr(live_store, "is_reparse_point",
                        lambda p: True if Path(p) == linked_overlay else real_is_reparse(p))
    seen = {}

    def apply(_cwd, mapping):
        seen.update(mapping)
        return len(mapping)

    monkeypatch.setattr(enrich, "apply_enrichments", apply)
    monkeypatch.setattr(sys, "argv", ["enrich_apply.py", "--cwd", str(project),
                                      "--inbox", str(inbox)])
    enrich_apply.main()
    data = json.loads(capsys.readouterr().out)
    assert data["applied"] == 0
    assert seen == {}


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
