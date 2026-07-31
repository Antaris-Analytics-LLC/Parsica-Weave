"""enrich_prep stages store content into a temp inbox for the enrichment pass. That inbox is a
security surface: its /tmp path is predictable (keyed by project) and the tool writes raw. These
are the regression guards for the two hardening rules:

  * B4 - the inbox may never RESOLVE inside the store, and no component of its path may be a
    symlink (either could redirect staged raw chunks INTO the durable store).
  * B5 - the tool clears ONLY its own staging files and refuses to touch a caller's real
    directory; a --inbox pointed at real files is refused, never wiped.
"""
import json
import os
import re
import sys
import tempfile

import pytest

# enrich_prep imports store: pin the root BEFORE that import so no run of this suite can
# ever bind STORE_ROOT to the real HOME store (these tests never write it, but the mandate
# is structural: every test runs against a temp root).
os.environ.setdefault("PARSICA_WEAVE_ROOT", tempfile.mkdtemp(prefix="prep_boot_"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import enrich_prep

NO_STORE = os.path.join(tempfile.gettempdir(), "parsica-no-such-store")


def test_has_symlink_component_plain_path():
    assert enrich_prep._has_symlink_component(os.path.realpath(tempfile.gettempdir())) is False


def test_has_symlink_component_stops_at_the_os_temp_anchor():
    # The RAW, unresolved form: on macOS tempfile.gettempdir() always crosses /var ->
    # /private/var, a symlink baked into every stock install, not an attacker's redirect.
    # An explicit --inbox built from tempfile.mkdtemp() (as test_gateway.py's does) hits this
    # exact raw form, not the realpath'd one above - both must come back False.
    assert enrich_prep._has_symlink_component(tempfile.gettempdir()) is False


def test_b5_refuses_to_clear_a_caller_directory_with_real_files():
    d = tempfile.mkdtemp()
    keep = os.path.join(d, "important.md")
    with open(keep, "w", encoding="utf-8") as fh:
        fh.write("do not delete me")
    with pytest.raises(SystemExit) as ex:
        enrich_prep._prepare_inbox(d, NO_STORE)
    assert ex.value.code == 2
    assert os.path.exists(keep)   # the caller's file survives the refusal


def test_b5_accepts_empty_inbox_then_clears_only_staging_after_it_is_marked(tmp_path):
    inbox = str(tmp_path / "wv_inbox")
    os.makedirs(inbox)
    enrich_prep._prepare_inbox(inbox, NO_STORE)                 # empty -> safely claimed
    open(os.path.join(inbox, "old.txt"), "w").close()            # our own staging (clearable)
    open(os.path.join(inbox, "old.overlay.json"), "w").close()   # our own staging (clearable)
    enrich_prep._prepare_inbox(inbox, NO_STORE)
    assert os.path.exists(os.path.join(inbox, ".parsica-weave-inbox"))   # claimed as Weave-owned
    assert not os.path.exists(os.path.join(inbox, "old.txt"))
    assert not os.path.exists(os.path.join(inbox, "old.overlay.json"))


def test_b4_refuses_inbox_that_resolves_inside_the_store(tmp_path):
    store_root = os.path.realpath(str(tmp_path / "store"))
    os.makedirs(store_root)
    inside = os.path.join(store_root, "inbox")
    with pytest.raises(SystemExit) as ex:
        enrich_prep._prepare_inbox(inside, store_root)
    assert ex.value.code == 2


def test_f6_unsafe_inbox_is_refused_and_never_echoed_raw(tmp_path, capsys, monkeypatch):
    """F6: the inbox path is echoed on stdout for the next stage. A workspace-fenced term in it (a
    --inbox codename, or one riding the project dir name into the default) must never print raw.
    enrich_prep REFUSES an unsafe inbox (exit 2) and fences the echoed path, so a safe path stays
    real for the handshake and an unsafe one is redacted, never leaked."""
    import store
    term = "ZZINBOXCODEZZ"
    # drive the workspace refusal union directly (no config-cache dependence); is_isolated off so
    # main() reaches the inbox seam rather than the zero-footprint early return.
    monkeypatch.setattr(store, "workspace_refusal_terms", lambda: [term])
    monkeypatch.setattr(store, "is_isolated", lambda: False)
    bad_inbox = str(tmp_path / (term + "_inbox"))          # a --inbox that carries the fenced term
    monkeypatch.setattr(sys, "argv", ["enrich_prep.py", "--cwd", str(tmp_path), "--inbox", bad_inbox])
    with pytest.raises(SystemExit) as ex:
        enrich_prep.main()
    assert ex.value.code == 2, "an inbox carrying a fenced term is refused"
    out = capsys.readouterr().out
    assert term not in out, "the fenced codename must never echo raw in the inbox error JSON"
    assert "<redacted>" in out, "the path is redacted, not dropped (the error still reports)"


def test_m2_default_inbox_with_a_fenced_project_name_is_sanitized_not_refused(tmp_path, capsys, monkeypatch):
    """M2 - the FALSE-POSITIVE companion to F6's true-positive. A fenced term riding the PROJECT-DIR
    NAME into the DEFAULT inbox (no --inbox) must NOT be refused: the autonomous enrich-swarm workflow
    passes no --inbox, so a _die here has no remedy and would SILENTLY kill enrichment (the exact
    failure The Eye caught). The default slug is refusal-SCRUBBED instead - enrichment proceeds, the
    term never reaches the emitted inbox path, and nothing is echoed raw."""
    import store
    term = "ZZCODENAMEZZ"
    # inject a project_key that carries the fenced term (as a client-codename dir name would), so the
    # scrub is exercised regardless of how git_root resolves this tmp dir.
    monkeypatch.setattr(store, "project_key", lambda cwd: "client-%s-proj-98a775d138" % term)
    monkeypatch.setattr(store, "workspace_refusal_terms", lambda: [term])
    monkeypatch.setattr(store, "is_isolated", lambda: False)
    monkeypatch.setattr(enrich_prep, "_pending", lambda *a, **k: [])   # no chunks: isolate the inbox-path behaviour
    monkeypatch.setattr(sys, "argv", ["enrich_prep.py", "--cwd", str(tmp_path)])   # NO --inbox: the machine default
    enrich_prep.main()                                    # must NOT raise SystemExit (the whole point)
    out = capsys.readouterr().out
    data = json.loads(out)
    assert term not in data["inbox"], "the fenced project-name term must be scrubbed from the default inbox path"
    assert term not in out, "no raw fenced term anywhere in the handshake output"
    assert data["count"] == 0, "no chunks staged in this fixture"


def test_m2_default_slug_is_reformation_safe_and_fence_clean(monkeypatch):
    """M2 convergence-verify MAJOR: DELETING a refusal term from the default slug is not re-formation-
    safe - removing it can bring neighbours together and RE-FORM the term ('aabb' minus 'ab' -> 'ab';
    'caxyt' minus 'xy' -> 'cat'), leaking the codename into the emitted inbox path. The slug must be
    fence-CLEAN by construction (replace-with-separator + fixed point + fence_output backstop), so no
    re-formed or cross-term-formed codename ever survives, while a term-free name stays readable."""
    import store
    cases = [
        ("proj-aabb-98a775d138", ["ab"]),                      # self-overlap re-forms 'ab'
        ("proj-caxyt-98a775d138", ["cat", "xy"]),              # removing 'xy' re-forms 'cat'
        ("proj-ccodenameodename-1a2b3c", ["codename"]),        # realistic length
    ]
    for pk, terms in cases:
        monkeypatch.setattr(store, "project_key", lambda cwd, _pk=pk: _pk)
        monkeypatch.setattr(store, "workspace_refusal_terms", lambda _t=terms: _t)
        slug = enrich_prep._safe_default_slug(str(tempfile.gettempdir()))
        for term in terms:
            assert term.lower() not in slug.lower(), \
                "a re-formed refusal term leaked into the default slug %r (terms %r)" % (slug, terms)
        assert store.fence_output(slug) == slug, \
            "the emitted slug %r still trips the authoritative fence (terms %r)" % (slug, terms)
    # FALSE-POSITIVE: a term-free project name is NOT hashed away - the readable head survives.
    monkeypatch.setattr(store, "project_key", lambda cwd: "proj-clean-12ab34cd")
    monkeypatch.setattr(store, "workspace_refusal_terms", lambda: ["ZZNOPEZZ"])
    assert enrich_prep._safe_default_slug(str(tempfile.gettempdir())) == "proj-clean-12ab34cd"


def test_m2_hash_fallback_is_fence_checked_too(monkeypatch):
    """Convergence pass-3 MAJOR: the hash fallback is a SIBLING PATH of the readable slug and must
    pass the same fence oracle. A refusal term that is a hex substring of the per-project digest, or
    a substring of the fixed 'proj-' prefix, previously returned UNCHECKED and leaked into the
    emitted inbox path. Every returned candidate - readable or hashed - must satisfy
    store.fence_output(slug) == slug; a fenced hash candidate is re-derived until one passes."""
    import hashlib
    import store
    cwd = str(tempfile.gettempdir())
    digest12 = hashlib.sha256(os.path.abspath(cwd).encode("utf-8")).hexdigest()[:12]

    # (a) a hex-substring term drawn from THIS project's real digest: the fallback must re-derive,
    # never return a slug the fence would redact.
    term = digest12[4:7]
    monkeypatch.setattr(store, "project_key", lambda c: term)          # readable head scrubs empty
    monkeypatch.setattr(store, "workspace_refusal_terms", lambda: [term])
    slug = enrich_prep._safe_default_slug(cwd)
    assert store.fence_output(slug) == slug, "hash-fallback slug %r still trips the fence" % slug
    assert term.lower() not in slug.lower()

    # (b) the fixed prefix itself fenced: the bare-digest candidate must be used instead.
    monkeypatch.setattr(store, "project_key", lambda c: "proj")
    monkeypatch.setattr(store, "workspace_refusal_terms", lambda: ["proj"])
    slug = enrich_prep._safe_default_slug(cwd)
    assert store.fence_output(slug) == slug and "proj" not in slug.lower()

    # (c) PATHOLOGICAL vocabulary (every hex digit fenced - no machine slug can be clean): a LOUD
    # refusal with the --inbox remedy, never a silently emitted fenced path. The legitimate default
    # never reaches this (a real term set converges on the first or second candidate). The helper
    # RAISES (each stage renders it in its own JSON shape); main() turns it into the exit-2 _die.
    monkeypatch.setattr(store, "project_key", lambda c: "0")
    monkeypatch.setattr(store, "workspace_refusal_terms", lambda: list("0123456789abcdef"))
    with pytest.raises(enrich_prep.NoSafeDefaultInbox):
        enrich_prep._safe_default_slug(cwd)


def test_pathological_vocabulary_still_exits_2_from_the_cli(tmp_path, capsys, monkeypatch):
    """The helper now RAISES instead of printing, so pin the CLI contract it feeds: enrich_prep's
    main() still dies with exit 2 and a single fenced error object (the shape the workflow reads),
    and enrich_apply renders its OWN applied/malformed-shaped refusal rather than leaking prep's."""
    import store
    import enrich_apply
    monkeypatch.setattr(store, "project_key", lambda c: "0")
    monkeypatch.setattr(store, "workspace_refusal_terms", lambda: list("0123456789abcdef"))
    monkeypatch.setattr(store, "is_isolated", lambda: False)
    monkeypatch.setattr(enrich_prep, "_pending", lambda *a, **k: [])
    monkeypatch.setattr(sys, "argv", ["enrich_prep.py", "--cwd", str(tmp_path)])
    with pytest.raises(SystemExit) as ex:
        enrich_prep.main()
    assert ex.value.code == 2
    out = capsys.readouterr().out
    assert len([ln for ln in out.splitlines() if ln.strip()]) == 1, \
        "exactly ONE JSON object on the handshake, never a helper's object plus the stage's"
    assert json.loads(out)["error"]

    monkeypatch.setattr(sys, "argv", ["enrich_apply.py", "--cwd", str(tmp_path)])
    with pytest.raises(SystemExit) as ex2:
        enrich_apply.main()
    assert ex2.value.code == 3
    data = json.loads(capsys.readouterr().out)
    assert data["applied"] == 0 and data["malformed"] == 0 and "refused" in data


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlinks unavailable on this platform")
def test_b4_symlink_component_anywhere_in_path_is_detected(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    try:
        os.symlink(str(real), str(link))
    except (OSError, NotImplementedError) as e:
        pytest.skip(f"cannot create symlink here: {e}")   # e.g. Windows without privilege
    # a symlink as an ANCESTOR (not just the leaf) must still be caught
    target = os.path.join(str(link), "sub", "inbox")
    assert enrich_prep._has_symlink_component(target) is True


def _private_tempdir(monkeypatch, tmp_path):
    """Point default_inbox()'s derivation at THIS test's tmp_path.

    Every test below pins project_key to a fixed slug so the derived path is deterministic - but
    the path is derived under tempfile.gettempdir(), so without this the fixed slug resolves to
    ONE shared directory under the machine's real temp. Runs then stage into each other's inbox
    and leave files behind, and the next run trips _prepare_inbox's "unmarked non-empty inbox"
    refusal. The guard is right every time; the tests were reaching into shared machine state.
    A fresh CI runner hides this completely - it only ever bites the second run on a real
    developer machine, which is the worst place for a suite to be order-dependent."""
    base = tmp_path / "_tmp"
    base.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(tempfile, "tempdir", str(base))
    return base


def test_4a_clean_default_inbox_echoes_byte_identical_and_not_withheld(tmp_path, capsys, monkeypatch):
    """False-positive direction of the round-4 4a echo fence: with no refusal term anywhere in the
    machine temp path, the success echo is the byte-identical REAL path (the fence is a no-op),
    inbox_withheld is False, and the value equals the deterministic default_inbox BOTH stages
    derive - the normal autonomous pipeline is untouched."""
    import store
    _private_tempdir(monkeypatch, tmp_path)
    monkeypatch.setattr(store, "project_key", lambda cwd: "proj-clean-12ab34cd")
    monkeypatch.setattr(store, "workspace_refusal_terms", lambda: ["ZZABSENTZZ"])
    monkeypatch.setattr(store, "is_isolated", lambda: False)
    monkeypatch.setattr(enrich_prep, "_pending", lambda *a, **k: [])
    monkeypatch.setattr(sys, "argv", ["enrich_prep.py", "--cwd", str(tmp_path)])
    enrich_prep.main()
    data = json.loads(capsys.readouterr().out)
    assert data["inbox_withheld"] is False
    assert data["inbox"] == enrich_prep.default_inbox(str(tmp_path)), \
        "the clean-path echo must be the REAL deterministic default (fence must be a no-op)"


def test_4a_refusal_term_in_temp_prefix_is_withheld_never_emitted_raw(tmp_path, capsys, monkeypatch):
    """Round-4 4a true-positive: the default inbox rides under tempfile.gettempdir(), whose
    components are operator-ENVIRONMENT-derived (a username, a parent dir) - exactly the kind of
    codename a workspace refusal list names; 'the %TEMP% prefix is machine-local, never a codename'
    was the assumption this scenario breaks. When a term coincides with a temp-path component the
    raw term must never transit stdout: the echo redacts, inbox_withheld goes True (the workflow
    stops the worker fan-out on it), and staging still happened on disk - the dir is the operator's
    own temp path, so its existence discloses nothing new; the degradation is VISIBLE, never a
    silent enrichment death and never a leak."""
    import store
    import tempfile as _tf
    term = "zzharborzz"        # neutral fake sentinel, like every other one in the tree
    fake_tmp = tmp_path / ("Users_%s" % term) / "Temp"
    fake_tmp.mkdir(parents=True)
    monkeypatch.setattr(_tf, "tempdir", str(fake_tmp))
    monkeypatch.setattr(store, "project_key", lambda cwd: "proj-clean-12ab34cd")
    monkeypatch.setattr(store, "workspace_refusal_terms", lambda: [term])
    monkeypatch.setattr(store, "is_isolated", lambda: False)
    monkeypatch.setattr(enrich_prep, "_pending", lambda *a, **k: [])
    monkeypatch.setattr(sys, "argv", ["enrich_prep.py", "--cwd", str(tmp_path)])
    enrich_prep.main()                                   # must NOT die: sanitize-not-refuse (M2)
    out = capsys.readouterr().out
    data = json.loads(out)
    assert term not in out, "the raw refusal term must never transit the handshake stdout"
    assert data["inbox_withheld"] is True
    assert "<redacted>" in data["inbox"], "the echoed path is redacted, not dropped"
    assert os.path.isdir(enrich_prep.default_inbox(str(tmp_path))), \
        "staging still happened on disk (visible degradation, not a silent death)"


def test_4a_apply_rederives_the_default_inbox_from_cwd(tmp_path, capsys, monkeypatch):
    """enrich_apply with NO --inbox re-derives the SAME deterministic default enrich_prep stages
    into, so the real path never needs to round-trip through worker context. Proven functionally:
    a malformed overlay dropped into the DERIVED inbox is seen by apply (malformed=1), so the glob
    really ran over the re-derived dir, not a guess."""
    import store
    import enrich_apply
    _private_tempdir(monkeypatch, tmp_path)
    monkeypatch.setattr(store, "project_key", lambda cwd: "proj-clean-12ab34cd")
    monkeypatch.setattr(store, "workspace_refusal_terms", lambda: ["ZZABSENTZZ"])
    monkeypatch.setattr(store, "is_isolated", lambda: False)
    inbox = enrich_prep.default_inbox(str(tmp_path))
    os.makedirs(inbox, exist_ok=True)
    # Stand in for the prep stage this test presumes already ran: apply requires the ownership
    # marker before it will glob an inbox holding overlays. Under the old shared machine temp the
    # marker was simply already there from some earlier run, so the setup never had to say so.
    open(os.path.join(inbox, ".parsica-weave-inbox"), "a", encoding="utf-8").close()
    with open(os.path.join(inbox, "cid-1.overlay.json"), "w", encoding="utf-8") as fh:
        fh.write("{this is not json")
    monkeypatch.setattr(sys, "argv", ["enrich_apply.py", "--cwd", str(tmp_path)])
    enrich_apply.main()
    data = json.loads(capsys.readouterr().out)
    # swept=0 because nothing applied: a malformed overlay must KEEP its staged .txt so a re-run
    # can retry it. That pairing is pinned directly in test_enrich_inbox_sweep.py.
    assert data == {"applied": 0, "malformed": 1, "swept": 0}, data


def test_conv_a_fenced_chunk_id_is_skipped_never_echoed(tmp_path, capsys, monkeypatch):
    """CONVERGENCE MAJOR (the 4a fix's unfenced sibling sink): the fix fenced the `inbox` field but
    left `ids` on the SAME stdout line raw - and ids are embedded verbatim in every worker prompt as
    <inbox>/<id>.txt, so a refusal term that is a hex substring of a chunk id (a short hex-alphabet
    codename hits roughly one id in a few hundred - the same digest-substring coincidence already
    adjudicated major twice on the slug's hash fallback) transited stdout, worker context, and the
    transcript with inbox_withheld staying false. An id is FUNCTIONAL (it is the filename) so it
    cannot be redacted: skip the chunk and report the count instead."""
    import store
    term = "beef"
    fenced_id = "cid-12dead%s34567890ab" % term
    clean_id = "cid-0123456789abcdef0123"
    _private_tempdir(monkeypatch, tmp_path)
    monkeypatch.setattr(store, "project_key", lambda cwd: "proj-clean-12ab34cd")
    monkeypatch.setattr(store, "workspace_refusal_terms", lambda: [term])
    monkeypatch.setattr(store, "is_isolated", lambda: False)
    monkeypatch.setattr(enrich_prep, "_pending", lambda *a, **k: [
        {"id": fenced_id, "user": "a question", "asst": "an answer"},
        {"id": clean_id, "user": "another question", "asst": "another answer"},
    ])
    monkeypatch.setattr(sys, "argv", ["enrich_prep.py", "--cwd", str(tmp_path)])
    enrich_prep.main()
    out = capsys.readouterr().out
    data = json.loads(out)
    assert term not in out, "a fenced term must never transit the ids array on stdout"
    assert data["ids"] == [clean_id], "the fence-tripping id is skipped, the clean one stages"
    assert data["skipped_fenced"] == 1, "the skip is REPORTED, never silent"
    assert data["count"] == 1
    staged = os.listdir(enrich_prep.default_inbox(str(tmp_path)))
    assert not any(term in f for f in staged), "no term-bearing file is written either"


def test_conv_clean_chunk_ids_all_stage_with_no_skips(tmp_path, capsys, monkeypatch):
    """FALSE-POSITIVE companion: with no refusal term matching any id, every pending chunk stages
    and skipped_fenced is 0 - the normal autonomous path is untouched by the new skip."""
    import store
    _private_tempdir(monkeypatch, tmp_path)
    monkeypatch.setattr(store, "project_key", lambda cwd: "proj-clean-12ab34cd")
    monkeypatch.setattr(store, "workspace_refusal_terms", lambda: ["ZZABSENTZZ"])
    monkeypatch.setattr(store, "is_isolated", lambda: False)
    ids = ["cid-0123456789abcdef0123", "cid-fedcba9876543210fedc"]
    monkeypatch.setattr(enrich_prep, "_pending", lambda *a, **k: [
        {"id": i, "user": "q", "asst": "a"} for i in ids])
    monkeypatch.setattr(sys, "argv", ["enrich_prep.py", "--cwd", str(tmp_path)])
    enrich_prep.main()
    data = json.loads(capsys.readouterr().out)
    assert data["ids"] == ids and data["skipped_fenced"] == 0 and data["count"] == 2


# --- the stage-to-workflow HANDSHAKE contract ------------------------------------------------
# enrich_prep's stdout is consumed by enrich-swarm.workflow.js under a STRICT json schema
# (additionalProperties:false, safety fields required). Nothing tied the two together, so the
# schema could tighten - as it did when skipped_fenced became required - while a producer path
# still emitted a short object, and the failure would surface only at runtime as a mid-stage
# schema error. The schema is READ OUT of the workflow file rather than restated here, so the
# test follows an edit to either side instead of pinning a stale copy. (convergence pass 2)
WORKFLOW = os.path.join(os.path.dirname(os.path.abspath(__file__)), "enrich-swarm.workflow.js")


def _mask_js_strings(src):
    """Return a same-LENGTH copy of `src` with the contents of every '/"/` string or template
    literal blanked out (quote characters kept, contents replaced with spaces), so a brace-balance
    walk over the result never miscounts a `{`/`}` that only appears inside message text (a `note:`
    field's prose, or a `${...}` template interpolation's own braces - blanked along with the rest
    of the literal, which is correct here: this file only ever needs the OUTER object's structure,
    never what is syntactically valid inside an interpolation expression). Convergence pass 3 MINOR,
    self-caught: the original walk counted every brace uniformly and would truncate early on a
    stray `}` inside a string, or overrun past the real end on a stray `{`. Basic backslash-escape
    handling; indices are preserved so callers can still slice the ORIGINAL `src` for content."""
    out = list(src)
    i, n = 0, len(src)
    while i < n:
        ch = src[i]
        if ch in ("'", '"', "`"):
            quote = ch
            j = i + 1
            while j < n and src[j] != quote:
                j += 2 if src[j] == "\\" and j + 1 < n else 1
            for k in range(i + 1, min(j, n)):
                out[k] = " "
            i = j + 1
        else:
            i += 1
    return "".join(out)


def _balanced_end(src, open_at):
    """The index of the `}` that closes the `{` at `open_at`, counted on a STRING-MASKED copy of
    `src` (see _mask_js_strings) so text inside a string can never miscount the depth."""
    masked = _mask_js_strings(src)
    depth = 0
    for i, ch in enumerate(masked[open_at:], open_at):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
    raise ValueError("unbalanced braces from %d" % open_at)


def _scan_schema():
    """The scan stage's declared property names and required keys, lifted from the workflow."""
    with open(WORKFLOW, encoding="utf-8") as fh:
        src = fh.read()
    body = src[src.index("label: 'scan'"):]
    props_at = body.index("properties:")
    end = _balanced_end(body, props_at)
    # scan INSIDE the properties object, not from the "properties:" token itself - starting at the
    # token captured the container's own name as a property, which quietly widened the allowed set.
    inner = body[body.index("{", props_at) + 1:end]
    props = set(re.findall(r"(\w+)\s*:\s*\{", inner))
    props.discard("items")                         # the array-element schema, not a property
    required = set(re.findall(r"'([^']+)'",
                              body[body.index("required:"):body.index("]", body.index("required:"))]))
    assert props and required, "failed to lift the scan schema from the workflow file"
    return props, required


def _assert_matches_scan_schema(data):
    props, required = _scan_schema()
    missing = required - set(data)
    assert not missing, "emitted object omits schema-REQUIRED key(s) %r: %r" % (sorted(missing), data)
    extra = set(data) - props
    assert not extra, ("emitted key(s) %r are not declared in the scan schema, which is "
                       "additionalProperties:false - the stage would fail validation: %r"
                       % (sorted(extra), data))


def test_conv_isolated_refusal_satisfies_the_workflow_scan_schema(tmp_path, capsys, monkeypatch):
    """An isolated session REFUSES to stage, and that refusal is a RESULT: it must satisfy the same
    schema a success does. It previously emitted only refused/ids/count/note - two undeclared keys
    and three missing required ones - so the strict schema turned a clean stop into a stage error."""
    import store
    monkeypatch.setattr(store, "is_isolated", lambda: True)
    monkeypatch.setattr(sys, "argv", ["enrich_prep.py", "--cwd", str(tmp_path)])
    with pytest.raises(SystemExit) as ex:
        enrich_prep.main()
    assert ex.value.code == 3, "isolation is a refusal, not a success"
    data = json.loads(capsys.readouterr().out)
    assert data["refused"] == "PARSICA_ISOLATED=1" and data["ids"] == []
    _assert_matches_scan_schema(data)


def test_conv_success_echo_satisfies_the_workflow_scan_schema(tmp_path, capsys, monkeypatch):
    """The other direction: the normal staging echo must also match, so the contract test cannot
    pass by only ever exercising the refusal shape."""
    import store
    _private_tempdir(monkeypatch, tmp_path)
    monkeypatch.setattr(store, "project_key", lambda cwd: "proj-clean-99ff88ee")
    monkeypatch.setattr(store, "workspace_refusal_terms", lambda: ["ZZABSENTZZ"])
    monkeypatch.setattr(store, "is_isolated", lambda: False)
    monkeypatch.setattr(enrich_prep, "_pending", lambda *a, **k: [
        {"id": "cid-0123456789abcdef0123", "user": "q", "asst": "a"}])
    monkeypatch.setattr(sys, "argv", ["enrich_prep.py", "--cwd", str(tmp_path)])
    enrich_prep.main()
    _assert_matches_scan_schema(json.loads(capsys.readouterr().out))


def test_conv_skipped_fenced_is_required_by_the_workflow_schema():
    """The counter is only load-bearing if the workflow REQUIRES it: an absent field reads as falsy
    in JS, so an optional skipped_fenced silently zeroes the very signal that makes an
    all-chunks-withheld run visible instead of reading as 'nothing pending'."""
    _, required = _scan_schema()
    for key in ("inbox_withheld", "skipped_fenced"):
        assert key in required, "%s must be REQUIRED, not optional - it is a safety signal" % key


def test_conv_every_workflow_return_path_reports_the_skip_count():
    """Structural: each `return {` in the workflow must carry skippedFenced. Both early returns
    dropped it, and the all-chunks-skipped run lands in the first one - reporting 'nothing pending
    to enrich' with pending 0, the exact silent enrichment death the counter exists to prevent."""
    with open(WORKFLOW, encoding="utf-8") as fh:
        src = fh.read()
    returns = [m.start() for m in re.finditer(r"return \{", src)]
    assert len(returns) >= 3, "expected the early-exit, withheld, and normal return paths"
    for at in returns:
        # brace-open is the LAST char of "return {" (at + len("return ")); _balanced_end masks
        # string/template-literal contents first, so a stray brace inside a `note:` message or a
        # `${...}` interpolation can no longer truncate or overrun the real object boundary
        # (convergence pass 3 MINOR, self-caught).
        end = _balanced_end(src, at + len("return "))
        assert "skippedFenced" in src[at:end], (
            "a workflow return path omits skippedFenced: %r" % src[at:end][:120])


def test_conv_balanced_end_ignores_braces_inside_strings():
    """_balanced_end's own teeth, both dangerous directions (convergence pass 3 MINOR, self-caught
    against my own pass-2 test): a stray `}` inside a message string must not truncate the object
    early (which would report a real skippedFenced as missing - a false alarm), and a stray `{`
    must not let the walk overrun into an UNRELATED later object (which would report a genuinely
    broken return path as fine - a false PASS, the dangerous direction)."""
    truncation_case = 'return { pending: 0, note: "unexpected } in output", skippedFenced: 3 }'
    at = truncation_case.index("return ") + len("return ")
    end = _balanced_end(truncation_case, at)
    assert "skippedFenced" in truncation_case[at:end], \
        "a stray '}' inside a string must not truncate the object before the real close"

    overrun_case = ('return { pending: 0, note: "malformed { config" } '
                    'return { pending: 1, skippedFenced: 2 }')
    at = overrun_case.index("return ") + len("return ")
    end = _balanced_end(overrun_case, at)
    assert "skippedFenced" not in overrun_case[at:end], \
        "a stray '{' inside a string must not let the walk overrun into the NEXT return path"
    assert overrun_case[end] == "}", "must stop at path #1's OWN closing brace, not path #2's"


def test_conv_every_emitted_object_satisfies_the_scan_schema():
    """DISCOVER, don't list. The two hand-picked paths above (isolated refusal, success echo) are
    exactly the trap: fixing one twin and leaving the other open is how this file's own history
    reads. _die was the twin left open - it emitted a bare {error, inbox}, which fails the strict
    schema on BOTH counts (undeclared key, three missing required ones) and is reachable on the
    AUTONOMOUS no---inbox path (NoSafeDefaultInbox, and every _prepare_inbox containment refusal).
    So this test finds every `print(json.dumps({...}))` in the module by parsing the source and
    checks each literal's keys against the lifted schema - a new exit path is covered the day it is
    written, not the day someone remembers to add a case here."""
    import ast
    props, required = _scan_schema()
    src = open(enrich_prep.__file__, encoding="utf-8").read()
    tree = ast.parse(src)

    emitted = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and getattr(node.func, "id", None) == "print"):
            continue
        for arg in node.args:
            if not (isinstance(arg, ast.Call)
                    and getattr(arg.func, "attr", None) == "dumps"
                    and arg.args and isinstance(arg.args[0], ast.Dict)):
                continue
            keys = {k.value for k in arg.args[0].keys
                    if isinstance(k, ast.Constant) and isinstance(k.value, str)}
            emitted.append((getattr(node, "lineno", "?"), keys))

    assert len(emitted) >= 3, ("the scan did not find the known emit sites - a vacuous pass is "
                               "worse than a failure here: %r" % emitted)
    problems = []
    for line, keys in emitted:
        missing = required - keys
        extra = keys - props
        if missing or extra:
            problems.append({"line": line, "missing": sorted(missing), "undeclared": sorted(extra)})
    assert not problems, (
        "every object enrich_prep prints is consumed by the workflow under a strict schema "
        "(additionalProperties:false); these exit paths would fail validation and kill the stage "
        "on a schema error instead of reporting their result: %r" % problems)
