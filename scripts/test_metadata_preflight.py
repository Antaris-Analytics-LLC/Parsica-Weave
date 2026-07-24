"""RC5L-F: a legacy silo's own metadata is validated BEFORE the first migration effect.

Two demonstrated harms, one root cause - the metadata that decides how a migration behaves was
validated at the END of that migration:

  * config.json holds the operator's refusal policy. A field that is present but malformed
    (`{"refusal": "ProjectHydra"}`) normalized to NO policy, every document was then activated
    unfenced, the emptied policy was written out and REPORTED AS VALIDATED, and the completion
    marker was stamped. The protected term landed verbatim in the live mirror and working set, the
    legacy sources were consumed, and the marker guaranteed no re-run would ever revisit it.

  * .state.json holds machine state. `{"cursors": 0}` survived load_state's setdefault untouched
    and reached _flush_mirror's `state["cursors"].get(...)`; an int cannot answer .get(), the Stop
    hook's fail-silent boundary swallowed the AttributeError, and capture stopped for that project
    with no operator-visible signal - marker already written, so no retry.

Every refusal test here has a legitimate-default twin: a guard that cannot pass a valid document is
not a guard, it is an outage.
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

_HERE = Path(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, str(_HERE))

KEY = "legacyproj-0123456789"
TERM = "ProjectHydra"          # the operator's protected codename, as in the review's reproduction


def _silo(tmp, config, state=None):
    """A legacy silo whose working set and mirror both carry the protected term."""
    silo = Path(tmp) / KEY
    (silo / "mirror").mkdir(parents=True)
    (silo / "working.md").write_text(
        "# Working set\n\n## Pinned facts\n- %s release credentials are in vault\n" % TERM,
        encoding="utf-8")
    (silo / "mirror" / "2026-07-01.4242.jsonl").write_text(
        json.dumps({"ts": "2026-07-01T00:00:00Z", "text": "%s launch sequence" % TERM}) + "\n",
        encoding="utf-8")
    (silo / "config.json").write_text(json.dumps(config), encoding="utf-8")
    if state is not None:
        (silo / ".state.json").write_text(json.dumps(state), encoding="utf-8")
    return silo


def _migrate(tmp):
    env = dict(os.environ, PARSICA_WEAVE_ROOT=str(tmp), PYTHONIOENCODING="utf-8")
    env.pop("PARSICA_ISOLATED", None)
    p = subprocess.run([sys.executable, str(_HERE / "pcx.py"), "migrate"],
                       capture_output=True, encoding="utf-8", errors="replace", env=env)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def _active_text(tmp, include_policy=True):
    """Every byte on an ACTIVE surface: the workspace tree minus the legacy silo, which migrate
    preserves on purpose. A recursive sweep rather than a list of files migrate claims it wrote -
    the point is to catch the term on a surface nobody enumerated.

    include_policy=False additionally drops the active config.json. A refusal term is SUPPOSED to
    appear there - that document is the fence's definition, and import_metadata_json keeps policy
    fields verbatim precisely so the fence does not redact itself into uselessness. When the
    question is "did protected CONTENT leak", the policy file is the one place a hit means the
    opposite of a leak."""
    blob = []
    for p in sorted(Path(tmp).rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(tmp).as_posix()
        if rel.startswith(KEY + "/"):
            continue
        if not include_policy and p.name == "config.json":
            continue
        try:
            blob.append("%s\n%s" % (rel, p.read_text(encoding="utf-8", errors="replace")))
        except OSError:
            pass
    return "\n".join(blob)


# --- config.json: a malformed fence is an UNKNOWN fence, never an empty one -------------------

@pytest.mark.parametrize("bad", [
    pytest.param("ProjectHydra", id="scalar-string"),
    pytest.param({"term": "ProjectHydra"}, id="object"),
    pytest.param(7, id="scalar-int"),
])
def test_a_malformed_policy_refuses_the_silo_with_zero_effects_and_no_marker(tmp_path, bad):
    """The review's headline reproduction, asserted on all five surfaces it named."""
    tmp = str(tmp_path)
    silo = _silo(tmp, {"refusal": bad})
    rc, out = _migrate(tmp)

    assert "REFUSED" in out, out
    assert "NO MARKER" in out, out

    # 1. no active mirror row, 2. no active working content - the term reached NO active surface
    assert TERM not in _active_text(tmp), "the protected term reached an active surface"
    # 3. no active config
    assert not (Path(tmp) / "projects" / KEY / "config.json").exists()
    # 4. no consumed legacy source: working.md is the detection signature and must survive intact
    assert (silo / "working.md").exists(), "the legacy working set was consumed by a refused run"
    assert TERM in (silo / "working.md").read_text(encoding="utf-8")
    assert not list(silo.glob("*.migrated")), "a legacy source was consumed despite the refusal"
    # 5. no marker - so the operator can fix the document and re-run
    import store
    assert not (silo / store.MIGRATED_MARKER).exists()


def test_a_valid_policy_list_migrates_normally_and_still_redacts_the_term(tmp_path):
    """The false-positive twin the review asked for by name. A well-formed fence must MIGRATE, and
    the term must be redacted on the active surface rather than refused off it."""
    tmp = str(tmp_path)
    silo = _silo(tmp, {"refusal": [TERM]})
    rc, out = _migrate(tmp)

    assert "REFUSED" not in out, out
    assert "done: 1 migrated" in out, out
    import store
    assert (silo / store.MIGRATED_MARKER).exists(), "a clean silo was not stamped done"
    assert (Path(tmp) / "projects" / KEY / "config.json").exists()
    # migrated, and fenced: the term is on no active CONTENT surface because it was REDACTED here,
    # not refused off. This is the difference the review asked to see proved.
    assert TERM not in _active_text(tmp, include_policy=False)
    # and the policy survived migration as a real term rather than an emptied list - a fence that
    # migrates into [] is exactly the silent downgrade the preflight exists to stop
    cfg = json.loads((Path(tmp) / "projects" / KEY / "config.json").read_text(encoding="utf-8"))
    assert cfg.get("refusal") == [TERM], cfg


def test_an_empty_policy_list_is_a_deliberate_no_policy_and_migrates(tmp_path):
    """`{"refusal": []}` is well-formed and by far the commonest shape. Refusing it would turn the
    guard into an outage on the default install."""
    tmp = str(tmp_path)
    silo = _silo(tmp, {"refusal": []})
    rc, out = _migrate(tmp)
    assert "REFUSED" not in out, out
    assert "done: 1 migrated" in out, out


def test_a_well_formed_list_whose_terms_drop_is_reported_not_refused(tmp_path):
    """The BOUNDARY of the refusal, pinned so it cannot drift in either direction.

    Container shape is the line, not term quality. A list tells us the operator's intent and then
    valid_policy_terms reports exactly what it lost; a scalar denies us the intent entirely. The
    distinction is load-bearing because _POLICY_TERM_MIN is 3, so "this list yields no usable term"
    also describes an ordinary `{"refusal": ["ab"]}` - escalating that to a refusal would strand a
    silo over a reported, recoverable drop."""
    tmp = str(tmp_path)
    silo = _silo(tmp, {"refusal": ["ab", 7]})
    rc, out = _migrate(tmp)

    assert "done: 1 migrated" in out, out
    import store
    assert (silo / store.MIGRATED_MARKER).exists(), "a reportable term drop stranded the silo"
    assert "dropped" in out, "the term loss was silent"


def test_a_wrong_typed_ordinary_key_quarantines_the_doc_but_does_not_strand_the_silo(tmp_path):
    """Deliberate asymmetry, and the boundary of what the preflight refuses.

    `{"scrub_pii": 0}` has a SAFE default: normalize_config drops it at every effect site and
    scrubbing stays ON, so it fails conservatively. Its config document is quarantined rather than
    activated - the migration stays honest instead of importing a document that lies - but the silo
    is NOT stranded, because a malformed knob is not a containment breach and the remaining datasets
    are perfectly migratable. A policy field gets the opposite treatment for the opposite reason:
    it has no safe default, so it refuses the whole silo before anything moves."""
    tmp = str(tmp_path)
    silo = _silo(tmp, {"scrub_pii": 0, "refusal": []})
    rc, out = _migrate(tmp)

    assert "done: 1 migrated" in out, out
    import store
    assert (silo / store.MIGRATED_MARKER).exists(), "a malformed knob stranded the silo"
    # the lying document did NOT become the active config
    assert not (Path(tmp) / "projects" / KEY / "config.json").exists()
    # and the loss is reported, never silent
    assert "config.json" in out


# --- .state.json: a scalar container kills capture silently ------------------------------------

def test_a_scalar_state_container_refuses_the_silo_and_leaves_no_marker(tmp_path):
    """`{"cursors": 0}` migrated as `validated` and every later Stop died silently."""
    tmp = str(tmp_path)
    silo = _silo(tmp, {"refusal": []}, state={"cursors": 0, "sizes": {}})
    rc, out = _migrate(tmp)

    assert "REFUSED" in out, out
    assert "NO MARKER" in out, out
    assert "cursors" in out, "the refusal must name the offending container"
    import store
    assert not (silo / store.MIGRATED_MARKER).exists()
    assert not (Path(tmp) / "projects" / KEY / ".state.json").exists()
    assert (silo / "working.md").exists(), "a refused run consumed the legacy source"


def test_a_well_formed_state_document_migrates_normally(tmp_path):
    """False-positive twin for the state gate."""
    tmp = str(tmp_path)
    silo = _silo(tmp, {"refusal": []},
                 state={"cursors": {"s-abc": 12}, "sizes": {}, "turns": {"s-abc": 3}})
    rc, out = _migrate(tmp)
    assert "REFUSED" not in out, out
    assert "done: 1 migrated" in out, out


def test_a_malformed_legacy_state_is_ignored_when_an_active_file_already_wins(tmp_path):
    """Scoping twin. A legacy .state.json is SKIPPED whenever an active one exists, so it never
    becomes active and cannot cause the harm. Refusing the silo over a document that was never
    going to be read would be a false refusal on an ordinary upgrade."""
    tmp = str(tmp_path)
    silo = _silo(tmp, {"refusal": []}, state={"cursors": 0})
    active = Path(tmp) / "projects" / KEY
    active.mkdir(parents=True)
    (active / ".state.json").write_text(json.dumps({"cursors": {}}), encoding="utf-8")

    rc, out = _migrate(tmp)
    assert "REFUSED" not in out, out
    import store
    assert (silo / store.MIGRATED_MARKER).exists(), "an ordinary upgrade was falsely refused"


# --- the runtime guard behind the preflight ----------------------------------------------------

def test_load_state_type_guards_every_container_not_just_three(tmp_path):
    """Defense in depth for state that is ALREADY corrupt on disk and never came through migrate.
    setdefault() only fills a MISSING key, so cursors and sizes - the two the guard loop did not
    cover - survived as scalars and reached the .get() that killed capture."""
    import store
    proj = store.project_dir(str(tmp_path))
    proj.mkdir(parents=True, exist_ok=True)
    (proj / ".state.json").write_text(
        json.dumps({"cursors": 0, "sizes": "nope", "turns": 5,
                    "checkpoint_due": [], "inject_ledger": None}), encoding="utf-8")

    st = store.load_state(str(tmp_path))
    for k in ("cursors", "sizes", "turns", "checkpoint_due", "inject_ledger"):
        assert isinstance(st[k], dict), "%s survived as a scalar" % k
    # the shape _flush_mirror actually relies on
    assert st["cursors"].get("any-session", 0) == 0


def test_load_state_preserves_a_valid_document_untouched(tmp_path):
    """False-positive twin: the guard must not flatten real cursors."""
    import store
    proj = store.project_dir(str(tmp_path))
    proj.mkdir(parents=True, exist_ok=True)
    (proj / ".state.json").write_text(
        json.dumps({"cursors": {"s-abc": 99}, "turns": {"s-abc": 4}}), encoding="utf-8")

    st = store.load_state(str(tmp_path))
    assert st["cursors"] == {"s-abc": 99}
    assert st["turns"] == {"s-abc": 4}


# --- W-F2, the three layers of the inject_ledger silencing (F review's exact reproduction) ------
# `inject_ledger` holding a non-list value made handle_posttooluse iterate a non-iterable inside its
# fail-silent boundary, so a valid Grep pairing vanished with no diagnostic. It is defended at three
# depths, each pinned here: preflight refuses it off a migrating silo, load_state drops it on an
# active store, and the hook itself coerces `seen` to a list before iterating.

def test_a_nested_non_list_inject_ledger_refuses_the_silo_at_migrate(tmp_path):
    """Depth 1: a legacy silo whose `.state.json` carries `inject_ledger: {"s1": 5}` refuses BEFORE
    any effect - the int can never reach an active store, let alone the Stop hook."""
    tmp = str(tmp_path)
    silo = _silo(tmp, {"refusal": []}, state={"inject_ledger": {"s1": 5}, "cursors": {}})
    rc, out = _migrate(tmp)
    assert "REFUSED" in out and "NO MARKER" in out, out
    assert "inject_ledger" in out, "the refusal must name the offending container"
    import store
    assert not (silo / store.MIGRATED_MARKER).exists()


def test_load_state_drops_a_nested_int_inject_ledger_value(tmp_path):
    """Depth 2: on an ALREADY-active store, load_state normalizes `{"s1": 5}` to an empty ledger, so
    `.get(sess_key)` can only ever return a list or None - never the int that broke the hook."""
    import store
    proj = store.project_dir(str(tmp_path))
    proj.mkdir(parents=True, exist_ok=True)
    (proj / ".state.json").write_text(
        json.dumps({"inject_ledger": {"s1": 5, "s2": ["grep:abc"]}}), encoding="utf-8")

    st = store.load_state(str(tmp_path))
    assert st["inject_ledger"] == {"s2": ["grep:abc"]}, \
        "the int-valued session was not dropped: %r" % st["inject_ledger"]


def test_the_pairing_hook_coerces_a_non_list_seen_before_iterating(tmp_path, monkeypatch):
    """Depth 3: the hook's own belt-and-suspenders. load_state above masks this on the public path,
    so the layer is proved by simulating a load_state that DIDN'T normalize (a future regression) -
    exactly the corruption the third layer exists to survive. With the coercion removed, the sum()
    over `seen` iterates an int and raises inside the hook."""
    import store
    import pcx
    cwd = str(tmp_path / "proj")
    os.makedirs(cwd, exist_ok=True)
    monkeypatch.setattr(store, "STORE_ROOT", Path(tmp_path))
    monkeypatch.setattr(store, "GLOBAL_CONFIG", Path(tmp_path) / "config.json")
    monkeypatch.setenv("PARSICA_ISOLATED", "1")   # pairing fires; the ledger write is skipped
    monkeypatch.setattr(store, "load_state",
                        lambda _cwd: {"inject_ledger": {"s1": 5}, "cursors": {}, "sizes": {},
                                      "turns": {}, "checkpoint_due": {}})

    # must not raise: a non-list `seen` is coerced to [] before the per-window cap sum() iterates it
    pcx.handle_posttooluse({"tool_name": "Grep", "tool_input": {"pattern": "carbonara"},
                            "cwd": cwd, "session_id": "s1"})


# --- W-F1 read side: the swap window between the containment proof and the open is closed ---------

def test_read_json_no_follow_reads_a_real_contained_file(tmp_path):
    """False-positive twin: an ordinary contained file reads back verbatim."""
    import store
    p = tmp_path / "config.json"
    p.write_text('{"refusal": ["ProjectHydra"]}', encoding="utf-8")
    assert json.loads(store._read_json_no_follow(p, tmp_path)) == {"refusal": ["ProjectHydra"]}


def test_read_json_no_follow_refuses_a_mid_read_identity_swap(tmp_path, monkeypatch):
    """The read-side twin of atomic_write's descriptor rule (Fable's adopted note): the file is
    proven by lstat, opened by name, and then the DESCRIPTOR is fstat'd and compared to the proof.
    A device/inode change between the two means a link was swapped in after the proof - refuse. With
    the fstat compare removed, this read would silently return the swapped file's bytes."""
    import store
    p = tmp_path / "config.json"
    p.write_text('{"refusal": []}', encoding="utf-8")

    class _Swapped:                      # an identity the real file can never have
        st_dev, st_ino = 0x7FFFFFFF, 0x7FFFFFFF
    monkeypatch.setattr(store.os, "fstat", lambda _fd: _Swapped())

    with pytest.raises(store.MigrationRefused):
        store._read_json_no_follow(p, tmp_path)


def test_read_json_no_follow_refuses_a_symlinked_config(tmp_path):
    """The link the swap-window guard exists for. Skip-guarded: file symlinks need privilege on
    Windows, but O_NOFOLLOW (POSIX) or the containment proof (both) refuse it where they can."""
    import store
    victim = tmp_path / "outside.json"
    victim.write_text('{"refusal": ["Leaked"]}', encoding="utf-8")
    link = tmp_path / "config.json"
    try:
        os.symlink(str(victim), str(link))
    except (OSError, NotImplementedError, AttributeError):
        pytest.skip("no privilege-free file symlink available")
    with pytest.raises(store.MigrationRefused):
        store._read_json_no_follow(link, tmp_path)


# --- the two remaining raw-policy readers ------------------------------------------------------

def test_the_workspace_output_union_never_char_splits_a_scalar(tmp_path, monkeypatch):
    """workspace_refusal_terms feeds fence_output, the OUTPUT fence for every surface at once. Read
    raw, a `{"refusal": "a"}` became the single term 'a' and redacted that letter everywhere."""
    import store
    monkeypatch.setattr(store, "STORE_ROOT", Path(tmp_path))
    monkeypatch.setattr(store, "GLOBAL_CONFIG", Path(tmp_path) / "config.json")
    (Path(tmp_path) / "config.json").write_text(json.dumps({"refusal": "a"}), encoding="utf-8")
    pdir = Path(tmp_path) / "projects" / KEY
    pdir.mkdir(parents=True)
    (pdir / "config.json").write_text(json.dumps({"refusal": TERM}), encoding="utf-8")

    terms = store.workspace_refusal_terms()
    assert terms == [], "a scalar policy produced live refusal terms: %r" % terms
    assert store.fence_output("banana carbonara") == "banana carbonara"


def test_the_workspace_output_union_still_fences_a_valid_term(tmp_path, monkeypatch):
    """False-positive twin: a well-formed list must still redact."""
    import store
    monkeypatch.setattr(store, "STORE_ROOT", Path(tmp_path))
    monkeypatch.setattr(store, "GLOBAL_CONFIG", Path(tmp_path) / "config.json")
    (Path(tmp_path) / "config.json").write_text(json.dumps({"refusal": [TERM]}), encoding="utf-8")

    assert store.workspace_refusal_terms() == [TERM]
    assert TERM not in store.fence_output("the %s launch" % TERM)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
