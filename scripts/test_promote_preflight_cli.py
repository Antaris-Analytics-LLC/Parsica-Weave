#!/usr/bin/env python3
"""RC5-H: the promote CLI refuses (exit 4), before any write, when the Spirit store cannot be
safely promoted - a lost/replaced salt or a version-too-new ledger, either of which would
otherwise silently fork the whole floor. This covers the CLI WIRING (import + call + message +
no-spurious-salt-mint); the guard logic itself is unit-tested in Spirit's test_promote_preflight.
Run: python test_promote_preflight_cli.py

Skips cleanly if Parsica Spirit is not importable (pure-Weave install has no promote to guard).
"""
import io
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def run():
    try:
        from parsica_spirit.promote import SyncLedger, salt_fingerprint
        from parsica_spirit.enrich._salt import resolve_salt
    except ImportError:
        print("ok   (skip) Parsica Spirit not installed - no promote path to guard")
        return 0

    weave_root = tempfile.mkdtemp(prefix="pf_weave_")
    spirit_root = tempfile.mkdtemp(prefix="pf_spirit_")
    os.environ["PARSICA_WEAVE_ROOT"] = weave_root
    os.environ["PARSICA_SPIRIT_ROOT"] = spirit_root
    import store
    import pcx

    fails = []

    def ck(c, m):
        print(("ok   " if c else "FAIL ") + m)
        if not c:
            fails.append(m)

    # --- build a healthy v2 Spirit store: minted salt + stamped ledger under the workspace id ---
    sid = store.workspace_id()
    scope_dir = os.path.join(spirit_root, sid)
    os.makedirs(scope_dir, exist_ok=True)
    salt = resolve_salt(spirit_root, create=True)          # <spirit_root>/.salt
    led = SyncLedger(scope_dir, sid)
    led.stamp_version()                                    # version=2 + salt_fp = fp(salt)
    salt_path = os.path.join(spirit_root, ".salt")

    def promote_dry():
        """Run the CLI promote --dry-run; return (exit_code, output). exit 0 == no SystemExit."""
        out, old = io.StringIO(), sys.stdout
        sys.stdout = out
        code = 0
        try:
            pcx.handle_promote(["promote", "--dry-run", "--cwd", weave_root])
        except SystemExit as e:
            code = int(e.code or 0)
        finally:
            sys.stdout = old
        return code, out.getvalue()

    # --- control: healthy salt promotes (does NOT refuse) ---
    code, out = promote_dry()
    ck(code != 4 and "refused" not in out, "healthy v2 store is not refused (exit %d)" % code)

    # --- salt REPLACED with a foreign one: refuse, name the remedy, mint NO replacement ---
    with open(salt_path, "w", encoding="utf-8") as f:
        f.write(os.urandom(32).hex())
    code, out = promote_dry()
    low = out.lower()
    ck(code == 4, "replaced salt -> exit 4 (got %d)" % code)
    ck("refused" in low and "salt" in low and "fork" in low and "restore" in low,
       "replaced salt -> refusal names the fork risk and the remedy")
    ck("verify_salt.py" in out, "refusal points at verify_salt.py")

    # --- salt LOST entirely: refuse, and do NOT mint a spurious replacement ---
    os.remove(salt_path)
    code, out = promote_dry()
    ck(code == 4, "lost salt -> exit 4 (got %d)" % code)
    ck(not os.path.exists(salt_path), "lost salt -> CLI minted NO spurious .salt (refused first)")

    # --- restore the correct salt: refusal lifts ---
    with open(salt_path, "w", encoding="utf-8") as f:
        f.write(salt.hex())
    code, out = promote_dry()
    ck(code != 4, "restoring the right salt lifts the refusal (exit %d)" % code)

    # --- version-too-new: refuse with an upgrade message, and NO salt hint (wrong knob) ---
    led2 = SyncLedger(scope_dir, sid)
    led2._data["version"] = 99
    led2.save()
    code, out = promote_dry()
    low = out.lower()
    ck(code == 4 and "newer" in low and "upgrade" in low,
       "version-too-new -> exit 4 with an upgrade message (exit %d)" % code)
    ck("verify_salt.py" not in out,
       "version-too-new refusal does NOT carry the salt-verify hint")
    led3 = SyncLedger(scope_dir, sid)
    led3._data["version"] = 2
    led3.save()

    # --- mint-seam guard: a REAL promote under an unpersistable salt refuses, forks nothing ---
    spirit2 = tempfile.mkdtemp(prefix="pf_spirit2_")      # fresh store, no salt_fp stamped yet
    os.environ["PARSICA_SPIRIT_ROOT"] = spirit2
    os.makedirs(os.path.join(spirit2, ".salt"))           # a directory squats on the salt path
    out2, old = io.StringIO(), sys.stdout
    sys.stdout = out2
    code = 0
    try:
        pcx.handle_promote(["promote", "--cwd", weave_root])       # REAL promote, not dry-run
    except SystemExit as e:
        code = int(e.code or 0)
    finally:
        sys.stdout = old
    low2 = out2.getvalue().lower()
    ck(code == 4 and "persist" in low2,
       "unpersistable salt -> real promote refuses at the mint seam (exit %d)" % code)
    scope2 = os.path.join(spirit2, sid)
    ck(not os.path.exists(os.path.join(scope2, "records.jsonl")),
       "mint-seam refusal wrote NO records")

    # --- mint-seam guard, migrate sibling: re-key under an unpersistable salt refuses ---
    os.makedirs(scope2, exist_ok=True)
    led4 = SyncLedger(scope2, sid)                        # v1 ledger with one active entry
    led4._data["records"]["weave:%s:aa" % sid] = {"pk": "aa" * 8, "state": "active"}
    led4.save()
    out3, old = io.StringIO(), sys.stdout
    sys.stdout = out3
    code = 0
    try:
        pcx.handle_promote(["promote", "--migrate-identity", "--cwd", weave_root])
    except SystemExit as e:
        code = int(e.code or 0)
    finally:
        sys.stdout = old
    low3 = out3.getvalue().lower()
    ck(code == 4 and "persist" in low3,
       "unpersistable salt -> migrate-identity refuses before re-keying (exit %d)" % code)

    # --- M4 version skew (Weave plugin NEWER than the installed Spirit wheel): every identity op
    #     REFUSES with an upgrade message, never falls back to a path that can mint/re-key. Simulated
    #     by removing the RC5-I seam so the CLI's feature-detect sees a pre-RC5-I Spirit. The healthy
    #     control at the top of this suite is the false-positive companion (a matched Spirit promotes).
    import parsica_spirit.promote as _pm
    _saved_rip = _pm.resolve_identity_plan
    try:
        del _pm.resolve_identity_plan                      # the feature-detect now yields None (old Spirit)
        # promote under skew, on the HEALTHY v2 store: refuse, do not mint/re-key
        os.environ["PARSICA_SPIRIT_ROOT"] = spirit_root
        outs, old = io.StringIO(), sys.stdout
        sys.stdout = outs
        code = 0
        try:
            pcx.handle_promote(["promote", "--cwd", weave_root])
        except SystemExit as e:
            code = int(e.code or 0)
        finally:
            sys.stdout = old
        lows = outs.getvalue().lower()
        ck(code == 4 and "newer" in lows and "upgrade" in lows,
           "version skew -> promote REFUSES with an upgrade message, no mint (exit %d)" % code)
        ck("verify_salt.py" not in outs.getvalue(),
           "the skew refusal carries no salt-verify hint (the remedy is upgrade, not restore)")

        # migrate-identity sibling under the same skew: a v1 store refuses to re-key, upgrade first
        spirit3 = tempfile.mkdtemp(prefix="pf_spirit3_")
        os.environ["PARSICA_SPIRIT_ROOT"] = spirit3
        s3scope = os.path.join(spirit3, sid)
        os.makedirs(s3scope, exist_ok=True)
        led5 = SyncLedger(s3scope, sid)                    # a v1 ledger with a live entry to re-key
        led5._data["records"]["weave:%s:bb" % sid] = {"pk": "bb" * 8, "state": "active"}
        led5.save()
        outm, old = io.StringIO(), sys.stdout
        sys.stdout = outm
        code = 0
        try:
            pcx.handle_promote(["promote", "--migrate-identity", "--cwd", weave_root])
        except SystemExit as e:
            code = int(e.code or 0)
        finally:
            sys.stdout = old
        lowm = outm.getvalue().lower()
        ck(code == 4 and "newer" in lowm and "upgrade" in lowm,
           "version skew -> migrate-identity REFUSES with an upgrade message (exit %d)" % code)
    finally:
        _pm.resolve_identity_plan = _saved_rip             # restore for any later assertions
        os.environ["PARSICA_SPIRIT_ROOT"] = spirit_root

    # --- the named remedy tool ships next to pcx.py, generic and leak-free ---
    here = os.path.dirname(os.path.abspath(__file__))
    vs = os.path.join(here, "verify_salt.py")
    ck(os.path.exists(vs), "verify_salt.py ships in scripts/ (the refusal names it)")
    body = open(vs, encoding="utf-8").read() if os.path.exists(vs) else ""
    ck("ad900b23" not in body and "Antaris" not in body and "C:/Users" not in body,
       "shipped verify_salt.py carries no operator-specific fingerprint or path")

    print(("\nALL PASSED" if not fails else "\nFAILURES: " + "; ".join(fails)))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(run())
