"""A refusal term must not ride a PROJECT LABEL onto an output surface.

SECURITY.md promises: "every output surface - query display, plan headers, session injects,
digests - passes the refusal fence before anything is shown, so a fenced term riding a label never
reaches a screen or transcript." This file is the test that claim owes.

WHY IT WAS MISSED. Every refusal term in this suite was a single alphanumeric token - ZZFENCETERMZZ,
ProjectNimbus, RZK, FW, ZK. Not one exercised the shape the DOCS TEACH: README and SKILL.md both
give `"refusal": ["ProjectX", "internal codename"]`, a phrase containing a space. That shape broke
the label fence, because project_key normalises every non-alphanumeric run to '-' BEFORE the fence
sees the name - so "Korra Succession" became "Korra-Succession", the literal no longer matched, and
the fence stayed silent. Worse than silent: the same term fenced correctly in prose, so an operator
had positive evidence it worked while the label leaked.

The vectors below are deliberately separator-VARIANT, because that is the whole defect: a term is
typed with spaces and a repo directory is named with hyphens, and the fence must not care which.
"""
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _isolated_store():
    root = tempfile.mkdtemp(prefix="labelfence_")
    os.environ["PARSICA_WEAVE_ROOT"] = root
    for mod in ("store", "scrub"):
        sys.modules.pop(mod, None)
    import store
    assert root in str(store.STORE_ROOT), "refusing to run against the live store"
    (store.STORE_ROOT / "projects").mkdir(parents=True, exist_ok=True)
    return store, root


def _set_terms(store, terms):
    store.GLOBAL_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    store.GLOBAL_CONFIG.write_text(json.dumps({"refusal": terms}), encoding="utf-8")
    time.sleep(0.01)          # distinct mtime_ns on a coarse clock, so the cache signature moves


def _key_for(store, root, dirname):
    d = os.path.join(root, "repos", dirname)
    os.makedirs(d, exist_ok=True)
    return store.project_key(d)


def run():
    fails = []
    store, root = _isolated_store()
    _prev = os.environ.get("PARSICA_WEAVE_ROOT")

    try:
        # --- the documented shape, in every separator a real repo might use --------------------
        _set_terms(store, ["Korra Succession"])
        for dirname in ("Korra Succession", "Korra-Succession", "korra_succession",
                        "KorraSuccession", "korra.succession", "KORRA-SUCCESSION"):
            key = _key_for(store, root, dirname)
            low = key.lower()
            if "korra" in low or "succession" in low:
                fails.append("LABEL[%s] fenced term rides the project key: %r" % (dirname, key))

        # --- a DECORATED spelling of the fenced ASCII term must fence too --------------------------
        # The operator fenced ASCII "Korra Succession". Two decoration classes must not let the
        # codename ride a label:
        #   - a combining-mark ACCENT (ó), folded by NFKD - drop the normalise and "korra" -> "krra".
        #   - a non-decomposing stroke LETTER (o-slash) that NFKD leaves whole and the strip would
        #     drop to nothing ("korra" -> "krra") - folded by _LATIN_FOLD. Drop that fold and the
        #     slug K-rra-... leaks, exactly the sibling an adversarial pass surfaced.
        for dirname in ("Kórra Succession", "Kórra-Succession", "kórrasuccession",
                        "Kørra Succession", "Kørra-Succession", "Korra Successiøn"):
            key = _key_for(store, root, dirname)
            low = key.lower()
            if "rra" in low or "succession" in low:
                fails.append("DECOR[%s] decorated fenced term rides the project key: %r" % (dirname, key))

        # --- FALSE-POSITIVE COMPANIONS: ordinary names keep their readable slug ----------------
        # Without these, a fence that returned "project" for everything would pass the block above.
        for dirname in ("ordinary-project", "parsica-weave", "my_app_2026"):
            key = _key_for(store, root, dirname)
            expect = dirname.replace("_", "-").replace(".", "-").lower()
            if not key.lower().startswith(expect.split("-")[0]):
                fails.append("LABEL[%s] over-fenced an unrelated name: %r" % (dirname, key))

        # --- the fallback must not collapse two projects onto one key -------------------------
        a = _key_for(store, root, "Korra Succession")
        b = _key_for(store, root, "Korra Succession Two")
        if a == b:
            fails.append("LABEL two distinct fenced projects collapsed onto one key: %r" % a)

        # --- a term added later must take effect (the union is cached; staleness would leak) ---
        before = _key_for(store, root, "Zephyr Protocol")
        _set_terms(store, ["Korra Succession", "Zephyr Protocol"])
        after = _key_for(store, root, "Zephyr Protocol")
        if before == after or "zephyr" in after.lower():
            fails.append("LABEL cached term union went stale - a newly fenced term still leaks: "
                         "before=%r after=%r" % (before, after))

        # --- and a term REMOVED later must stop fencing (cache invalidates both directions) ----
        _set_terms(store, [])
        restored = _key_for(store, root, "Zephyr Protocol")
        if not restored.lower().startswith("zephyr"):
            fails.append("LABEL cache did not notice a term being removed: %r" % restored)

        # --- FAIL CLOSED when the refusal configs cannot be read ------------------------------
        # If the union cannot be established (an unreadable projects/ tree), project_key must fall
        # back to the neutral name - not raise (which would break every command) and not leak a
        # name it could not check. Simulate the unreadable tree by making the union raise, and
        # confirm an ORDINARY name is neutralised, stays unique, and recovers once readable again.
        _real_union = store._refusal_union_cached

        def _boom():
            raise OSError("projects tree unreadable")

        store._refusal_union_cached = _boom
        try:
            k1 = _key_for(store, root, "ordinary-project")        # must NOT raise
            k2 = _key_for(store, root, "another-ordinary-one")
            if not k1.lower().startswith("project-"):
                fails.append("FAILCLOSED an unverifiable name kept its slug: %r" % k1)
            if k1 == k2:
                fails.append("FAILCLOSED two names collapsed onto one neutral key: %r" % k1)
        except Exception as e:
            fails.append("FAILCLOSED project_key raised instead of degrading: %r" % e)
        finally:
            store._refusal_union_cached = _real_union

        recovered = _key_for(store, root, "ordinary-project")
        if not recovered.lower().startswith("ordinary"):
            fails.append("FAILCLOSED did not recover once the union was readable: %r" % recovered)

    finally:
        if _prev is not None:
            os.environ["PARSICA_WEAVE_ROOT"] = _prev
        else:
            os.environ.pop("PARSICA_WEAVE_ROOT", None)
        for mod in ("store", "scrub"):
            sys.modules.pop(mod, None)

    if fails:
        print("FAIL - project label fence (%d)" % len(fails))
        for f in fails:
            print("  -", f)
        return 1
    print("OK - project label fence holds across separator variants")
    return 0


def test_project_label_fence():
    assert run() == 0, "project label fence failed - see captured stdout"


if __name__ == "__main__":
    sys.exit(run())
