"""Structural guard: every refusal-term application routes through the ONE longest-first compiler.

WHY THIS EXISTS. The same defect was found and fixed three separate times, in three separate
places, by three separate manual sweeps:

    scrub._compile_refusal        - fixed when a token beat the phrase containing it
    Spirit promote._apply_refusal - fixed when a per-term loop rewrote a marker it had just minted
    enrich_prep._safe_default_slug - found only after it stranded 16 of 21 characters of a fenced
                                     phrase into a %TEMP% directory name that transited stdout

Three siblings means manual sweeping failed twice. Hand-listing the known sites would fail a third
time, because the whole failure mode is a site nobody thought to list. So this guard DISCOVERS
application sites from the source itself and asserts each one routes through the shared primitive.
A fourth sibling becomes structurally impossible rather than eventually found.

THE RULE. Applying refusal terms per-term is banned everywhere except inside the sanctioned
compiler. Sequential application is not a style preference: with terms ["vela", "vela-lyra"], the
shorter term matches first, consumes its own prefix, and leaves "-lyra" behind - a fragment of a
fenced phrase, in output, that the whole-term oracle cannot recognise as a leak.

WHAT IT WOULD MISS, stated rather than implied: a site that applies terms without any loop and
without naming them "term" (e.g. `re.sub("|".join(x), ...)` over an alias) is not detected. The
guard also cannot see dynamic construction through getattr or exec. It covers the shape all three
real defects took, and the self-test below proves it detects that shape.
"""
import ast
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

#: The ONE place allowed to walk terms individually. Anything else touching terms per-term is a
#: sibling waiting to happen. Named, not path-based, so moving the function does not silently
#: exempt a new file.
SANCTIONED = {"_compile_refusal", "compile_refusal"}

#: A name that plausibly holds refusal terms. Deliberately broad: a false positive here costs a
#: comment explaining why a site is fine, while a false negative costs a fenced codename in output.
_TERMY = re.compile(r"term|refus|fence", re.IGNORECASE)


def _is_termy(node):
    """Does this expression plausibly evaluate to a collection of refusal terms?"""
    if isinstance(node, ast.Name):
        return bool(_TERMY.search(node.id))
    if isinstance(node, ast.Attribute):
        return bool(_TERMY.search(node.attr))
    if isinstance(node, ast.Call):
        f = node.func
        name = getattr(f, "attr", None) or getattr(f, "id", "")
        return bool(_TERMY.search(name or ""))
    if isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp)):
        return any(_is_termy(g.iter) for g in node.generators)
    if isinstance(node, ast.Subscript):
        return _is_termy(node.value)
    # `for t in (terms or []):` is THIS CODEBASE'S HOUSE STYLE - _compile_refusal itself is written
    # that way - and the first cut of this guard was blind to it, because a BoolOp is neither a Name
    # nor a Call. A guard blind to the idiom its own repo prefers protects nothing: every historical
    # revision of the real defect used exactly this shape. Same reasoning for a ternary.
    if isinstance(node, ast.BoolOp):
        return any(_is_termy(v) for v in node.values)
    if isinstance(node, ast.IfExp):
        return _is_termy(node.body) or _is_termy(node.orelse)
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return any(_is_termy(e) for e in node.elts)
    if isinstance(node, ast.Starred):
        return _is_termy(node.value)
    return False


def _substitution_calls(node):
    """Every regex-build or substitution call inside this subtree, as (callname, node)."""
    out = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            f = sub.func
            name = getattr(f, "attr", None) or getattr(f, "id", "")
            if name in ("sub", "subn", "escape", "replace", "compile"):
                out.append((name, sub))
    return out


def _names_in(node):
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def _loop_targets(target):
    if isinstance(target, ast.Name):
        return {target.id}
    return {n.id for n in ast.walk(target) if isinstance(n, ast.Name)}


def find_violations(src, path):
    """Return [(line, why)] for every per-term application outside the sanctioned compiler."""
    tree = ast.parse(src)
    violations = []

    # functions we are allowed to skip, by name
    sanctioned_nodes = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in SANCTIONED:
            sanctioned_nodes.update(id(n) for n in ast.walk(node))

    for node in ast.walk(tree):
        if not isinstance(node, ast.For) or id(node) in sanctioned_nodes:
            continue
        if not _is_termy(node.iter):
            continue
        targets = _loop_targets(node.target)
        for callname, call in _substitution_calls(node):
            used = set()
            for arg in list(call.args) + [kw.value for kw in call.keywords]:
                used |= _names_in(arg)
            if targets & used:
                violations.append((
                    getattr(call, "lineno", node.lineno),
                    "per-term %s() using loop variable %r - route through %s instead"
                    % (callname, ", ".join(sorted(targets & used)), " / ".join(sorted(SANCTIONED))),
                ))
    return violations


def scan_files():
    """Every shipped non-test python file in this repo, discovered, never enumerated."""
    out = []
    for root, dirs, files in os.walk(HERE):
        dirs[:] = [d for d in dirs if d not in ("__pycache__", ".git")]
        for fn in sorted(files):
            if fn.endswith(".py") and not fn.startswith("test_"):
                out.append(os.path.join(root, fn))
    return out


def _sanctioned_compiler_is_longest_first():
    """The primitive itself must not silently lose the property everything else delegates to it.

    Routing every site through one function is worth nothing if that function stops sorting. Checked
    behaviourally, not by grepping for `key=len`: a phrase must beat a token it contains.
    """
    import scrub
    pat = scrub._compile_refusal(["corp", "corpsecret"])
    if pat is None:
        return False, "compiler returned None for a non-empty term list"
    got = pat.sub("-", "corpsecret")
    if got != "-":
        return False, ("shorter term won: 'corpsecret' -> %r (a fragment of the fenced phrase "
                       "survives)" % got)
    if scrub._compile_refusal([]) is not None:
        return False, "compiler must return None for an empty term list"
    return True, ""


def run():
    fails = []

    files = scan_files()
    if len(files) < 5:
        fails.append("discovery found only %d source files - the scan is broken, not the code"
                     % len(files))

    scanned = 0
    for path in files:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                src = fh.read()
        except OSError:
            continue
        scanned += 1
        for line, why in find_violations(src, path):
            fails.append("%s:%d %s" % (os.path.relpath(path, HERE), line, why))

    ok, why = _sanctioned_compiler_is_longest_first()
    if not ok:
        fails.append("the sanctioned compiler is no longer longest-first: %s" % why)

    # --- SELF-TEST: the guard must DETECT the shape it exists to ban -----------------------------
    # Without this, deleting the detector body leaves a green suite that proves nothing. The three
    # real defects all took this shape.
    BAD = (
        "import re\n"
        "def slug(terms, raw):\n"
        "    for term in terms:\n"
        "        raw = re.sub(re.escape(term), '-', raw)\n"
        "    return raw\n"
    )
    if not find_violations(BAD, "<planted>"):
        fails.append("SELF-TEST: the guard did not flag a planted per-term substitution loop")

    BAD2 = (
        "def slug(workspace_refusal_terms, raw):\n"
        "    for t in workspace_refusal_terms():\n"
        "        raw = raw.replace(t, '-')\n"
        "    return raw\n"
    )
    if not find_violations(BAD2, "<planted>"):
        fails.append("SELF-TEST: the guard missed a per-term str.replace loop")

    # The house-style guard-around-empty form. Every historical revision of the real defect was
    # written this way, and the first cut of this guard did not flag it - so this vector exists to
    # keep that hole closed rather than to describe a hypothetical.
    BAD3 = (
        "import re\n"
        "def slug(terms, raw):\n"
        "    for t in (terms or []):\n"
        "        raw = re.sub(re.escape(t), '-', raw)\n"
        "    return raw\n"
    )
    if not find_violations(BAD3, "<planted>"):
        fails.append("SELF-TEST: the guard missed `for t in (terms or [])`, this repo's own idiom")

    BAD4 = (
        "import re\n"
        "def slug(cfg, raw):\n"
        "    for t in (cfg.refusal_terms if cfg else []):\n"
        "        raw = re.sub(re.escape(t), '-', raw)\n"
        "    return raw\n"
    )
    if not find_violations(BAD4, "<planted>"):
        fails.append("SELF-TEST: the guard missed a ternary-guarded terms expression")

    # --- FALSE-POSITIVE COMPANIONS: legitimate code must NOT be flagged --------------------------
    # A guard with no companion drifts toward flagging everything, and the first person to hit a
    # false alarm weakens the guard rather than the code.
    GOOD = (
        "import re\n"
        "def fence(terms, raw):\n"
        "    pat = _compile_refusal(terms)\n"
        "    return pat.sub('-', raw) if pat else raw\n"
    )
    if find_violations(GOOD, "<planted>"):
        fails.append("FALSE POSITIVE: the sanctioned single-alternation form was flagged")

    GOOD2 = (            # looping over terms is fine when not substituting per term
        "def count(terms, raw):\n"
        "    return sum(1 for term in terms if term in raw)\n"
    )
    if find_violations(GOOD2, "<planted>"):
        fails.append("FALSE POSITIVE: a non-substituting loop over terms was flagged")

    GOOD3 = (            # substitution unrelated to terms
        "import re\n"
        "def clean(paths, raw):\n"
        "    for p in paths:\n"
        "        raw = re.sub(re.escape(p), '-', raw)\n"
        "    return raw\n"
    )
    if find_violations(GOOD3, "<planted>"):
        fails.append("FALSE POSITIVE: a loop unrelated to refusal terms was flagged")

    if fails:
        print("FAIL - refusal routing guard (%d)" % len(fails))
        for f in fails:
            print("  -", f)
        return 1
    print("OK - %d source files scanned, every refusal application routes through %s"
          % (scanned, " / ".join(sorted(SANCTIONED))))
    return 0


def test_refusal_routing():
    assert run() == 0, "refusal routing guard failed - see captured stdout"


if __name__ == "__main__":
    sys.exit(run())
