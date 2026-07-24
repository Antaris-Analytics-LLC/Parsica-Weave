#!/usr/bin/env python3
"""RC5L-G W-F3: every durable publish in store.py renames a mkstemp-created temp into place.

The finding: several store writers published through a PREDICTABLE, non-exclusive temp name -
`p.with_suffix(".json.tmp")` (save_channels, the review's sharpest reproduction) and
`<name>.<pid>.<tid>.tmp` (save_state, the migration marker, workspace_id, save_global_config,
_raw_write) - written with write_text() and then os.replace()'d onto the live file. That name is
guessable from a process list and the write is not exclusive, so a symlink or hardlink pre-planted
at exactly that path is FOLLOWED (symlink -> an external file is overwritten) or SHARED (hardlink ->
the same inode is written) and the replace then installs the result as the live document, under a
command that reports success. mkstemp - O_EXCL creation with an unguessable random suffix - is the
one construction that stops both: the temp cannot be named in advance and cannot already exist.

Two kinds of test here:
  * a DISCOVERY GATE that walks store.py's AST and asserts EVERY function which renames a file into
    place also creates its temp with mkstemp, so a writer added later cannot reintroduce the pattern
    by a route no one thought to re-check (the hand-maintained exemption list is exactly how trace.py
    stayed invisible on the Spirit side). It carries its own non-vacuity and false-positive twins.
  * RUNTIME teeth that plant a hardlink at the OLD predictable temp name each writer used and prove
    the writer never touches it. These FAIL against the pre-fix shape (write_text hits the planted
    name) and PASS now (mkstemp names a different file). Hardlinks need no privilege on NTFS, so
    these run on the release box; the symlink-through-destination twin is skip-guarded.
"""
import ast
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import pytest

os.environ.setdefault("PARSICA_WEAVE_ROOT", tempfile.mkdtemp(prefix="atomic_disc_boot_"))
_HERE = Path(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, str(_HERE))
import store   # noqa: E402


# ============================================================================================
# The discovery gate
# ============================================================================================

def _own_nodes(fn):
    """Every AST node in `fn`'s OWN scope, NOT descending into a nested def - so a rename inside a
    nested helper is attributed to that helper, never falsely to its enclosing function."""
    out = []
    stack = list(ast.iter_child_nodes(fn))
    while stack:
        n = stack.pop()
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        out.append(n)
        stack.extend(ast.iter_child_nodes(n))
    return out


def _is_rename_publish(call):
    """True if `call` is an atomic-publish rename: os.replace/os.rename (any arity), or a Path
    method .replace/.rename with exactly one positional arg and no keywords.

    The one-positional-no-keyword rule is what separates a file publish from its lookalikes:
    datetime.replace(tzinfo=...) carries a keyword, and str.replace(a, b) carries two positionals."""
    f = call.func
    if not isinstance(f, ast.Attribute) or f.attr not in ("replace", "rename"):
        return False
    if isinstance(f.value, ast.Name) and f.value.id == "os":
        return True
    return len(call.args) == 1 and not call.keywords


def _is_mkstemp(call):
    f = call.func
    return (isinstance(f, ast.Attribute) and f.attr == "mkstemp") or \
           (isinstance(f, ast.Name) and f.id == "mkstemp")


def _writes_content(call):
    """True if `call` writes bytes to a file: write_text/write_bytes, os.fdopen, os.write.

    This is what separates a publish-through-temp (the pattern the fix governs) from a plain file
    MOVE. `_move_aside` and `migrate_move` rename an already-existing real file into place; they
    create no temp and write no content, so the mkstemp rule does not apply to them - a move follows
    a destination link the same way os.replace does anywhere, which is safe, and their source/dest
    containment is the migration-containment suite's job, not this gate's."""
    f = call.func
    if isinstance(f, ast.Attribute):
        if f.attr in ("write_text", "write_bytes"):
            return True
        if isinstance(f.value, ast.Name) and f.value.id == "os" and f.attr in ("fdopen", "write"):
            return True
    return False


def _with_suffix_tmp(call):
    """A `.with_suffix('...tmp...')` - the predictable-temp-name tell the fix removed."""
    f = call.func
    if not (isinstance(f, ast.Attribute) and f.attr == "with_suffix"):
        return False
    return any(isinstance(a, ast.Constant) and isinstance(a.value, str) and "tmp" in a.value
               for a in call.args)


def _publishers(tree):
    """(name, has_mkstemp) for every function in `tree` that PUBLISHES a file: writes content to a
    temp AND renames it into place. A plain move (rename only, no content write) is not a publisher."""
    out = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        calls = [n for n in _own_nodes(fn) if isinstance(n, ast.Call)]
        renames = any(_is_rename_publish(n) for n in calls)
        writes = any(_writes_content(n) for n in calls)
        if renames and writes:
            out.append((fn.name, any(_is_mkstemp(n) for n in calls)))
    return out


def _store_tree():
    return ast.parse((_HERE / "store.py").read_text(encoding="utf-8"))


def test_every_durable_publish_creates_its_temp_with_mkstemp():
    """The gate. A function that renames a file into place without mkstemp is publishing through a
    name it chose - the exact predictable-temp shape this blocker exists to keep out."""
    offenders = [name for name, has in _publishers(_store_tree()) if not has]
    assert not offenders, (
        "these store.py functions publish a file (os.replace / Path.replace) without creating the "
        "temp via mkstemp - a predictable temp name a link can capture: %r" % offenders)


def test_the_discovery_reaches_the_known_publishers_and_is_not_vacuous():
    """Anti-vacuity twin. If the AST match silently stops finding renames, the gate above passes over
    an empty set forever. Pin the two publishers that must always be found."""
    found = {name for name, _ in _publishers(_store_tree())}
    assert "atomic_write" in found, "discovery lost the shared atomic_write publisher: %r" % found
    assert "write_identity_migration_receipt" in found, (
        "discovery lost the router-receipt publisher: %r" % found)


def test_no_store_writer_builds_a_predictable_tmp_suffix():
    """The negative-space twin: the concrete construction the fix deleted (`with_suffix('.json.tmp')`)
    must not reappear anywhere in the module."""
    bad = [n for n in ast.walk(_store_tree()) if isinstance(n, ast.Call) and _with_suffix_tmp(n)]
    assert not bad, "a with_suffix('...tmp...') predictable temp name is back in store.py"


def test_the_gate_would_flag_a_reintroduced_predictable_publish():
    """False-positive/true-positive twin for the checker itself, on synthetic sources - so a bug in
    the AST logic that made it match nothing (or everything) is caught here, not in the field."""
    caught = _publishers(ast.parse(
        "import os\ndef bad(p, t):\n    (p + '.tmp').write_text(t)\n    os.replace(p + '.tmp', p)\n"))
    assert caught == [("bad", False)], "the gate failed to flag a mkstemp-less publish: %r" % caught

    clean = _publishers(ast.parse(
        "import os, tempfile\ndef good(dst, t):\n"
        "    fd, tmp = tempfile.mkstemp(dir='.')\n    os.write(fd, t)\n    os.replace(tmp, dst)\n"))
    assert clean == [("good", True)], "the gate misjudged a proper mkstemp publish: %r" % clean

    # a plain MOVE (rename, no content write) is not a publisher and needs no mkstemp
    assert _publishers(ast.parse("import os\ndef mv(a, b):\n    os.replace(a, b)\n")) == [], \
        "the gate wrongly demanded mkstemp of a plain file move"
    # datetime.replace(tzinfo=...) and str.replace(a, b) are NOT file publishes
    assert _publishers(ast.parse("def f(dt):\n    return dt.replace(tzinfo=1)\n")) == []
    assert _publishers(ast.parse("def f(s):\n    return s.replace('a', 'b')\n")) == []


# ============================================================================================
# Runtime teeth: a hardlink planted at the OLD predictable temp name is never written through
# ============================================================================================

VICTIM = "PRECIOUS EXTERNAL DATA\n"


def _fresh_root():
    root = Path(tempfile.mkdtemp(prefix="atomic_disc_root_"))
    store.STORE_ROOT = root
    store.GLOBAL_CONFIG = root / "config.json"
    return root


def _hardlink(link, target):
    """A hardlink at `link` -> `target`. NTFS `mklink /H` needs no privilege; POSIX os.link likewise.
    Returns False when the platform/filesystem forbids it, so the test skips rather than lies."""
    if os.name != "nt":
        try:
            os.link(str(target), str(link))
            return True
        except OSError:
            return False
    r = subprocess.run(["cmd", "/c", "mklink", "/H", str(link), str(target)],
                       capture_output=True, text=True)
    return r.returncode == 0


def _old_pid_tid_tmp(p):
    """The `<name>.<pid>.<tid>.tmp` name the pre-fix writers built, for THIS process - so a
    mismatched guess can never make the test pass vacuously."""
    return p.with_name("%s.%s.%s.tmp" % (p.name, os.getpid(), threading.get_ident()))


def test_save_channels_never_writes_through_the_old_predictable_temp(tmp_path):
    """save_channels' pre-fix temp was `channels.json.tmp` (with_suffix) - the review's sharpest
    reproduction. A hardlink planted there must be left untouched; the registry lands via mkstemp."""
    root = _fresh_root()
    victim = tmp_path / "VICTIM.txt"
    victim.write_text(VICTIM, encoding="utf-8")
    root.mkdir(parents=True, exist_ok=True)
    old_tmp = store.channels_path().with_suffix(".json.tmp")   # channels.json -> channels.json.tmp
    if not _hardlink(old_tmp, victim):
        pytest.skip("no privilege-free hardlink available on this filesystem")

    store.save_channels({"lanes": {"main": "id-0123456789ab"}})

    assert victim.read_text(encoding="utf-8") == VICTIM, \
        "save_channels wrote through the planted channels.json.tmp hardlink"
    assert json.loads(store.channels_path().read_text(encoding="utf-8"))["lanes"]["main"] \
        == "id-0123456789ab", "the registry did not land at its real destination"


def test_save_state_never_writes_through_the_old_predictable_temp(tmp_path):
    """save_state runs in THIS process, so its pre-fix `<name>.<pid>.<tid>.tmp` name is computable
    exactly. It is also fail-silent, so the proof is the untouched victim, not a raised error."""
    root = _fresh_root()
    cwd = str(tmp_path / "proj")
    p = store.project_dir(cwd) / ".state.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    victim = tmp_path / "VICTIM.txt"
    victim.write_text(VICTIM, encoding="utf-8")
    if not _hardlink(_old_pid_tid_tmp(p), victim):
        pytest.skip("no privilege-free hardlink available on this filesystem")

    store.save_state(cwd, {"cursors": {"s-1": 3}, "sizes": {}, "turns": {}, "checkpoint_due": {}})

    assert victim.read_text(encoding="utf-8") == VICTIM, \
        "save_state wrote through the planted <name>.<pid>.<tid>.tmp hardlink"
    assert store.load_state(cwd)["cursors"].get("s-1") == 3, "state did not land at its destination"


def test_write_migration_marker_never_writes_through_the_old_predictable_temp(tmp_path):
    """The marker's pre-fix temp was `<name>.<pid>.<tid>.tmp` too."""
    _fresh_root()
    silo = tmp_path / "legacyproj-0123456789"
    silo.mkdir(parents=True, exist_ok=True)
    victim = tmp_path / "VICTIM.txt"
    victim.write_text(VICTIM, encoding="utf-8")
    marker = silo / store.MIGRATED_MARKER
    if not _hardlink(_old_pid_tid_tmp(marker), victim):
        pytest.skip("no privilege-free hardlink available on this filesystem")

    store.write_migration_marker(str(silo))

    assert victim.read_text(encoding="utf-8") == VICTIM, \
        "write_migration_marker wrote through the planted <name>.<pid>.<tid>.tmp hardlink"
    assert marker.exists() and "workspace layout" in marker.read_text(encoding="utf-8")


def test_an_ordinary_write_still_lands_through_atomic_write(tmp_path):
    """False-positive twin: mkstemp + replace must still perform the write, with no temp left over."""
    root = _fresh_root()
    root.mkdir(parents=True, exist_ok=True)
    store.save_channels({"lanes": {"main": "id-abcdef012345"}})
    assert json.loads(store.channels_path().read_text(encoding="utf-8"))["lanes"]["main"] \
        == "id-abcdef012345"
    assert not list(root.glob("channels.json.*.tmp")) and not list(root.glob("*.tmp")), \
        "a temp file leaked from atomic_write"


# ============================================================================================
# The migration marker is decided by lstat, never .exists() (which follows a link)
# ============================================================================================

def test_migrated_marker_state_reports_absent_present_and_tampered(tmp_path):
    silo = tmp_path / "silo"
    silo.mkdir()
    assert store.migrated_marker_state(str(silo)) == "absent", "no marker must read absent"

    (silo / store.MIGRATED_MARKER).write_text("done\n", encoding="utf-8")
    assert store.migrated_marker_state(str(silo)) == "present", "a real regular marker must read present"


@pytest.mark.skipif(os.name != "nt", reason="NTFS junctions are Windows-only")
def test_a_junctioned_marker_reads_tampered_not_present(tmp_path):
    """`.exists()` follows a link and would report a planted junction as 'already migrated', letting
    an attacker SUPPRESS a silo's migration. lstat sees the reparse point and refuses."""
    silo = tmp_path / "silo"
    silo.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    r = subprocess.run(["cmd", "/c", "mklink", "/J", str(silo / store.MIGRATED_MARKER), str(outside)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        pytest.skip("mklink /J unavailable")
    assert store.migrated_marker_state(str(silo)) == "tampered", \
        "a junctioned marker was accepted as a real completion marker"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
