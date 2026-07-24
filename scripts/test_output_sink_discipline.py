#!/usr/bin/env python3
"""Structural output-fence guard: no interpolated CLI output may skip the refusal fence.

Every confirmed output-fence bypass so far was the SAME shape: an interpolated string
written straight to stdout/stderr with no fence, so a refusal term configured in another
project could ride out on the terminal. The W2 channel line, the W4 query error branches,
and the migrate manifest were three rounds of the same bug. Patching each site is
whack-a-mole; this guard makes the class un-reintroducible.

It DISCOVERS every CLI module in scripts/ AND bin/ (glob, not a hand-list), walks each with
`ast`, and fails if a print() / sys.stdout.write() / sys.stderr.write() is handed a
CONTENT-CARRYING argument with no fence call in its subtree (fence_output / emit_text /
emit_json / _fence_value / _s / apply_refusal). Content-carrying means either an INTERPOLATION
(a `%` expression, an f-string, or `.format()`) OR a SERIALIZER call (`json.dumps(...)` / any
`.dumps(...)`), which builds a terminal string out of arbitrary values just like an f-string.
Rationale:

  * a constant string cannot carry adversarial content - allowed;
  * a pre-fenced variable (`print(line)` where line already went through the fence) is a
    Name, not an interpolation - allowed (interpolation at the call site is what leaks);
  * an interpolation that DOES wrap a fence (`print(_s(f"..."))`) is allowed;
  * a bare interpolation (`print("x %s" % v)`, `sys.stderr.write(f"{e}")`) is the bug - fail;
  * a bare serialize (`print(json.dumps(x))`, `sys.stdout.write(json.dumps(out))`) is the SAME
    bug in a shape the first cut missed - fail unless wrapped (`json.dumps(_fence_value(x))`).

Content emitters must route through store.emit_text / store.emit_json (which fence with the
workspace refusal union) or wrap the string in store.fence_output / query._s. bin/ is scanned
too: the CLI dispatcher lives there and was a blind spot until the walk covered it.

DOCUMENTED exceptions, each for a structural reason (not convenience):
  * doctor.py - a diagnostics tool whose output is install-local probe results (paths,
    versions, its own pass/warn/fail counts). It must run against a broken or absent store,
    so it cannot depend on the workspace-union fence; it never emits stored content.
  * scrub.py  - the scrub PRIMITIVE that store.py imports; it cannot import store back
    without a cycle, and its only output is a redaction COUNT in a stdin-pipe __main__.
  * enrich_prep.py, enrich_apply.py - inter-stage JSON HANDSHAKE (each stage's stdout is
    json.loads-parsed by the next), not a human terminal: the emitted values are FUNCTIONAL
    keys/ids/counts. The AST guard cannot reason about a json.dumps handshake, so these stay
    excluded from IT - but every inbox echo IS fenced at the emit site (round-4 4a closed the
    success-vs-error sibling): the SUCCESS echo runs store.fence_output over the assembled path
    (byte-identical when clean; redacted + inbox_withheld=True when a workspace term coincides
    with a %TEMP% component - operator-environment path components CAN be codenames, the round-4
    counterexample to the old machine-local assumption - and the workflow stops the fan-out on it
    loudly), the _die ERROR path fences identically, and the functional default path never rides
    the echo at all: enrich_apply re-derives it from --cwd via the shared default_inbox
    derivation. Backed by test_enrich_prep_guard.py: F6 (a fenced --inbox refuses, echoed
    fenced), M2 (a fenced project NAME sanitizes, never refuses), and the 4a cases (a clean
    default echoes byte-identical; a term-bearing temp prefix redacts and withholds).
  * enrich.py - its one content-bearing surface, --list-pending, is already fenced AT THE SOURCE:
    list_pending() runs every emitted string through the workspace refusal union + a PII scrub
    before it prints (the _out closure), so an AST-level guard here would be redundant, not
    missing. Backed by test_metadata_fence.py case (4), which asserts one project's refusal term
    never surfaces in another project's enrich pending output.

A second, narrower check rides along: no fence may be applied INSIDE an output sink. The sinks
fence their own argument, and fence_output's "idempotent - safe to apply more than once" contract
is FALSE for any refusal term that is a substring of the marker word - the second pass matches
inside the `<redacted>` the first emitted and rewrites it. Convergence pass 2 hit this for real on
the migrate manifest; two sites in pcx.py were the same shape waiting for the same term.

STATED LIMITS (a guard that overstates its reach is worse than a smaller honest one):
  * the double-fence check sees a fence nested in the sink call and a fence hoisted into a local
    bound in the SAME function scope (`_own_scope_walk`, function-precise since convergence pass
    3 - a name fenced in one function no longer blesses an unrelated same-named local in another).
    A fence result carried through a helper call, stashed in a dict/list, or imported from another
    module is NOT tracked - that needs real dataflow, not an AST pass.
  * the unfenced-output check reasons about SHAPE, not taint: `print(some_var)` is allowed as a
    WHOLE top-level sink argument because a pre-fenced variable is the sanctioned idiom, so a
    variable that was never fenced passes too there. That concession does NOT extend to a value
    SLOT inside an interpolation (convergence pass 3 - the old rule let one fenced slot bless
    every OTHER slot in the same f-string/%%-op/`.format()`; a slot must now itself be a constant,
    a direct fence call, or a recursively-covered interpolation/serializer, or the interpolation's
    RESULT must pass through an ancestor fence). Closing the remaining top-level-bare-Name gap is
    a v1.1 discover-not-list taint guard; this guard covers the interpolate-at-the-call-site
    class only.

The guard SELF-TESTS its teeth: it proves each detector fires on known-bad samples (every shape)
and stays silent on known-good ones, so a defanged detector fails loudly instead of vacuously.
"""
import ast
import os

HERE = os.path.dirname(os.path.abspath(__file__))            # scripts/
BIN = os.path.join(os.path.dirname(HERE), "bin")             # bin/ - sibling of scripts/
FENCE_NAMES = {"fence_output", "emit_text", "emit_json", "_fence_value", "_s", "apply_refusal"}
SINK_NAMES = {"emit_text", "emit_json"}      # fence their own argument - never fence one twice
# structural exceptions - see the module docstring for why each cannot route through the sink.
EXCLUDE = {"doctor.py", "scrub.py", "enrich.py", "enrich_prep.py", "enrich_apply.py"}


def _is_interpolated(node):
    """True when the expression inlines a runtime value into text: `"..." % x`, an f-string,
    or `"...".format(...)`. These are the shapes that can carry content out."""
    if isinstance(node, ast.JoinedStr):                                  # f-string
        return True
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):     # "..." % x
        return True
    if isinstance(node, ast.Call):                                       # "...".format(...)
        f = node.func
        if isinstance(f, ast.Attribute) and f.attr == "format":
            return True
    return False


def _is_serializer(node):
    """True when the expression is a `json.dumps(...)` / any `.dumps(...)` call - a serializer that
    builds a terminal string out of arbitrary runtime values, the same content-carry an f-string is.
    `json.dump(obj, fp)` (writes to a file, returns None) is not here: it is never a print/write arg."""
    if isinstance(node, ast.Call):
        f = node.func
        name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
        return name == "dumps"
    return False


def _call_name(node):
    """The bare callee name for a Call ('fence_output' for store.fence_output), else None."""
    if not isinstance(node, ast.Call):
        return None
    f = node.func
    return f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)


def _serializer_is_covered(node):
    """A serializer call is covered when its object argument is itself directly a fence call (the
    sanctioned whole-object-fenced shape, pcx.py:77's `json.dumps(_fence_value(obj, terms))` - the
    only in-tree stream-bound json.dumps) or, when that argument is a Dict/List/Tuple LITERAL, when
    every one of its own values/elements is independently covered (recursively, the same standard
    an interpolation slot is held to - see _slot_is_covered). This replaces the old
    any-fence-anywhere-in-subtree rule (convergence pass 4 MINOR): that rule walked the WHOLE
    serializer call including sibling dict entries, so a fence on one key blessed an unfenced peer
    - `json.dumps({"name": fence_output(a), "note": secret})` passed with `secret` riding out raw,
    the exact sibling-blessing shape pass 2 closed for interpolations and pass 3 closed one level
    down for interpolation slots, left open here for the one node type with the same failure mode.
    An object argument that is neither a direct fence call nor a Dict/List/Tuple literal (a bare
    Name, a general Call whose return isn't itself a fence) has no slots to decompose and no
    whole-object fence to trust, so it is NOT covered - the same documented, deliberate limit
    _slot_is_covered already states for a bare top-level interpolation slot."""
    if not node.args:
        return False
    obj = node.args[0]
    if _call_name(obj) in FENCE_NAMES:
        return True
    if isinstance(obj, ast.Dict):
        return all(_slot_is_covered(v) for v in obj.values if v is not None)
    if isinstance(obj, (ast.List, ast.Tuple)):
        return all(_slot_is_covered(e) for e in obj.elts)
    return False


def _interpolation_slots(node):
    """The individual runtime-value expressions an interpolation substitutes: each FormattedValue
    in an f-string, each element of a %-operator's right-hand tuple (or dict, for %(name)s) or the
    lone right operand for a single %s, and each positional/keyword argument to .format()."""
    if isinstance(node, ast.JoinedStr):
        return [v.value for v in node.values if isinstance(v, ast.FormattedValue)]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
        rhs = node.right
        if isinstance(rhs, ast.Tuple):
            return list(rhs.elts)
        if isinstance(rhs, ast.Dict):
            return [v for v in rhs.values if v is not None]
        return [rhs]
    if isinstance(node, ast.Call):
        f = node.func
        if isinstance(f, ast.Attribute) and f.attr == "format":
            return list(node.args) + [k.value for k in node.keywords if k.value is not None]
    return []


def _slot_is_covered(slot):
    """True when a single interpolation VALUE SLOT is provably safe on its own: a literal, a
    direct call to a known fence, or (recursively) a further interpolation/serializer whose own
    slots are all covered. Deliberately NOT extended to a bare Name/Attribute/other Call, even
    though a bare top-level `print(var)` argument IS trusted elsewhere (the guard's own stated,
    documented limit) - once ANY sibling slot in the SAME interpolation is visibly fenced, an
    uncovered peer is exactly the shape convergence pass 3 found live at query.py:505
    (`f"{_s(p.name)} ({len(act)} ...)"`- benign only because the uncovered peers happened to be
    len() ints; the guard cannot tell that from a slot that turns out to hold a codename)."""
    if isinstance(slot, ast.Constant):
        return True
    if _call_name(slot) in FENCE_NAMES:
        return True
    if _is_interpolated(slot):
        return all(_slot_is_covered(s) for s in _interpolation_slots(slot))
    if _is_serializer(slot):
        return _serializer_is_covered(slot)
    return False


def _has_unfenced_content(node, under_fence=False):
    """True when this expression carries content to the terminal with no fence covering IT.

    Coverage is per-NODE, not per-argument, and that distinction is the fix for a real hole
    (convergence pass 2): the old check asked only whether a fence appeared ANYWHERE in the
    argument's subtree, so one fenced operand blessed its unfenced peer and
    `print(store.fence_output(a) + json.dumps(b))` passed with b riding out raw. A fence covers a
    content node only when it is an ANCESTOR of it (the fence runs over the finished string) or
    lives INSIDE it (json.dumps over already-fenced values). A fence in a SIBLING branch covers
    nothing, which is exactly what the old walk could not express.

    The recursion is load-bearing for a second reason, from convergence pass 1: checking only an
    argument's TOP-level node let the tree's own commonest idiom launder a violation invisibly -
    `sys.stdout.write(json.dumps(out) + "\\n")` is a BinOp at the top, so a shape-shallow check saw
    neither a dumps nor an interpolation and passed the exact bug class this guard exists to stop.

    An INTERPOLATION node is checked per-SLOT (convergence pass 3, the sibling-branch fix's own
    commonest twin): the old rule cleared a JoinedStr/%%-op/`.format()` call the moment ANY fence
    call appeared anywhere in its subtree, so `f"{fence_output(a)} {secret}"` passed with `secret`
    riding out raw - the exact `bad_peer` shape the pass-2 fix claims to close, left open one level
    down (peer VALUES within the same interpolation, not just peer BRANCHES of a bigger expression).
    A SERIALIZER node is checked per-SLOT too (convergence pass 4 MINOR, the interpolation
    per-slot fix's own twin): `json.dumps(_fence_value(obj))` fences the whole object before
    serializing it (the tree's one sanctioned whole-object shape, still trusted directly), but a
    Dict/List/Tuple literal argument decomposes into its own values/elements exactly like an
    interpolation's slots - see _serializer_is_covered."""
    if _call_name(node) in FENCE_NAMES:
        under_fence = True
    elif not under_fence and _is_interpolated(node) \
            and not all(_slot_is_covered(s) for s in _interpolation_slots(node)):
        return True
    elif not under_fence and _is_serializer(node) and not _serializer_is_covered(node):
        return True
    for child in ast.iter_child_nodes(node):
        if _has_unfenced_content(child, under_fence):
            return True
    return False


def _output_target(node):
    """'print' / 'sys.stdout.write' / 'sys.stderr.write' for a matching Call, else None.
    A bare `stream.write` (the sink internals in store.py) is NOT a target - only the two
    literal sys streams and print()."""
    f = node.func
    if isinstance(f, ast.Name) and f.id == "print":
        return "print"
    if isinstance(f, ast.Attribute) and f.attr == "write":
        base = f.value
        if isinstance(base, ast.Attribute) and isinstance(base.value, ast.Name) \
                and base.value.id == "sys" and base.attr in ("stdout", "stderr"):
            return "sys.%s.write" % base.attr
    return None


def _violations(src, filename="<src>"):
    tree = ast.parse(src, filename=filename)
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        tgt = _output_target(node)
        if tgt is None or not node.args:
            continue
        # EVERY argument is checked, not only the first (round-4 4c: `print("prefix:",
        # json.dumps(x))` carries content in position 2, and a constant first arg must not bless
        # the whole call), KEYWORDS included - print's sep=/end= are interpolated into the emitted
        # line exactly like a positional, and nothing structural stopped a leak from moving there.
        # Coverage is judged per-node inside each argument (see _has_unfenced_content).
        parts = list(node.args) + [k.value for k in node.keywords if k.value is not None]
        if any(_has_unfenced_content(a) for a in parts):
            out.append((getattr(node, "lineno", "?"), tgt))
    return out


def _cli_modules():
    """Discovered, not listed: every non-test .py under scripts/ AND bin/ minus the documented
    exceptions. Full paths (bin/ and scripts/ can share a basename); callers key by basename."""
    out = []
    for d in (HERE, BIN):
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if fn.endswith(".py") and not fn.startswith("test_") and fn not in EXCLUDE:
                out.append(os.path.join(d, fn))
    return sorted(out)


def test_no_unfenced_interpolated_output():
    problems = {}
    for path in _cli_modules():
        with open(path, encoding="utf-8") as fh:
            v = _violations(fh.read(), os.path.basename(path))
        if v:
            problems[os.path.basename(path)] = v
    assert not problems, (
        "content-carrying output (interpolation OR json.dumps) must route through "
        "store.emit_text/emit_json or wrap the string in store.fence_output / _s "
        "(constants and pre-fenced variables are fine): %r" % problems)


def _own_scope_walk(node):
    """Yield every descendant belonging to `node`'s OWN scope - like ast.walk, but does not
    descend PAST a nested function boundary. `ast.walk(tree)` already visits everything in every
    nested function, so iterating it once from the module root and again from each FunctionDef
    (the pre-fix approach) is pure redundancy on the module entry's own account, AND a correctness
    bug on the function entries': the module-level pass's `fenced_names` is the union of every
    binding in the WHOLE FILE, so a name fenced in one function blesses an unrelated SAME-NAMED
    local in a completely different function (convergence pass 3 MAJOR - live in enrich_prep.py's
    rebinding pattern). Scoping the walk itself, not just re-iterating scope roots, is what
    actually separates one function's bindings from another's."""
    for child in ast.iter_child_nodes(node):
        yield child
        if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield from _own_scope_walk(child)


def _bound_names(target):
    """Every Name a single assignment target binds - a plain Name, or (recursively) each element
    of a Tuple/List unpacking target (`a, b = ...`). MINOR, convergence pass 3: the hoisted-fence
    tracking below only ever checked `isinstance(t, ast.Name)`, so `a, b = store.fence_output(p)`
    silently bound NOTHING - a fenced local via tuple-unpack was invisible to the double-fence
    check, the opposite of a false positive: a real double-fence through that binding shape would
    pass silently."""
    if isinstance(target, ast.Name):
        yield target.id
    elif isinstance(target, (ast.Tuple, ast.List)):
        for elt in target.elts:
            yield from _bound_names(elt)


def _double_fences(src, filename="<src>"):
    """Sites where already-fenced text is handed to a sink, which fences it a SECOND time.

    Two shapes, because the tree contained one of each. The nested form
    (`emit_text("%s" % store.fence_output(p))`) is visible in the call's own subtree. The
    hoisted form (`d = store.fence_output(p)` on one line, `emit_text(... % d)` on the next)
    is not, so the walk also carries the set of local names bound to a fence result and flags
    a sink that consumes one. Binding is tracked per enclosing scope via `_own_scope_walk`, which
    is where the real pair lived and where a same-named binding in an UNRELATED scope must NOT
    reach; a fence result passed through a call, a container, or another module would still be
    invisible here (see the module docstring's stated limits). Three assignment SHAPES bind a
    hoisted fence result (MINOR, convergence pass 3 - only plain `ast.Assign` was tracked before):
    `d = fence(p)`, an annotated `d: str = fence(p)`, and a walrus `(d := fence(p))`; `_bound_names`
    additionally unpacks a tuple/list target on the plain-Assign shape."""
    tree = ast.parse(src, filename=filename)
    out = []
    scopes = [tree] + [n for n in ast.walk(tree)
                       if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    for scope in scopes:
        fenced_names = set()
        for n in _own_scope_walk(scope):
            if isinstance(n, ast.Assign) and _call_name(n.value) in FENCE_NAMES:
                for t in n.targets:
                    fenced_names.update(_bound_names(t))
            elif (isinstance(n, ast.AnnAssign) and n.value is not None
                  and _call_name(n.value) in FENCE_NAMES):
                fenced_names.update(_bound_names(n.target))
            elif isinstance(n, ast.NamedExpr) and _call_name(n.value) in FENCE_NAMES:
                fenced_names.update(_bound_names(n.target))
        for node in _own_scope_walk(scope):
            if _call_name(node) not in SINK_NAMES:
                continue
            for arg in list(node.args) + [k.value for k in node.keywords if k.value is not None]:
                for n in ast.walk(arg):        # an ARGUMENT's own subtree - not a scope crossing
                    if _call_name(n) in FENCE_NAMES:
                        out.append((getattr(node, "lineno", "?"), _call_name(n)))
                    elif isinstance(n, ast.Name) and n.id in fenced_names:
                        out.append((getattr(node, "lineno", "?"), n.id))
    return sorted(set(out))


def test_no_double_fenced_output():
    """A fence inside a sink is not belt-and-braces, it is a corruption bug. fence_output's
    "idempotent - safe to apply more than once" contract is FALSE for any refusal term that is a
    substring of the marker word: the second pass matches INSIDE the `<redacted>` the first pass
    emitted and rewrites it (`<red<redacted>ed>`), on every line carrying a redaction. The migrate
    manifest hit this for real in convergence pass 2; two `emit_text(... fence_output(...))` sites
    in pcx.py were the same shape waiting for the same term. One sink, one pass."""
    problems = {}
    for path in _cli_modules():
        with open(path, encoding="utf-8") as fh:
            d = _double_fences(fh.read(), os.path.basename(path))
        if d:
            problems[os.path.basename(path)] = d
    assert not problems, (
        "output sinks fence their own argument - remove the inner fence call, a second pass "
        "corrupts the redaction marker for marker-substring terms: %r" % problems)


def test_discovers_the_real_cli_modules():
    """The glob must actually see the store-coupled CLI handlers AND the bin/ dispatcher - a guard
    that silently discovers nothing (or misses bin/) would pass vacuously."""
    found = {os.path.basename(p) for p in _cli_modules()}
    for expected in ("pcx.py", "query.py", "calibrate.py", "parsica-weave.py"):
        assert expected in found, "guard failed to discover %s" % expected
    assert not (EXCLUDE & found), "excluded modules must not be scanned"


def test_guard_has_teeth():
    bad = ('print("hello %s" % name)\n'
           'sys.stdout.write("x=%s" % v)\n'
           'sys.stderr.write(f"{err}")\n'
           'print("{}".format(v))\n')
    assert len(_violations(bad)) == 4, "detector must flag every bare interpolation"

    # the json.dumps shape - the same bug the first cut missed (pcx.py:1277, the enrich pipeline)
    bad_dumps = ('print(json.dumps({"k": v}))\n'
                 'sys.stdout.write(json.dumps(out))\n'
                 'sys.stderr.write(json.dumps(err))\n')
    assert len(_violations(bad_dumps)) == 3, "detector must flag every unfenced json.dumps to a stream"

    # content in a NON-FIRST position (round-4 4c): a constant first arg must not bless the call
    bad_pos = ('print("prefix:", "x=%s" % v)\n'
               'print("state:", json.dumps(d))\n'
               'print(label, f"{v}")\n')
    assert len(_violations(bad_pos)) == 3, "detector must flag content-carrying args in any position"

    # content NESTED inside the argument expression (convergence pass 1): the tree's own
    # trailing-newline concat idiom must not launder a bare dumps/interpolation past the check
    bad_nested = ('sys.stdout.write(json.dumps(out) + "\\n")\n'
                  'sys.stderr.write("err: " + ("%s" % e) + "\\n")\n'
                  'print(("a" if c else json.dumps(d)))\n')
    assert len(_violations(bad_nested)) == 3, \
        "detector must flag content-carrying expressions nested inside an argument"

    # a fence in a SIBLING branch must not bless its unfenced peer (convergence pass 2): the
    # old any-fence-in-the-argument-subtree test passed every one of these with content raw.
    bad_peer = ('print(store.fence_output(a) + json.dumps(b))\n'
                'sys.stdout.write(_s(a) + ("%s" % b))\n'
                'print(store.fence_output(a) if c else f"{b}")\n')
    assert len(_violations(bad_peer)) == 3, \
        "a fence over one operand must not clear an unfenced peer operand"

    # content moved into a KEYWORD argument - print's sep=/end= reach the terminal too
    bad_kw = ('print("x", end="%s" % v)\n'
              'print("x", sep=json.dumps(d))\n')
    assert len(_violations(bad_kw)) == 2, "detector must scan keyword arguments"

    # a fence over ONE VALUE inside an interpolation must not clear its raw PEER value in the
    # SAME interpolation (convergence pass 3's own commonest twin: bad_peer above closes the
    # sibling-BRANCH shape; this is the sibling-SLOT shape, one level down - live in the tree
    # today at query.py:505 before the fix, benign only because the uncovered peers were ints).
    # All three interpolation shapes (f-string, %-tuple, .format) get the same attack.
    bad_peer_slot = ('print(f"{store.fence_output(a)} {secret}")\n'
                      'print("%s %s" % (store.fence_output(a), secret))\n'
                      'print("{} {}".format(store.fence_output(a), secret))\n')
    assert len(_violations(bad_peer_slot)) == 3, \
        "a fence over one interpolation SLOT must not clear an unfenced peer slot"

    # a fence over one json.dumps DICT/LIST VALUE must not clear an unfenced peer value
    # (convergence pass 4 MINOR, the interpolation per-slot fix's own twin for the one node type
    # it didn't cover): the old any-fence-anywhere-in-subtree rule blessed the WHOLE serializer
    # call the moment ANY fence appeared in its argument, so both of these passed with `secret`
    # riding out raw.
    bad_dumps_peer = ('print(json.dumps({"name": store.fence_output(a), "note": secret}))\n'
                       'sys.stdout.write(json.dumps([store.fence_output(a), secret]))\n')
    assert len(_violations(bad_dumps_peer)) == 2, \
        "a fence over one json.dumps dict/list slot must not clear an unfenced peer slot"

    good = ('print("a constant line")\n'                     # constant
            'store.emit_text("x %s" % v)\n'                  # routed through the sink
            'store.emit_json({"k": v})\n'                    # json sink
            'print(store.fence_output("y %s" % v))\n'        # wrapped in fence_output
            'sys.stdout.write(_s(f"z {v}"))\n'               # wrapped in _s
            'sys.stdout.write(json.dumps(_fence_value(obj, terms)))\n'  # json.dumps wrapped in a fence
            'print(json.dumps({"name": store.fence_output(a), "kind": "info"}))\n'  # every dict value covered
            'print(json.dumps([store.fence_output(a), store.fence_output(b)]))\n'   # every list elt covered
            'print(already_fenced_line)\n'                   # pre-fenced variable (a Name)
            'print("a:", store.fence_output("x %s" % v))\n'  # fenced content in a later position
            'print("total:", n)\n'                           # bare Name in a later position (trusted)
            'print(store.fence_output(a) + store.fence_output("y %s" % v))\n'  # BOTH operands fenced
            'print("x", end="\\n", file=sys.stderr)\n'        # constant/Name keywords are fine
            'name = store.fence_output(p.name)\n'             # the query.py:505 fix idiom:
            'line = f"{name} ({len(act)} active)"\n'          # assemble mixed fenced+harmless
            'print(line)\n'                                   # content first, print the bare Name
            'print(f"{store.fence_output(a)} {store.fence_output(b)}")\n')  # every slot covered
    assert _violations(good) == [], "detector must not flag fenced/constant output or the sinks"

    # the double-fence detector, both shapes and both directions
    assert len(_double_fences('store.emit_text("a %s" % store.fence_output(p))\n'
                              'store.emit_json({"k": _fence_value(v)})\n')) == 2, \
        "detector must flag a fence nested inside a sink"
    hoisted = ('def f():\n'
               '    disp = store.fence_output(pkey)\n'
               '    store.emit_text("scope %s" % disp)\n')
    assert len(_double_fences(hoisted)) == 1, \
        "detector must flag a fence HOISTED into a local and then handed to a sink"
    assert _double_fences('store.emit_text("a %s" % p)\n'
                          'store.emit_json({"k": v})\n'
                          'print(store.fence_output(p))\n'
                          'def g():\n'
                          '    d = store.fence_output(p)\n'
                          '    print(d)\n') == [], \
        "a sink with no inner fence, and a fence used OUTSIDE a sink, are both correct"

    # convergence pass 3 MAJOR: a name fenced in ONE function must not bless a SAME-NAMED,
    # never-fenced local in an UNRELATED function (the enrich_prep.py rebinding shape) - the
    # pre-fix module-level scope walk collected the whole file's bindings into one flat set.
    cross_fn = ('def f():\n'
               '    d = store.fence_output(p)\n'
               '    store.emit_text("scope %s" % d)\n'
               'def g():\n'
               '    d = compute_something(p)\n'
               '    store.emit_text("scope %s" % d)\n')
    hits = _double_fences(cross_fn)
    assert len(hits) == 1 and hits[0][0] == 3, \
        "f's genuinely-double-fenced line must flag, g's same-named-but-never-fenced local " \
        "must NOT - got %r" % hits
    # and the SAME rebinding pattern must still be reachable at module scope directly (no
    # function wrapper at all) - the fix must not accidentally require a function to detect.
    assert len(_double_fences('d = store.fence_output(p)\n'
                              'store.emit_text("scope %s" % d)\n')) == 1, \
        "module-level (no enclosing function) double-fencing must still be caught"

    # MINOR, convergence pass 3: only plain `ast.Assign` was tracked as a hoisted-fence binding.
    # An annotated assignment, a walrus, and a tuple-unpack target were all invisible - each is a
    # FALSE NEGATIVE (a real double-fence through that shape would pass silently), not a false
    # positive, so each case here is a genuinely double-fenced line that must be flagged.
    assert len(_double_fences('def f():\n'
                              '    disp: str = store.fence_output(pkey)\n'
                              '    store.emit_text("scope %s" % disp)\n')) == 1, \
        "an ANNOTATED hoisted assignment (`d: str = fence(p)`) must still be tracked"
    assert len(_double_fences('def f():\n'
                              '    if (disp := store.fence_output(pkey)):\n'
                              '        store.emit_text("scope %s" % disp)\n')) == 1, \
        "a WALRUS hoisted assignment (`(d := fence(p))`) must still be tracked"
    assert len(_double_fences('def f():\n'
                              '    disp, other = store.fence_output(pkey)\n'
                              '    store.emit_text("scope %s" % disp)\n')) == 1, \
        "a TUPLE-UNPACK hoisted assignment (`a, b = fence(p)`) must still track EACH bound name"
    # false-positive companion: the unpacked but never-consumed-by-a-sink name must not itself
    # spuriously trip anything, and a name that merely happens to share a fenced name's spelling
    # in an ANNOTATED/walrus form elsewhere must still respect the per-scope isolation proven above.
    assert _double_fences('def f():\n'
                          '    disp: str = compute_something(pkey)\n'
                          '    store.emit_text("scope %s" % disp)\n') == [], \
        "an annotated assignment that is NOT a fence call must not be treated as one"

    # MINOR, convergence pass 3: the double-fence detector's keyword-argument scanning had no
    # DEDICATED test (the unfenced-output detector's `bad_kw` above only covers the sibling
    # `_violations` check). Both shapes: a fence NESTED inside a keyword argument's own subtree,
    # and a fence HOISTED into a local that a keyword argument then consumes.
    assert len(_double_fences(
        'store.emit_text("x", end=store.fence_output(p))\n')) == 1, \
        "a fence nested inside a KEYWORD argument must be flagged the same as a positional one"
    assert len(_double_fences(
        'def f():\n'
        '    disp = store.fence_output(p)\n'
        '    store.emit_text("x", end=disp)\n')) == 1, \
        "a fence hoisted into a local and consumed via a KEYWORD argument must be flagged"
