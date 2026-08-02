#!/usr/bin/env python3
"""Every appended shared-doc entry must be its OWN markdown section.

WHY THIS EXISTS. A journal page carries no headings at all, and a lesson appended without one
falls under the trailing H2. Spirit's promote splits a doc into sections by heading and mints a
source id as `kind:date:occurrence:_h8(section_body)`. So when N entries share one section, every
append re-hashes the whole growing blob into a NEW id -> new pk -> new ledger slot -> a new durable
record CONTAINING ALL PRIOR ENTRIES, and the previous record is never retired (the update path
requires an existing ledger entry).

Measured on the live store before the fix: three copies of one lesson slot at 1801 / 4537 / 6713
chars, each ending in a later provenance stamp - snapshots of one accumulating section, not
versions of an edit. 4 forked slots, 6 extra live records. The trigger is APPENDING, which is this
tool's primary operation, so it compounds with normal use.

THE RULE. One appended entry == one section. An entry's id must therefore depend on ITS OWN body
and nothing else, which makes it stable while later entries land beside it.

WHAT THIS WOULD MISS, stated rather than implied: it does not prove Spirit retires anything, and it
does not repair records already forked (that is a separate, gated live backfill). It proves that
from the fix forward, an append stops minting new ids for entries that did not change.
Run: python test_doc_entry_sections.py
"""
import io
import ast
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

HERE = os.path.dirname(os.path.abspath(__file__))

#: The ONE helper allowed to shape an appended shared-doc entry. Named, not path-based, so moving
#: it does not silently exempt a new call site.
MINTER = "doc_entry_block"


class _Stdin(object):
    """handle_note reads sys.stdin.buffer; give it exactly that and nothing else."""

    def __init__(self, text):
        self.buffer = io.BytesIO(text.encode("utf-8"))


def _write_entry(pcx, kind, cwd, sess, body):
    real = sys.stdin
    out = sys.stdout
    sys.stdin = _Stdin(body)
    sys.stdout = io.StringIO()
    try:
        pcx.handle_note(["note", "--kind", kind, "--cwd", cwd, "--sess", sess])
    finally:
        sys.stdin = real
        sys.stdout = out


def _doc_ids(text, kind, date):
    """Spirit's own view of a doc: {source_record_id: body}. Imported lazily by the caller."""
    from parsica_spirit.promote import _doc_entries
    return dict(_doc_entries(text, kind, date))


def run():
    fails = []

    root = tempfile.mkdtemp(prefix="docsec_")
    cwd = tempfile.mkdtemp(prefix="docsec_proj_")
    os.environ["PARSICA_WEAVE_ROOT"] = root
    os.environ.pop("PARSICA_ISOLATED", None)
    import store
    import pcx
    try:
        import importlib
        importlib.reload(store)
        importlib.reload(pcx)
    except Exception:
        pass

    # --- 1. WRITER: one heading per appended entry, earlier text untouched -----------------------
    _write_entry(pcx, "journal", cwd, "sess0001", "First entry body alpha.")
    page = sorted((store.journal_dir()).glob("*.md"))
    if not page:
        fails.append("no journal page was written at all")
        print("FAIL - doc entry sections (%d)" % len(fails))
        for f in fails:
            print("  -", f)
        return 1
    page = page[0]
    after_one = page.read_text(encoding="utf-8")
    _write_entry(pcx, "journal", cwd, "sess0001", "Second entry body beta.")
    after_two = page.read_text(encoding="utf-8")

    heads = re.findall(r"(?m)^##\s+.+$", after_two)
    if len(heads) != 2:
        fails.append("expected 2 entry headings after 2 appends, found %d: %r" % (len(heads), heads))
    if not after_two.startswith(after_one.rstrip()[: len(after_one.rstrip()) // 2]):
        fails.append("appending rewrote earlier bytes; appends must be strictly additive")
    if "First entry body alpha." not in after_two or "Second entry body beta." not in after_two:
        fails.append("an entry body did not survive the append")

    # --- 2/3/4. THE DEFECT, end to end, through Spirit's own sectioniser -------------------------
    try:
        from parsica_spirit.promote import _doc_entries        # noqa: F401
        have_spirit = True
    except ImportError:
        have_spirit = False
        print("note (skip) Parsica Spirit not importable - id-stability checks not run")

    if have_spirit:
        date = page.stem
        ids_two = _doc_ids(after_two, "journal", date)
        if len(ids_two) != 2:
            fails.append("Spirit saw %d sections in a 2-entry page (expected 2): %r"
                         % (len(ids_two), sorted(ids_two)))

        _write_entry(pcx, "journal", cwd, "sess0001", "Third entry body gamma.")
        after_three = page.read_text(encoding="utf-8")
        ids_three = _doc_ids(after_three, "journal", date)

        # THE regression this file exists for: earlier entries keep their ids.
        vanished = set(ids_two) - set(ids_three)
        if vanished:
            fails.append("appending a 3rd entry CHANGED the id of an earlier one (%d): %s"
                         % (len(vanished), sorted(vanished)[:2]))
        # ...and their bodies did not absorb the newcomer.
        for rid, body in ids_two.items():
            if rid in ids_three and "gamma" in ids_three[rid]:
                fails.append("an earlier entry's body absorbed a later append: %s" % rid)

        # FALSE-POSITIVE COMPANION: a genuinely new entry must still mint a NEW id. Without this a
        # "fix" that collapsed every entry onto one constant id would pass the check above.
        fresh = set(ids_three) - set(ids_two)
        if len(fresh) != 1:
            fails.append("a genuinely new entry should mint exactly 1 new id, got %d" % len(fresh))

        # LESSONS: the heading carries the date, so entries stop landing in the 'undated' bucket
        # (every lesson this session promoted as lesson:undated:0, all colliding in one slot).
        _write_entry(pcx, "lesson", cwd, "sess0001", "A lesson body delta.")
        ltext = store.lessons_path().read_text(encoding="utf-8")
        lids = _doc_ids(ltext, "lesson", None)
        mine = [k for k, v in lids.items() if "delta" in v]
        if not mine:
            fails.append("the appended lesson was not visible as its own section")
        elif any(":undated:" in k for k in mine):
            fails.append("lesson still lands undated - the heading must carry its date: %r" % mine)

    # --- 5. STRUCTURAL: no shared-doc append may skip the one entry minter ----------------------
    def _append_violations(src):
        """append_doc(path, text, ...) calls whose text neither comes from the minter, nor opens a
        markdown section of its own, nor carries a declared exemption.

        Exemptions are a `# doc-sections-ok:` pragma within a few lines above the call. They exist
        because one real site (migration) appends parts already split on their own `## ` headings,
        which no static check can see. Declared-and-greppable beats a hardcoded allowlist in this
        file, which would silently exempt whatever a future refactor moved."""
        out = []
        lines = src.splitlines()
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fname = getattr(node.func, "attr", None) or getattr(node.func, "id", "")
            if fname != "append_doc" or len(node.args) < 2:
                continue
            ln = getattr(node, "lineno", 0)
            window = "\n".join(lines[max(0, ln - 7):ln])
            if "doc-sections-ok" in window:
                continue
            arg = node.args[1]
            names = {getattr(n.func, "attr", None) or getattr(n.func, "id", "")
                     for n in ast.walk(arg) if isinstance(n, ast.Call)}
            if MINTER in names:
                continue
            lits = [n.value for n in ast.walk(arg)
                    if isinstance(n, ast.Constant) and isinstance(n.value, str)]
            if any(re.search(r"(?m)^#{1,6}\s", s) for s in lits):
                continue                      # opens its own section explicitly
            out.append(ln)
        return out

    for fn in sorted(os.listdir(HERE)):
        if not fn.endswith(".py") or fn.startswith("test_"):
            continue
        with open(os.path.join(HERE, fn), "r", encoding="utf-8") as fh:
            src = fh.read()
        for line in _append_violations(src):
            fails.append("%s:%d append to a shared doc that opens no section - route through %s()"
                         % (fn, line, MINTER))

    # SELF-TEST: the guard must DETECT the shape it bans, or deleting its body reads as green.
    BAD = ("def f(cfg):\n"
           "    store.append_doc(p, '\\n' + raw + '\\n' + stamp + '\\n', cfg)\n")
    if not _append_violations(BAD):
        fails.append("SELF-TEST: the guard did not flag a headingless shared-doc append")

    GOOD = ("def f(cfg):\n"
            "    store.append_doc(p, store.doc_entry_block(raw, sess, key), cfg)\n")
    if _append_violations(GOOD):
        fails.append("FALSE POSITIVE: a minted entry block was flagged")

    GOOD2 = ("def f(cfg):\n"
             "    store.append_doc(dst, '\\n### migrated from %s\\n\\n%s\\n' % (k, t), cfg)\n")
    if _append_violations(GOOD2):
        fails.append("FALSE POSITIVE: an append that opens its own section was flagged")

    if fails:
        print("FAIL - doc entry sections (%d)" % len(fails))
        for f in fails:
            print("  -", f)
        return 1
    print("OK - every appended entry is its own section; earlier ids stable under append")
    return 0


def test_doc_entry_sections():
    assert run() == 0, "doc entry section guard failed - see captured stdout"


if __name__ == "__main__":
    sys.exit(run())
