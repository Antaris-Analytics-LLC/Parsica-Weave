"""enrich_apply is the APPLY stage of the enrich-swarm workflow: it prints a JSON handshake the
workflow parses with a STRICT (additionalProperties:false) schema. Q1/Q2 (convergence pass 3): the
Scan stage's own schema was tightened to accept its refusal paths (the `_die` fix); the Apply
stage's schema was left at its original shape - `{applied, malformed}`, `required: ['applied']` -
so BOTH of enrich_apply's refusal paths (isolated session, no fence-clean default inbox) would fail
schema validation the instant they fired, turning a clean, visible refusal into a stage error. These
are the regression guards for that fix, mirroring test_enrich_prep_guard.py's scan-schema tests."""
import json
import os
import re
import sys
import tempfile

import pytest

os.environ.setdefault("PARSICA_WEAVE_ROOT", tempfile.mkdtemp(prefix="apply_boot_"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import enrich_apply
import enrich_prep

WORKFLOW = os.path.join(os.path.dirname(os.path.abspath(__file__)), "enrich-swarm.workflow.js")


def _mask_js_strings(src):
    """Return a same-LENGTH copy of `src` with the contents of every '/"/` string or template
    literal blanked out, so a brace-balance walk over the result never miscounts a `{`/`}` that
    only appears inside message text. Same helper as test_enrich_prep_guard.py's (convergence
    pass 3 MINOR, self-caught there) - duplicated rather than imported, matching this codebase's
    existing convention of no cross-test-file imports (checked: none exist)."""
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
    """The index of the `}` that closes the `{` at `open_at`, counted on a string-masked copy."""
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


def _apply_schema():
    """The apply stage's declared property names and required keys, lifted from the workflow -
    same technique as test_enrich_prep_guard.py's _scan_schema, so this test follows an edit to
    either side instead of pinning a stale copy."""
    with open(WORKFLOW, encoding="utf-8") as fh:
        src = fh.read()
    body = src[src.index("label: 'apply'"):]
    props_at = body.index("properties:")
    end = _balanced_end(body, props_at)
    inner = body[body.index("{", props_at) + 1:end]
    props = set(re.findall(r"(\w+)\s*:\s*\{", inner))
    props.discard("items")
    required = set(re.findall(r"'([^']+)'",
                              body[body.index("required:"):body.index("]", body.index("required:"))]))
    assert props and required, "failed to lift the apply schema from the workflow file"
    return props, required


def _assert_matches_apply_schema(data):
    props, required = _apply_schema()
    missing = required - set(data)
    assert not missing, "emitted object omits schema-REQUIRED key(s) %r: %r" % (sorted(missing), data)
    extra = set(data) - props
    assert not extra, ("emitted key(s) %r are not declared in the apply schema, which is "
                       "additionalProperties:false - the stage would fail validation: %r"
                       % (sorted(extra), data))


def test_conv_isolated_refusal_satisfies_the_workflow_apply_schema(tmp_path, capsys, monkeypatch):
    """An isolated session REFUSES to apply, and that refusal is a RESULT: it must satisfy the
    same schema a success does. Pre-fix it emitted {applied, malformed, refused} against a schema
    with no `refused` property and only `applied` required - undeclared key AND (harmlessly, since
    malformed was also present) an unenforced required list that happened not to fail on THIS path,
    but WOULD fail additionalProperties:false on the undeclared key alone."""
    import store
    monkeypatch.setattr(store, "is_isolated", lambda: True)
    monkeypatch.setattr(sys, "argv", ["enrich_apply.py", "--cwd", str(tmp_path)])
    with pytest.raises(SystemExit) as ex:
        enrich_apply.main()
    assert ex.value.code == 3, "isolation is a refusal, not a success"
    data = json.loads(capsys.readouterr().out)
    assert data["refused"] == "PARSICA_ISOLATED=1" and data["applied"] == 0
    _assert_matches_apply_schema(data)


def test_conv_no_safe_default_inbox_refusal_satisfies_the_workflow_apply_schema(
        tmp_path, capsys, monkeypatch):
    """The sibling refusal: no fence-clean default inbox exists for this project (a pathological
    refusal vocabulary). Same schema, same requirement - this is the SECOND refusal shape the
    scan-stage fix's own history warns against leaving unfixed while the first gets patched."""
    import store
    monkeypatch.setattr(store, "is_isolated", lambda: False)

    def _refuse(cwd):
        raise enrich_prep.NoSafeDefaultInbox("no fence-clean slug survives")

    monkeypatch.setattr(enrich_prep, "default_inbox", _refuse)
    monkeypatch.setattr(sys, "argv", ["enrich_apply.py", "--cwd", str(tmp_path)])
    with pytest.raises(SystemExit) as ex:
        enrich_apply.main()
    assert ex.value.code == 3
    data = json.loads(capsys.readouterr().out)
    assert data["applied"] == 0 and "refused" in data
    _assert_matches_apply_schema(data)


def test_conv_success_echo_satisfies_the_workflow_apply_schema(tmp_path, capsys, monkeypatch):
    """The normal path: an empty inbox (nothing to apply) is the simplest success shape - the
    contract test cannot pass by only ever exercising the refusal shapes."""
    import store
    monkeypatch.setattr(store, "is_isolated", lambda: False)
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    monkeypatch.setattr(sys, "argv",
                        ["enrich_apply.py", "--cwd", str(tmp_path), "--inbox", str(inbox)])
    enrich_apply.main()
    data = json.loads(capsys.readouterr().out)
    # `swept` joined the handshake when apply began deleting the staged copies whose enrichment
    # landed. Kept as exact equality rather than loosened to a subset check: the strictness is
    # the point here, because the workflow schema is additionalProperties:false and an UNEXPECTED
    # key is exactly the failure this test exists to catch.
    assert data == {"applied": 0, "malformed": 0, "swept": 0}
    _assert_matches_apply_schema(data)


def test_conv_malformed_is_required_by_the_workflow_apply_schema():
    """The counter is only load-bearing if the workflow REQUIRES it (Q2): the workflow reads
    `applied.malformed || 0` while the pre-fix schema required only `applied` - safe today only
    because every emit path happens to include it, but an optional field is one refactor away from
    silently dropping the malformed-overlay count the operator relies on to notice worker failures."""
    _, required = _apply_schema()
    assert "malformed" in required, "malformed must be REQUIRED - every emit path already includes it"


def test_conv_balanced_end_ignores_braces_inside_strings():
    """Sibling of test_enrich_prep_guard.py's own regression test for the same helper - a stray
    brace inside a JS string literal must not truncate or overrun the schema-lifting walk."""
    truncation_case = 'properties: { applied: 0, note: "unexpected } in output", malformed: 3 }'
    at = truncation_case.index("properties:") + len("properties:")
    end = _balanced_end(truncation_case, truncation_case.index("{", at))
    assert "malformed" in truncation_case[at:end], \
        "a stray '}' inside a string must not truncate the object before the real close"

    overrun_case = ('properties: { applied: 0, note: "malformed { config" } '
                    'required: [ "skippedFenced" ]')
    at = overrun_case.index("properties:") + len("properties:")
    end = _balanced_end(overrun_case, overrun_case.index("{", at))
    assert "skippedFenced" not in overrun_case[at:end], \
        "a stray '{' inside a string must not let the walk overrun into unrelated later content"


def test_conv_every_emitted_object_satisfies_the_apply_schema():
    """DISCOVER, don't list: AST-walk every `print(json.dumps({...}))` literal in enrich_apply.py
    and check its keys against the lifted schema, so a new exit path is covered the day it is
    written - the same technique that caught the Scan-stage's `_die` gap independently."""
    import ast
    props, required = _apply_schema()
    src = open(enrich_apply.__file__, encoding="utf-8").read()
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

    assert len(emitted) >= 3, ("expected the isolated-refusal, no-safe-inbox-refusal, and success "
                               "emit sites - a vacuous pass is worse than no test: %r" % emitted)
    for lineno, keys in emitted:
        missing = required - keys
        assert not missing, "line %s omits required key(s) %r: emits %r" % (lineno, missing, keys)
        extra = keys - props
        assert not extra, "line %s emits undeclared key(s) %r: %r" % (lineno, extra, keys)
