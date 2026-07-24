#!/usr/bin/env python3
"""Plain-ASCII gate for shipped docs. No AI-tell presentation glyphs (em/en dash, ellipsis,
curly quotes, bullet, arrows, box-drawing, emoji) in any .md that ships. Functional non-ASCII
is intentionally kept: the middle-dot U+00B7 (byline / migrated-stamp separator), multilingual
fixtures, and data-file emoji all pass - this scans DOCS only, for these tells only.

The denylist is by \\u escape (not literal glyphs) so the file that forbids these glyphs does
not itself contain them. Mirrors the Parsica Spirit verify_release plain-ascii-docs gate, so
canon is enforced mechanically in both repos instead of depending on a human noticing.
"""
import os
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_AI_TELLS = {
    "\u2014": "em dash", "\u2013": "en dash", "\u2015": "horizontal bar", "\u2012": "figure dash",
    "\u2026": "ellipsis", "\u2022": "bullet",
    "\u2018": "curly quote", "\u2019": "curly quote", "\u201c": "curly quote", "\u201d": "curly quote",
    "\u00a0": "no-break space",
}
_DOC_SKIP_PREFIX = ("UPSTREAM_",)   # vendored upstream docs keep their upstream text


def _tell(ch):
    o = ord(ch)
    if ch in _AI_TELLS:
        return _AI_TELLS[ch]
    if (0x2190 <= o <= 0x21FF) or (0x27F0 <= o <= 0x27FF) or (0x2900 <= o <= 0x297F):
        return "arrow"
    if 0x2500 <= o <= 0x257F:
        return "box-drawing"
    if 0x2300 <= o <= 0x23FF:
        return "misc-technical"
    if (0x2600 <= o <= 0x27BF) or (0x2B00 <= o <= 0x2BFF) or \
       (0x1F000 <= o <= 0x1FAFF) or (0x1F1E6 <= o <= 0x1F1FF):
        return "emoji"
    return None


def _git_tracked_md():
    """Tracked .md via git, or None if git is unavailable / this is not a repo."""
    try:
        r = subprocess.run(["git", "-c", "core.quotePath=false", "ls-files", "*.md"],
                           cwd=ROOT, capture_output=True, encoding="utf-8", errors="replace")
    except (FileNotFoundError, OSError):
        return None
    if r.returncode != 0:
        return None
    return [ln for ln in r.stdout.splitlines() if ln.endswith(".md")]


def _walk_md():
    out = []
    for dp, dns, fns in os.walk(ROOT):
        dns[:] = [d for d in dns if not d.startswith(".") and d != "node_modules"]
        for f in fns:
            if f.endswith(".md"):
                out.append(os.path.relpath(os.path.join(dp, f), ROOT))
    return out


def _shipping_docs():
    # Discover the tracked doc set so a doc is gated the moment it is added (before it lands
    # on public). Fall back to a filesystem walk when git is unavailable - a non-git tarball /
    # ZIP is still scanned, never vacuously passed. Dev-only docs are skipped by the .local. and
    # UPSTREAM_ naming conventions - matched by pattern, so no private filename is named here.
    rels = _git_tracked_md()
    if rels is None:
        rels = _walk_md()
    for rel in sorted(set(rels)):
        base = os.path.basename(rel)
        if ".local." in base or base.startswith(_DOC_SKIP_PREFIX):
            continue
        p = os.path.join(ROOT, rel)
        if os.path.isfile(p):
            yield p


def test_scanner_has_teeth():
    assert _tell("\u2014") == "em dash"          # em dash
    assert _tell("\u2022") == "bullet"           # bullet
    assert _tell("\u2b50") == "emoji"            # star (the 2B00-2BFF gap)
    assert _tell("\u2192") == "arrow"            # rightwards arrow
    assert _tell("a") is None                    # plain ASCII passes
    assert _tell("\u00b7") is None               # middle-dot is functional, kept


def test_shipped_docs_are_plain_ascii():
    docs = list(_shipping_docs())
    assert docs, "no shipping .md docs discovered (not a git checkout, or git unavailable) - a gate must not certify an empty set"
    hits = []
    for p in docs:
        for ln, line in enumerate(open(p, encoding="utf-8").read().splitlines(), 1):
            for ch in line:
                kind = _tell(ch)
                if kind:
                    hits.append("%s:%d %s (U+%04X)" % (os.path.relpath(p, ROOT).replace(os.sep, "/"),
                                                       ln, kind, ord(ch)))
                    break
    assert not hits, "AI-tell glyphs in shipped docs:\n  " + "\n  ".join(hits)
