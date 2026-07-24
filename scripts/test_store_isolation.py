#!/usr/bin/env python3
"""Store-level regression tests for GLOBAL isolation + private-file hardening.

  * ISOLATION (the blocker): workspace_id() is the shared scope-id minter every standalone writer
    imports. A read-only isolated session (PARSICA_ISOLATED=1) - and any pure read path passing
    create=False (e.g. query federation) - must NEVER mint durable identity. Under isolation on a
    fresh root, workspace_id() must return "" and leave ZERO files behind (not even the sidecar
    lock). An EXISTING id stays readable in every mode (reads are always fine).
  * PERMS (hardening, POSIX-guarded): the raw write helpers land content owner-only (0600) under
    owner-only (0700) parents. chmod/geteuid are POSIX-only, so those checks SKIP on Windows,
    where NTFS ACLs already scope to the owning user (the _chmod helper no-ops there).

Written RED against the pre-fix code (workspace_id() minted workspace.json + workspace.json.lock
under isolation). Run: python test_store_isolation.py
"""
import os
import sys
import tempfile
from pathlib import Path

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPTS)
# Pin a throwaway root BEFORE importing store, so no run can ever bind STORE_ROOT to the real
# HOME store at import time; each test then points store.STORE_ROOT at its own fresh temp dir.
os.environ.setdefault("PARSICA_WEAVE_ROOT", tempfile.mkdtemp(prefix="store_iso_boot_"))
os.environ.pop("PARSICA_ISOLATED", None)
import store

fails = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        fails.append(msg)


def _fresh_root(prefix):
    """A brand-new empty store root, bound as store.STORE_ROOT for the call under test."""
    root = Path(tempfile.mkdtemp(prefix=prefix))
    store.STORE_ROOT = root
    return root


def isolation():
    # 1) isolated + fresh root: return "" and write NOTHING (no workspace.json, no .lock)
    os.environ["PARSICA_ISOLATED"] = "1"
    try:
        root = _fresh_root("store_iso_iso_")
        wid = store.workspace_id()
        check(wid == "", "ISOLATION: workspace_id() returns '' under PARSICA_ISOLATED=1 (got %r)" % wid)
        check(os.listdir(root) == [],
              "ISOLATION: nothing is written under isolation (listdir=%r)" % os.listdir(root))
    finally:
        os.environ.pop("PARSICA_ISOLATED", None)

    # 2) not isolated, but create=False (a pure read path must not mint either)
    root = _fresh_root("store_iso_cf_")
    wid = store.workspace_id(create=False)
    check(wid == "", "ISOLATION: workspace_id(create=False) returns '' when it would mint (got %r)" % wid)
    check(os.listdir(root) == [],
          "ISOLATION: create=False writes nothing on a fresh root (listdir=%r)" % os.listdir(root))

    # 3) normal (create=True, not isolated): mints a valid id, then it is READABLE in every mode
    root = _fresh_root("store_iso_mint_")
    wid = store.workspace_id()
    check(bool(store._WORKSPACE_ID_RE.match(wid or "")),
          "ISOLATION: a normal session mints a valid 24-hex id (got %r)" % wid)
    check(store.workspace_id(create=False) == wid,
          "ISOLATION: an existing id is returned even with create=False (reads are always fine)")
    os.environ["PARSICA_ISOLATED"] = "1"
    try:
        check(store.workspace_id() == wid,
              "ISOLATION: an existing id is returned even under isolation (reads are always fine)")
    finally:
        os.environ.pop("PARSICA_ISOLATED", None)


def _mode(path):
    return os.stat(path).st_mode & 0o777


def perms():
    # POSIX-only: chmod/geteuid do not exist on Windows, where the _chmod helper no-ops and NTFS
    # ACLs already scope to the owner. Skip cleanly there, like the symlink guard test does.
    if not hasattr(os, "geteuid"):
        print("SKIP perms assertions (no POSIX chmod/geteuid on this platform)")
        return
    root = _fresh_root("store_iso_perms_")

    # _raw_append: a NEW file lands 0600 under a 0700 parent (the parent is freshly created here)
    ap = root / "mirror" / "k" / "day.jsonl"
    store._raw_append(ap, "line one\n")
    check(_mode(ap) == 0o600, "PERMS: _raw_append creates a 0600 file (got %o)" % _mode(ap))
    check(_mode(ap.parent) == 0o700, "PERMS: _raw_append's parent dir is 0700 (got %o)" % _mode(ap.parent))
    # append to the same file keeps it 0600
    store._raw_append(ap, "line two\n")
    check(_mode(ap) == 0o600, "PERMS: a second _raw_append keeps the file 0600 (got %o)" % _mode(ap))

    # _raw_write: the published (replaced) file is 0600 under a 0700 parent
    wp = root / "working" / "doc.md"
    store._raw_write(wp, "hello\n")
    check(_mode(wp) == 0o600, "PERMS: _raw_write publishes a 0600 file (got %o)" % _mode(wp))
    check(_mode(wp.parent) == 0o700, "PERMS: _raw_write's parent dir is 0700 (got %o)" % _mode(wp.parent))


import types


def _inject_fake_module(name, **attrs):
    """Register a throwaway module under `name` so a guarded lazy import resolves to it. Returns
    a (name, previous) pair for restoration. Spirit is an OPTIONAL dependency often absent on the
    test box; these fakes let the read-path wiring be exercised without a real Parsica Spirit."""
    prev = sys.modules.get(name)
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod
    return name, prev


def _restore(saved):
    for name, prev in saved:
        if prev is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = prev


def query_federation_read_only():
    """Blocker completion: query federation is a pure READ - it must call workspace_id(create=False)
    and NEVER mint durable identity. With a fake Spirit router present (so the guarded lazy import
    SUCCEEDS and the create=False path is actually reached), a federated query on a FRESH workspace
    (no minted id) degrades to Weave-only AND leaves ZERO files behind. RED against the pre-fix code,
    which called workspace_id() (create=True) and minted workspace.json on the read path."""
    os.environ.pop("PARSICA_ISOLATED", None)
    root = _fresh_root("store_iso_fed_")
    saved = [
        _inject_fake_module("parsica_spirit"),
        _inject_fake_module("parsica_spirit.router",
                             federated_hits=lambda *a, **k: [{"id": "d1"}],
                             layer_order=lambda q: ("weave", "spirit"),
                             federated_recent=lambda *a, **k: [{"id": "r1"}]),
    ]
    try:
        import query
        args = types.SimpleNamespace(grep="foo", limit=5, hours=12.0, recent=5,
                                     all_sessions=False, sess=None)
        hits, order, tr = query._spirit_federation(args)
        check(hits == [] and tr is None,
              "FED read-only: a fresh workspace (no id) degrades to Weave-only (hits=%r tr=%r)" % (hits, tr))
        check(query._spirit_recent(args) == [],
              "FED read-only: --recent federation degrades on a fresh workspace (no id)")
        check(os.listdir(root) == [],
              "FED read-only: a federated READ mints NOTHING on a fresh workspace (listdir=%r)"
              % os.listdir(root))
    finally:
        _restore(saved)


def query_trace_isolation():
    """query tracing appends to Spirit's trace dir - a DURABLE side-effect. Under isolation
    _emit_query_trace must be a no-op (return before it ever imports/calls emit_trace). RED against
    the pre-fix code, which emitted a trace even in a zero-footprint session."""
    calls = []
    saved = [
        _inject_fake_module("parsica_spirit"),
        _inject_fake_module("parsica_spirit.trace",
                            emit_trace=lambda *a, **k: calls.append(1),
                            exposure=lambda *a, **k: {},
                            query_hash=lambda q: "h"),
    ]
    try:
        import query
        tr = {"root": "/tmp/does-not-matter", "key": "k"}
        args = types.SimpleNamespace(grep="foo")
        os.environ.pop("PARSICA_ISOLATED", None)
        query._emit_query_trace(tr, args, [], [], set(), ("weave", "spirit"))
        check(calls == [1], "TRACE: a non-isolated query emits a router trace (control)")
        os.environ["PARSICA_ISOLATED"] = "1"
        try:
            query._emit_query_trace(tr, args, [], [], set(), ("weave", "spirit"))
            check(calls == [1], "TRACE: an isolated session emits NO router trace (durable side-effect)")
        finally:
            os.environ.pop("PARSICA_ISOLATED", None)
    finally:
        _restore(saved)


def run():
    isolation()
    perms()
    query_federation_read_only()
    query_trace_isolation()
    print("\n" + ("ALL STORE ISOLATION TESTS PASSED (green)" if not fails
                  else "RED: %d failing -> %s" % (len(fails), " | ".join(fails))))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(run())
