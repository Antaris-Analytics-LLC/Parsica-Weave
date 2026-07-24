#!/usr/bin/env python3
"""RC5-H reminder: the SessionStart pending-promote line. Additive, informational, and bound by
hard rules - strictly READ-ONLY, MINT-FREE, silent at zero, and never a source of breakage.
Run: python test_promote_reminder.py

Skips cleanly if Parsica Spirit is not importable (pure-Weave install has nothing to remind about).
"""
import hashlib
import io
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _tree_hash(root):
    h = hashlib.sha256()
    for dp, dns, fns in os.walk(root):
        for name in sorted(fns):
            full = os.path.join(dp, name)
            h.update(os.path.relpath(full, root).replace("\\", "/").encode())
            h.update(b"\0")
            try:
                with open(full, "rb") as f:
                    h.update(f.read())
            except OSError:
                h.update(b"<unreadable>")
            h.update(b"\n")
    return h.hexdigest()


def run():
    try:
        from parsica_spirit.promote import SyncLedger, collect_candidates, salt_fingerprint
        from parsica_spirit.enrich._salt import resolve_salt
    except ImportError:
        print("ok   (skip) Parsica Spirit not installed - no promote reminder to test")
        return 0

    weave = tempfile.mkdtemp(prefix="rem_weave_")
    spirit = tempfile.mkdtemp(prefix="rem_spirit_")
    os.environ["PARSICA_WEAVE_ROOT"] = weave
    os.environ["PARSICA_SPIRIT_ROOT"] = spirit
    import store
    import pcx

    fails = []

    def ck(c, m):
        print(("ok   " if c else "FAIL ") + m)
        if not c:
            fails.append(m)

    cwd = weave
    sid = store.workspace_id(create=True)
    # one promotable lesson -> exactly one candidate
    lp = store.lessons_path()
    os.makedirs(os.path.dirname(str(lp)), exist_ok=True)
    with open(str(lp), "w", encoding="utf-8") as f:
        f.write("# Lessons\n\n## 2026-07-17 - retry cap\n\nThe retry cap is 60 seconds.\n")

    scope_dir = os.path.join(spirit, sid)
    os.makedirs(scope_dir, exist_ok=True)             # store EXISTS but nothing promoted -> pending
    cfg = store.load_config(cwd)                       # compute once (any config scaffold happens now)

    # --- A) pending nonzero (saltless store): line appears, names count + review command ---
    line = pcx._pending_promote_line(cwd, cfg)
    ck("awaiting promote" in line and "1 item" in line and "promote --dry-run" in line,
       "pending>0 -> one line naming the count and the review command")
    ck("Do not promote without their" in line,
       "line is surface-to-user (ask first), not a do-it instruction")

    # --- D) MINT-FREE: counting on a saltless store created no .salt ---
    ck(not os.path.exists(os.path.join(spirit, ".salt")),
       "counting minted NO salt file on a saltless store")

    # --- C) READ-ONLY: neither the Spirit floor nor the Weave store changed across the call ---
    hs, hw = _tree_hash(spirit), _tree_hash(weave)
    _ = pcx._pending_promote_line(cwd, cfg)
    ck(_tree_hash(spirit) == hs, "Spirit store byte-identical before/after the reminder")
    ck(_tree_hash(weave) == hw, "Weave store byte-identical before/after the reminder")

    # --- E) off-switch mutes it ---
    cfg_off = dict(cfg); cfg_off["promote_reminder"] = False
    ck(pcx._pending_promote_line(cwd, cfg_off) == "", "promote_reminder=false -> silent")

    # --- B) silent at zero: record the candidate in the ledger (matching revision) -> nothing pending
    led = SyncLedger(scope_dir, sid)
    cands = collect_candidates(store.project_dir(cwd), scope_id=sid,
                               source_project=store.project_key(cwd),
                               refusal=store.workspace_refusal_terms(),
                               workspace_root=str(store.STORE_ROOT))
    for c in cands:
        led._data["records"][c.ledger_key] = {
            "pk": c.provenance_key, "current_revision": c.revision, "state": "active"}
    # a store that has promoted records is realistically on-scheme (v2); stamp it so the shared
    # legacy-v1 preflight gate (v1 + live records -> must migrate) does not fire on this fixture.
    led.stamp_version()
    ck(pcx._pending_promote_line(cwd, cfg) == "", "all candidates already promoted -> silent at zero")

    # --- F) graceful: no Spirit store dir at all -> silent, no breakage ---
    spirit2 = tempfile.mkdtemp(prefix="rem_none_")
    os.environ["PARSICA_SPIRIT_ROOT"] = spirit2        # exists but no scope dir
    ck(pcx._pending_promote_line(cwd, cfg) == "", "no promoted store yet -> silent")
    os.environ["PARSICA_SPIRIT_ROOT"] = spirit          # restore

    # --- G) preflight refusal surfaces its remedy instead of a count ---
    salt = resolve_salt(spirit, create=True)            # now a real salt + stamped v2 ledger
    led2 = SyncLedger(scope_dir, sid); led2.stamp_version()
    with open(os.path.join(spirit, ".salt"), "w", encoding="utf-8") as f:
        f.write(os.urandom(32).hex())                   # replace the salt -> fingerprint mismatch
    gline = pcx._pending_promote_line(cwd, cfg)
    ck(gline.startswith("Parsica: promote is BLOCKED") and "salt" in gline.lower(),
       "salt-mismatch store -> reminder surfaces the guard's remedy, not a count")

    # --- H) end-to-end: the line rides in the SessionStart inject, before the reference caveat ---
    os.remove(os.path.join(spirit, ".salt")); f = open(os.path.join(spirit, ".salt"), "w"); f.write(salt.hex()); f.close()
    led3 = SyncLedger(scope_dir, sid); led3._data["records"] = {}; led3.save()   # make it pending again
    out, old = io.StringIO(), sys.stdout
    sys.stdout = out
    try:
        pcx.handle_session_start({"cwd": cwd, "session_id": "testsess"})
    except SystemExit:
        pass
    finally:
        sys.stdout = old
    emitted = out.getvalue()
    ck("awaiting promote" in emitted, "SessionStart inject carries the promote reminder")
    ck(emitted.find("awaiting promote") < emitted.find("reference context only"),
       "reminder rides in the operational banner, ABOVE the reference-only caveat")

    print(("\nALL PASSED" if not fails else "\nFAILURES: " + "; ".join(fails)))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(run())
