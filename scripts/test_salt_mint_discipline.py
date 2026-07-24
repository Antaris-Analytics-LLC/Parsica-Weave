"""Structural guard (Weave twin of Spirit's test_salt_mint_discipline): no CLI path mints a Spirit
salt outside the blessed persisted-guarded seam.

Weave imports the salt primitives from the installed Spirit and mints one only on promote /
migrate-identity. The RC5-H scar applies here too: resolve_salt(create=True) can hand back an
EPHEMERAL salt (unwritable install root, a dir squatting on <install>/.salt, a locked file) whose
identities no future run could reproduce - a silent whole-store fork. The blessed mint is
resolve_persisted_salt, which proves the salt round-trips from disk and returns a `persisted` flag
the handler must honor. As of RC5-I the CLI mints EXCLUSIVELY through resolve_persisted_salt; an
older Spirit that predates that seam makes the identity op REFUSE (version skew degrades loud), so
there is no raw-mint fallback left to carry a marker. Any NEW bare mint fails RED by default.

Discovers every non-test .py under scripts/ AND bin/ (glob, not a hand-list), AST-walks for calls to
the mint-capable primitive, and flags any mint-capable call (create not literal False) without an
audited `# salt-mint-ok` marker.

Soundness against evasion (m7). The callee match is not a literal-name substring - a one-line rename
must not slip the guard:
  * IMPORT ALIAS - `from parsica_spirit.enrich._salt import resolve_salt as _rs` then `_rs(...)`.
  * REBIND       - `rs = resolve_salt`, `rs: T = resolve_salt` (annotated), `(rs := resolve_salt)`
                   (walrus), `rs, _ = resolve_salt, 1` / `rs, *_ = resolve_salt, 1, 2` (tuple /
                   starred unpack), `def g(rs=resolve_salt)` / `lambda rs=resolve_salt` (param
                   default), `rs = resolve_salt or x` / `rs = resolve_salt if c else x` (bool /
                   conditional), or `rs = mod.resolve_salt`, then `rs(...)`; fixed-point over chains.
  * ATTRIBUTE    - `mod.resolve_salt(root, create=True)`.
  * GETATTR      - `getattr(mod, "resolve_salt")(...)`, incl. stored-then-called.
The marker is a real trailing COMMENT token (tokenize), never a substring - so `salt-mint-ok` inside
a string literal or identifier on the call line does not confer the exemption.

What this single-file AST pass CANNOT follow is stated, not silently capped: a fully DYNAMIC callee
(`getattr(mod, "resolve_"+"salt")`, `__import__`, `exec`); an indirection through a callable
CONTAINER (`{"f": resolve_salt}["f"](...)`, `[resolve_salt][0](...)`, `for rs in (resolve_salt,)`) or
`functools.partial`; and a renamed CROSS-MODULE re-export (the AST sees one file at a time). CALL-SITE
indirection is likewise out-of-scope when the callee is not a plain tracked name: a class attribute
invoked under its rebound name (`class C: rs = resolve_salt` then `C.rs(...)` / `self.rs(...)`) or a
walrus called directly in func position (`(rs := resolve_salt)(...)`). Each is a louder smell for
human review than the in-file rename this pass closes. Bias is deliberate: a false RED costs a marker
or rename; a false GREEN is the silent fork. Self-tests its teeth AND its false-positives so a
defanged detector fails loudly, not vacuously.
"""
import ast
import io
import os
import tokenize

HERE = os.path.dirname(os.path.abspath(__file__))                       # scripts/
BIN = os.path.join(os.path.dirname(HERE), "bin")                        # bin/ - sibling of scripts/

TARGET = "resolve_salt"              # the mint-capable primitive
BLESSED = "resolve_persisted_salt"   # the audited seam (proves round-trip); never a violation
MARKER = "salt-mint-ok"              # audited-exception token, matched as a real COMMENT


def _marker_lines(src):
    """Line numbers carrying a genuine trailing-COMMENT audit marker. tokenize (not a substring
    scan) means the marker inside a STRING literal or an identifier never confers the exemption -
    only a real `# ... salt-mint-ok ...` comment does."""
    marked = set()
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type == tokenize.COMMENT and MARKER in tok.string:
                marked.add(tok.start[0])
    except (tokenize.TokenError, IndentationError):
        pass
    return marked


def _is_getattr_of_target(node):
    """getattr(<x>, "resolve_salt") with a CONSTANT string naming the target primitive."""
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id == "getattr" and len(node.args) >= 2):
        return False
    s = node.args[1]
    return isinstance(s, ast.Constant) and isinstance(s.value, str) and s.value == TARGET


def _target_names(tree):
    """Local names that refer to the mint primitive: the import alias(es) plus rebinds - plain,
    ANNOTATED (`rs: T = resolve_salt`), WALRUS (`(rs := resolve_salt)`), and tuple/list unpack
    (`rs, x = resolve_salt, 1`) - through `= resolve_salt`, `= mod.resolve_salt`, or
    `= getattr(mod, "resolve_salt")`. Fixed-point so a chain (`a = resolve_salt; b = a`) is fully
    followed. The bare defined name always seeds the set (a same-module reference needs no import)."""
    names = {TARGET}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for a in node.names:
                if a.name == TARGET:
                    names.add(a.asname or a.name)

    def _value_is_target(v):
        if isinstance(v, ast.Name):
            return v.id in names
        if isinstance(v, ast.Attribute):
            return v.attr == TARGET
        if isinstance(v, ast.BoolOp):                 # rs = resolve_salt or fallback
            return any(_value_is_target(x) for x in v.values)
        if isinstance(v, ast.IfExp):                  # rs = resolve_salt if c else other
            return _value_is_target(v.body) or _value_is_target(v.orelse)
        return _is_getattr_of_target(v)

    def _bind(target, value):
        """Bind target Name(s) to a target-valued expression; unpacks tuple/list assignments
        element-wise - INCLUDING a starred target (the non-star prefix aligns from the front, the
        suffix from the back) - so `rs, x = resolve_salt, 1` and `rs, *rest = resolve_salt, 1, 2`
        both bind rs. A starred NAME itself binds to a list (container) - documented out-of-scope."""
        added = False
        if isinstance(target, (ast.Tuple, ast.List)):
            if not isinstance(value, (ast.Tuple, ast.List)):
                return False
            telts, velts = target.elts, value.elts
            star = next((i for i, e in enumerate(telts) if isinstance(e, ast.Starred)), None)
            if star is None:
                if len(telts) == len(velts):
                    for t, v in zip(telts, velts):
                        added = _bind(t, v) or added
            else:
                nsuf = len(telts) - star - 1
                if len(velts) >= star + nsuf:
                    for t, v in zip(telts[:star], velts[:star]):                 # prefix from front
                        added = _bind(t, v) or added
                    if nsuf:
                        for t, v in zip(telts[star + 1:], velts[len(velts) - nsuf:]):  # suffix from back
                            added = _bind(t, v) or added
            return added
        if isinstance(target, ast.Name) and target.id not in names and _value_is_target(value):
            names.add(target.id)
            added = True
        return added

    binders = []                                  # (target_node, value_node) - assignment family
    param_binders = []                            # (arg_name, default_value) - parameter defaults
    for n in ast.walk(tree):
        if isinstance(n, ast.Assign):
            for t in n.targets:
                binders.append((t, n.value))
        elif isinstance(n, ast.AnnAssign) and n.value is not None:   # rs: T = resolve_salt
            binders.append((n.target, n.value))
        elif isinstance(n, ast.NamedExpr):                           # (rs := resolve_salt)
            binders.append((n.target, n.value))
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            a = n.args                                               # def g(rs=resolve_salt) / lambda
            posargs = list(getattr(a, "posonlyargs", [])) + list(a.args)
            for arg, dflt in zip(posargs[len(posargs) - len(a.defaults):], a.defaults):
                param_binders.append((arg.arg, dflt))
            for arg, dflt in zip(a.kwonlyargs, a.kw_defaults):
                if dflt is not None:
                    param_binders.append((arg.arg, dflt))

    changed = True
    while changed:                                # resolve rebind chains to a fixed point
        changed = False
        for target, value in binders:
            if _bind(target, value):
                changed = True
        for name, value in param_binders:
            if name not in names and _value_is_target(value):
                names.add(name)
                changed = True
    return names


def _mint_violations(src, filename="<src>"):
    tree = ast.parse(src, filename=filename)
    names = _target_names(tree)
    marked = _marker_lines(src)
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        # match the callee through alias / attribute / getattr, not by literal spelling
        if isinstance(f, ast.Name) and f.id in names:
            callee = f.id
        elif isinstance(f, ast.Attribute) and f.attr == TARGET:   # mod.resolve_salt(...)
            callee = TARGET
        elif _is_getattr_of_target(f):                            # getattr(mod, "resolve_salt")(...)
            callee = TARGET
        else:
            continue
        # create=False (literal) is a READ - always safe. Missing create defaults True; a non-literal
        # create COULD be True at runtime. Both are mint-capable and need the marker. resolve_salt's
        # create is KEYWORD-ONLY (def resolve_salt(root, *, create=True)), so a real read always names
        # it by keyword; a positional create is a TypeError at runtime and never a live read.
        create = next((k.value for k in node.keywords if k.arg == "create"), None)
        if isinstance(create, ast.Constant) and create.value is False:
            continue
        lo = getattr(node, "lineno", 1)
        hi = getattr(node, "end_lineno", lo)
        if not any(lo <= ln <= hi for ln in marked):   # a real marker comment within the call span
            out.append((lo, callee))
    return out


def _cli_modules():
    out = []
    for d in (HERE, BIN):
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if fn.endswith(".py") and not fn.startswith("test_"):
                out.append(os.path.join(d, fn))
    return sorted(out)


def _violations_by_module():
    problems = {}
    for path in _cli_modules():
        with open(path, encoding="utf-8") as fh:
            v = _mint_violations(fh.read(), os.path.basename(path))
        if v:
            problems[os.path.basename(path)] = v
    return problems


def test_no_unguarded_salt_mint_in_cli():
    problems = _violations_by_module()
    assert not problems, (
        "a mint-capable resolve_salt (create not literal False) must route through "
        "resolve_persisted_salt or carry an audited `# salt-mint-ok` marker: %r" % problems)


def test_guard_discovers_pcx():
    """pcx.py is the module that mints on promote/migrate; the glob must actually see it."""
    found = {os.path.basename(p) for p in _cli_modules()}
    assert "pcx.py" in found, "guard failed to discover pcx.py"
    assert not any(b.startswith("test_") for b in found), "test modules must not be scanned"


def test_guard_has_teeth():
    """TRUE-POSITIVES: every in-scope mint vector, including the rebind forms m7 closes, must flag."""
    assert len(_mint_violations("salt = resolve_salt(root)\n")) == 1, \
        "a bare mint (create defaulted to True) must be flagged"
    assert len(_mint_violations("salt = resolve_salt(root, create=True)\n")) == 1, \
        "an explicit create=True must be flagged"
    assert len(_mint_violations("salt = resolve_salt(root, create=may_mint)\n")) == 1, \
        "a non-literal create must be flagged"
    assert len(_mint_violations(
        "from parsica_spirit.enrich._salt import resolve_salt as _rs\nsalt = _rs(root, create=True)\n")) == 1, \
        "an IMPORT ALIAS mint must be flagged"
    assert len(_mint_violations("rs = resolve_salt\nsalt = rs(root, create=True)\n")) == 1, \
        "a plain REBIND mint must be flagged"
    assert len(_mint_violations(
        "rs: object = resolve_salt\nsalt = rs(root, create=True)\n")) == 1, \
        "an ANNOTATED-assignment rebind (rs: T = resolve_salt) mint must be flagged"
    assert len(_mint_violations(
        "(rs := resolve_salt)\nsalt = rs(root, create=True)\n")) == 1, \
        "a WALRUS rebind mint must be flagged"
    assert len(_mint_violations(
        "rs, _x = resolve_salt, 1\nsalt = rs(root, create=True)\n")) == 1, \
        "a TUPLE-UNPACK rebind mint must be flagged"
    assert len(_mint_violations(
        "rs, *_rest = resolve_salt, 1, 2\nsalt = rs(root, create=True)\n")) == 1, \
        "a STARRED tuple-unpack rebind mint must be flagged"
    assert len(_mint_violations(
        "def g(rs=resolve_salt):\n    return rs(root, create=True)\n")) == 1, \
        "a function PARAMETER-DEFAULT mint (def g(rs=resolve_salt)) must be flagged"
    assert len(_mint_violations(
        "f = lambda rs=resolve_salt: rs(root, create=True)\n")) == 1, \
        "a LAMBDA parameter-default mint must be flagged"
    assert len(_mint_violations(
        "rs = resolve_salt or None\nsalt = rs(root, create=True)\n")) == 1, \
        "a BOOLOP rebind (resolve_salt or x) mint must be flagged"
    assert len(_mint_violations(
        "rs = resolve_salt if cond else other\nsalt = rs(root, create=True)\n")) == 1, \
        "an IFEXP rebind (resolve_salt if c else x) mint must be flagged"
    assert len(_mint_violations(
        "a = resolve_salt\nrs = a\nsalt = rs(root, create=True)\n")) == 1, \
        "a CHAINED rebind mint must be flagged (fixed-point resolution)"
    assert len(_mint_violations("salt = mod.resolve_salt(root, create=True)\n")) == 1, \
        "an ATTRIBUTE mint (mod.resolve_salt) must be flagged"
    assert len(_mint_violations('salt = getattr(mod, "resolve_salt")(root, create=True)\n')) == 1, \
        "a GETATTR mint must be flagged"
    assert len(_mint_violations(
        'fn = getattr(mod, "resolve_salt")\nsalt = fn(root, create=True)\n')) == 1, \
        "a stored-then-called GETATTR mint must be flagged"
    assert len(_mint_violations('salt = resolve_salt(root); log("salt-mint-ok")\n')) == 1, \
        "the marker inside a STRING literal must NOT confer the exemption"


def test_guard_false_positives():
    """FALSE-POSITIVES: legitimate shapes must PASS (the standing rule - every refusal ships its
    legitimate-default-passes companion beside its attack-refuses case)."""
    assert _mint_violations("salt = resolve_salt(root, create=False)\n") == [], \
        "a create=False read must never be flagged"
    assert _mint_violations("salt = resolve_salt(root)  # salt-mint-ok: fallback, verified\n") == [], \
        "a marked, audited mint must pass"
    assert _mint_violations("salt, ok = resolve_persisted_salt(root, create=True)\n") == [], \
        "the blessed resolve_persisted_salt seam is never a violation"
    assert _mint_violations(
        "from parsica_spirit.enrich._salt import resolve_salt as _rs\nsalt = _rs(root, create=False)\n") == [], \
        "an aliased READ (create=False) must never be flagged"
    assert _mint_violations(
        "rs: object = resolve_salt\nsalt = rs(root, create=False)\n") == [], \
        "an ANNOTATED-rebind READ (create=False) must never be flagged"
    assert _mint_violations(
        "rs = resolve_salt\nsalt = rs(root, create=True)  # salt-mint-ok\n") == [], \
        "a marked aliased mint must pass"


if __name__ == "__main__":
    fails = 0
    for fn in (test_no_unguarded_salt_mint_in_cli, test_guard_discovers_pcx,
               test_guard_has_teeth, test_guard_false_positives):
        try:
            fn()
            print("PASS " + fn.__name__)
        except AssertionError as e:
            fails += 1
            print("FAIL " + fn.__name__ + ": " + str(e))
    print("\n" + ("ALL SALT-MINT DISCIPLINE TESTS PASSED (green)"
                  if not fails else "RED: %d failing" % fails))
    raise SystemExit(1 if fails else 0)
