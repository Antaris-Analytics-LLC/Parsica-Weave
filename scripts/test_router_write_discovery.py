"""RC5L-F: every Weave writer that reaches into Spirit's `.parsica_router` must prove containment.

Weave is a CLIENT of Spirit's store, and two of its code paths reach into that store's router
directory. Both were hardened on the Spirit side and neither was swept for here:

  * `write_identity_migration_receipt` built `<spirit_root>/.parsica_router/...` by bare
    concatenation and `mkdir(parents=True)`, so a junction planted on that directory put the
    receipt outside the store while the command reported success;
  * it then wrote through `<name>.<pid>.<tid>.tmp` with `write_text` - the exact predictable,
    non-exclusive, link-following temp pattern that was already fixed once in SyncLedger.save().
    A hardlink pre-planted at that path (no admin needed) makes the write land in an external file
    and the replace then installs it as the live receipt. A hardlink has no target to resolve, so
    no path check can see one: O_EXCL at creation is the only thing that stops it.

The discovery below is deliberately scoped to files that BUILD a router path AND mutate the
filesystem. A file that merely names the directory in an operator message (verify_salt.py prints
one to explain where a salt fingerprint lives) constructs nothing and writes nothing, so dragging it
into the population would train the next person to add exemptions - and an exemption list is how
trace.py stayed invisible on the Spirit side.
"""
import ast
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

_HERE = Path(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, str(_HERE))

_ROUTER = re.compile(r"parsica_router")
_MUTATES = re.compile(r"\bmkdir\(|\bwrite_text\(|\bmkstemp\(|\breplace\(|\bos\.open\(|\bopen\([^)]*[\"'][wa]")
_PROVES = re.compile(r"is_reparse_point|within\(|assert_contained_file|walk_contained")


def _docstrings(tree):
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                out.add(id(body[0].value))
    return out


def _builds_router_path(text):
    """A router reference in CODE, not in a docstring. Comments never reach the AST at all, so a
    file that only discusses the directory in prose is correctly invisible here."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return False
    docs = _docstrings(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and id(node) not in docs and _ROUTER.search(node.value):
            return True
    return False


def _population():
    out = []
    for p in sorted(_HERE.rglob("*.py")):
        if p.name.startswith("test_"):
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            continue
        if _builds_router_path(text) and _MUTATES.search(text):
            out.append((p, text))
    return out


def test_every_weave_router_writer_proves_containment():
    unguarded = [p.name for p, text in _population() if not _PROVES.search(text)]
    assert not unguarded, (
        "these Weave files build a path into Spirit's .parsica_router and write through it "
        "without proving containment: %r" % unguarded)


def test_the_discovery_is_not_vacuous():
    """Anti-vacuity twin. If the AST walk stops matching, the assertion above passes over an empty
    set forever - the precise failure mode that let trace.py ship."""
    found = {p.name for p, _ in _population()}
    assert "store.py" in found, (
        "discovery no longer reaches store.py's identity-migration receipt writer: %r" % found)


def test_a_prose_only_mention_is_not_dragged_into_the_population():
    """False-positive twin. verify_salt.py names the router in a help string and builds no path."""
    assert not _builds_router_path('"""docs mention .parsica_router/promotion_sync.json"""\n')
    assert not _builds_router_path("# comment about .parsica_router\nx = 1\n")
    assert _builds_router_path("p = root / '.parsica_router' / 'x.json'\n"), (
        "the discovery misses a real path construction")


# --- the receipt writer itself -----------------------------------------------------------------

def _junction(link, target):
    if os.name != "nt":
        return False
    Path(target).mkdir(parents=True, exist_ok=True)
    return subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                          capture_output=True, text=True).returncode == 0


@pytest.mark.skipif(os.name != "nt", reason="NTFS junctions are Windows-only")
def test_the_receipt_refuses_a_junctioned_router_and_writes_nothing_outside(tmp_path):
    import store
    spirit = tmp_path / "spirit"
    spirit.mkdir()
    outside = tmp_path / "outside"
    if not _junction(spirit / ".parsica_router", outside):
        pytest.skip("mklink /J unavailable")

    with pytest.raises(store.MigrationRefused):
        store.write_identity_migration_receipt(str(spirit), {"map": {}})
    assert not [p for p in outside.rglob("*") if p.is_file()], (
        "the identity-migration receipt landed outside the Spirit store")


@pytest.mark.skipif(os.name != "nt", reason="mklink /H is Windows-only")
def test_the_receipt_never_writes_through_a_planted_predictable_temp_name(tmp_path):
    import store
    import threading as _threading
    spirit = tmp_path / "spirit"
    rdir = spirit / ".parsica_router"
    rdir.mkdir(parents=True)
    victim = tmp_path / "VICTIM.txt"
    victim.write_text("PRECIOUS EXTERNAL DATA\n", encoding="utf-8")

    # the REAL pid/tid this process will use - a mismatched guess would make the test pass
    # vacuously whether or not the fix is present
    old_style = rdir / ("identity_migration_receipt.json.%s.%s.tmp"
                        % (os.getpid(), _threading.get_ident()))
    rc = subprocess.run(["cmd", "/c", "mklink", "/H", str(old_style), str(victim)],
                        capture_output=True, text=True)
    if rc.returncode != 0:
        pytest.skip("hardlink unavailable on this filesystem")

    store.write_identity_migration_receipt(str(spirit), {"map": {}})
    assert victim.read_text(encoding="utf-8") == "PRECIOUS EXTERNAL DATA\n", (
        "the receipt wrote through a planted predictable temp name")


def test_an_ordinary_receipt_write_still_works(tmp_path):
    """False-positive twin: the containment proof and the mkstemp conversion must not break the
    write this function exists to perform."""
    import store
    spirit = tmp_path / "spirit"
    spirit.mkdir()
    p = store.write_identity_migration_receipt(str(spirit), {"map": {"old": "new"}})
    assert p.exists()
    assert json.loads(p.read_text(encoding="utf-8")) == {"map": {"old": "new"}}
    assert not list((spirit / ".parsica_router").glob("*.tmp")), "a temp file leaked"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
