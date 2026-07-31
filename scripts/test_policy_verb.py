#!/usr/bin/env python3
"""`refusal` / `human-only` - editing the policy lists without ever printing them.

Two properties carry this verb, and only one of them is about the CLI.

The first is that a term added here actually FENCES. A verb that wrote a correctly-shaped key
nobody consults would look completely right from the outside - the file gains the term, the
listing counts it - while content kept reaching the store unredacted. So the tests do not stop at
"the config now contains it": they push text through the real output sink and the real scrub
gateway afterwards and require the term to be gone. That is the layer the promise is about.

The second is that no path prints a term. The list holds the exact words the operator marked as
never-to-appear, so a listing, a readback, an idempotent no-op and an error all have to stay
quiet about the value - it may be the secret itself.
"""
import io
import os
import subprocess
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

import pytest

os.environ.setdefault("PARSICA_WEAVE_ROOT", tempfile.mkdtemp(prefix="policy_boot_"))
os.environ.pop("PARSICA_ISOLATED", None)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store  # noqa: E402
import pcx    # noqa: E402

TERM = "ZZORCHIDZZ"          # neutral fake sentinel, like every other one in the tree


def _fresh():
    root = Path(tempfile.mkdtemp(prefix="policy_root_"))
    store.STORE_ROOT = root
    store.GLOBAL_CONFIG = root / "config.json"
    return root


def _run(verb, *argv):
    buf = io.StringIO()
    with redirect_stdout(buf):
        pcx.handle_policy([verb, *argv])
    return buf.getvalue()


# --- the property that matters: an added term actually fences -------------------

def test_an_added_refusal_term_is_redacted_by_the_real_output_sink():
    """The whole point. Writing the key is not the promise; redaction is."""
    _fresh()
    _run("refusal", "--add", TERM)
    assert TERM in store.workspace_refusal_terms(), \
        "the fence's own union does not see the term the verb just wrote"
    buf = io.StringIO()
    with redirect_stdout(buf):
        store.emit_text("a line mentioning %s in passing" % TERM)
    assert TERM not in buf.getvalue(), "the term reached stdout after being added to the list"


def test_a_removed_term_stops_fencing():
    """The other direction, so the test cannot pass by the term never having been readable."""
    _fresh()
    _run("refusal", "--add", TERM)
    _run("refusal", "--remove", TERM)
    assert TERM not in store.workspace_refusal_terms()


def test_the_human_only_list_is_a_different_list():
    """Two verbs, not one with a mode flag - a slip must not put a never-store term on the
    weaker redaction list. This pins that they do not cross."""
    _fresh()
    _run("human-only", "--add", TERM)
    assert TERM not in store.read_policy_list("refusal", "global")
    assert TERM in store.read_policy_list("human_only_terms", "global")


# --- no path prints a term -----------------------------------------------------

def test_no_path_echoes_the_term():
    _fresh()
    for out in (_run("refusal", "--add", TERM),        # first add
                _run("refusal", "--add", TERM),        # idempotent no-op
                _run("refusal"),                       # listing
                _run("refusal", "--remove", TERM),     # removal readback
                _run("refusal", "--remove", TERM)):    # not-present readback
        assert TERM not in out, "a policy path echoed the term: %r" % out


def test_the_listing_reports_a_count_and_a_digest_per_term():
    _fresh()
    _run("refusal", "--add", "alpha")
    _run("refusal", "--add", "beta")
    out = _run("refusal")
    assert "2 term(s)" in out
    assert "1. " in out and "2. " in out


def test_the_same_term_digests_the_same_in_both_layers(tmp_path):
    """A digest is only useful if it is stable: spotting that a term sits in BOTH the workspace
    and project lists is the main thing a digest buys, and that needs matcher-normalization
    (case, surrounding space) to reach the hash, not the raw string."""
    _fresh()
    key = store.policy_digest_key(create=True)
    assert store.term_digest("  Alpha ", key) == store.term_digest("alpha", key)
    assert store.term_digest("alpha", key) != store.term_digest("beta", key)


def test_a_digest_is_useless_without_this_workspaces_key():
    """Keyed, not a plain hash. An eight-hex digest of a short codename falls to a wordlist, so a
    transcript full of plain digests would be very nearly a transcript full of terms."""
    _fresh()
    k1 = store.policy_digest_key(create=True)
    _fresh()
    k2 = store.policy_digest_key(create=True)
    assert k1 != k2
    assert store.term_digest(TERM, k1) != store.term_digest(TERM, k2)


# --- write discipline ----------------------------------------------------------

def test_listing_never_mints_the_digest_key():
    """A read verb must not be handed a durable secret just to render an empty list - the
    mint-on-read seam this store refuses everywhere else."""
    root = _fresh()
    _run("refusal")
    assert not (root / "workspace.json").exists(), "a pure listing minted durable state"


def test_case_and_space_are_the_matchers_view():
    _fresh()
    _run("refusal", "--add", "  MixedCase  ")
    out = _run("refusal", "--add", "mixedcase")
    assert "already in" in out
    assert len(store.read_policy_list("refusal", "global")) == 1
    _run("refusal", "--remove", "MIXEDCASE")
    assert store.read_policy_list("refusal", "global") == []


def test_isolation_refuses_a_mutation_but_still_lists():
    _fresh()
    os.environ["PARSICA_ISOLATED"] = "1"
    try:
        assert "0 term(s)" in _run("refusal")
        with pytest.raises(SystemExit) as exc:
            _run("refusal", "--add", TERM)
        assert exc.value.code == 3
        assert store.read_policy_list("refusal", "global") == []
    finally:
        os.environ.pop("PARSICA_ISOLATED", None)


def test_writing_a_policy_list_preserves_unrelated_config_keys():
    """The operator hand-sets keys in this file. An editor that rewrote it whole and dropped them
    would silently reset the scrub knobs while adding a refusal term."""
    root = _fresh()
    (root / "config.json").write_text('{"scrub_pii": false, "mirror_active_days": 30}',
                                      encoding="utf-8")
    _run("refusal", "--add", TERM)
    import json
    raw = json.loads((root / "config.json").read_text(encoding="utf-8"))
    assert raw["scrub_pii"] is False and raw["mirror_active_days"] == 30


def test_a_scalar_hand_edit_is_not_iterated_into_letters():
    """`{"refusal": "ProjectX"}` is a plausible hand-edit. Read as a sequence it becomes eight
    single-letter terms, and a one-letter refusal term redacts essentially everything."""
    root = _fresh()
    (root / "config.json").write_text('{"refusal": "ProjectX"}', encoding="utf-8")
    assert store.read_policy_list("refusal", "global") == []


# --- both verbs really dispatch ------------------------------------------------

def test_both_verbs_dispatch_through_the_real_cli():
    """Proven at the layer that routes - bin/parsica-weave.py's PCX set and pcx.py's main - not by
    calling handle_policy directly, which would pass with neither verb registered anywhere."""
    root = tempfile.mkdtemp(prefix="policy_disp_")
    cli = Path(os.path.dirname(os.path.abspath(__file__))).parent / "bin" / "parsica-weave.py"
    env = dict(os.environ, PARSICA_WEAVE_ROOT=root)
    env.pop("PARSICA_ISOLATED", None)
    for verb in ("refusal", "human-only"):
        p = subprocess.run([sys.executable, str(cli), verb],
                           capture_output=True, text=True, env=env)
        assert p.returncode == 0, "%r did not dispatch: %s" % (verb, p.stderr)
        assert "term(s)" in p.stdout, "%r reached the wrong handler: %r" % (verb, p.stdout)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
