#!/usr/bin/env python3
"""
Parsica Weave - retrieval helper.

Granular, provenance-stamped retrieval over the enriched chat mirror. Retrieval-first:
pull the relevant 2-3 chunks by tag/keyword/file/time instead of re-reading history.
Every hit carries its timestamp + session so you can jump back to the exact slice.

Ranking: a grep hit in the SEMANTIC overlay outranks a raw-text hit - `search_queries`
(doc2query) highest, then `topic`, then keywords/summary, then facts/categories, then
raw user/assistant text. search_queries rank highest because a doc2query line is the most
on-topic signal; a simple ordered weighting suffices at this scale.

Usage (run from any project dir; defaults to the current project):
    python query.py --grep "scrubber"          # keyword, overlay-weighted
    python query.py --file effects.py          # chunks that touched a file
    python query.py --cmd "git commit"         # chunks that ran a command
    python query.py --tool Edit                # chunks that used a tool
    python query.py --category security        # chunks in a semantic category
    python query.py --since 2                  # only the last N active days
    python query.py --grep auth --full --limit 5
    python query.py --grep auth --archive      # include rotated archive/ shards
    python query.py --grep auth --workspace    # union across every project in the workspace
    python query.py --project <key> ...        # query another project's store
    python query.py --list-projects

Flags combine (AND). Default output is compact; --full prints stored text + overlay.
"""
import sys
import os
import re
import json
import argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store
import scrub
import enrich
import calibrate


_RF = []   # refusal terms for the output sink; set in main() from config


def _s(text):
    """Defense-in-depth scrub at the retrieval sink (mirror is already scrubbed). Applies the
    CURRENT refusal list too, so a term added after capture is still fenced on output."""
    return scrub.scrub(text or "", scrub_pii=True, refusal=_RF)[0]


def _strlist(v):
    """Render a collection field as a list of strings whatever shape it arrived in: a list is
    coerced element-wise, a bare string is one item (never char-split by the downstream `,`.join),
    any other scalar becomes its own single item. Defense in depth behind store.normalize_import_row,
    which is what stops a `{"files": [7]}` from reaching the store in the first place."""
    if isinstance(v, list):
        return [str(x) for x in v]
    if isinstance(v, str):
        return [v]
    return [str(v)]


# archive rotation can mint numbered-collision names (<date>.<pid>.1.jsonl) that
# store.mirror_date rejects by design - a date read here must tolerate them.
_SHARD_DATE = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:\.\d+)*\.jsonl$")


def _shard_date(path):
    m = _SHARD_DATE.match(os.path.basename(str(path)))
    return m.group(1) if m else None


def _shard_files(md, archive=False):
    """A project's mirror shards, active first; archive=True appends the rotated archive/ shards.
    Listed through store.safe_mirror_shard_files (safe-iterdir'd, not glob'd) - RC5L-I: glob()
    follows a symlink/junction exactly like iterdir() does, so a linked shard was read as this
    project's own capture the same as an ordinary one; the --workspace project-level iterator just
    below already went through _safe_iterdir, but this per-project shard listing (used by every
    query path, not just --workspace) did not."""
    files = list(store.safe_mirror_shard_files(md))
    if archive:
        files += list(store.safe_mirror_shard_files(md / "archive"))
    return files


def _load_chunks(cwd=None, project=None, since=None, archive=False, workspace=False):
    mroot = store.STORE_ROOT / "mirror"
    if workspace:
        # union across EVERY project dir under mirror/ (deduped by chunk id below). _safe_iterdir
        # (RC5L-H), not a bare iterdir()+is_dir(): that FOLLOWED a junctioned mirror/ or a linked
        # child project dir and enumerated its external shards as if they belonged to the workspace -
        # the exact containment proof --project already has (validate_project_key) but --workspace
        # lacked.
        dirs = sorted(store.safe_mirror_projects(), key=lambda p: p.name)
    elif project:
        dirs = [mroot / project]
    else:
        dirs = [store.mirror_dir(cwd or os.getcwd())]
    chunks, seen = [], set()
    for md in dirs:
        files = _shard_files(md, archive)
        if since:
            # --since N = the last N ACTIVE DATES (per project), grouped by calendar date, not
            # file count - per-window shards (<date>.<pid>.jsonl) make files-per-date > 1.
            keep = set(sorted({d for d in (_shard_date(f) for f in files) if d},
                              reverse=True)[: int(since)])
            files = [f for f in files if _shard_date(f) in keep]
        found = []
        for f in files:
            for o in store.read_jsonl_rows(f):
                cid = o.get("id")
                if workspace and cid:
                    if cid in seen:   # the workspace union dedupes by chunk id
                        continue
                    seen.add(cid)
                found.append(o)
        # join the semantic overlay (separate append-only files) onto this project's chunks by id
        try:
            overlay = enrich.load_overlay(cwd, md.name if (workspace or project) else None,
                                          archive=archive)
        except Exception:
            overlay = {}
        for c in found:
            oc = overlay.get(c.get("id"))
            if oc:
                c["enrich"] = oc
        chunks += found
    return chunks


# grep field weights - overlay hits outrank raw text (the ranking rule)
_WEIGHTS = [
    ("search_queries", 5), ("topic", 4), ("keywords", 3),
    ("summary", 3), ("facts", 2), ("categories", 2),
]


def _conviction_value(d):
    """A decision's conviction value, tolerant of a malformed overlay: 0.5 when the decision or
    its `conviction` isn't a dict (a `conviction` of "bad"/None/list must not crash retrieval)."""
    c = d.get("conviction") if isinstance(d, dict) else None
    return c.get("value", 0.5) if isinstance(c, dict) else 0.5


def recent_chunks(chunks, hours=12.0, limit=5, exclude_sess=None):
    """Cross-session recency retrieval - the workspace analogue of the engine's
    search_by_recency: the most recent chunks inside the window, newest first,
    optionally excluding the caller's own session (high recency + a different
    session id = "something that just happened in another session"). Transcript
    timestamps are UTC (Z-suffixed); a naive ts is treated as UTC too."""
    from datetime import datetime, timezone, timedelta
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    # the stored `sess` is an opaque token; hash the caller-supplied session id the same way so the
    # exclusion compares like-for-like (a user's --sess a91c6844 matches the token stamped on the chunk).
    exclude_tok = store.sess_token(exclude_sess) if exclude_sess else None
    out = []
    for c in chunks:
        ts = str(c.get("ts") or "")
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if dt < cutoff:
            continue
        if exclude_tok and str(c.get("sess") or "") == exclude_tok:
            continue
        out.append(c)
    out.sort(key=lambda c: str(c.get("ts") or ""), reverse=True)
    return out[:limit]


def _score(chunk, args, cal_stats=None):
    """Return (matches, score). Filters are AND; grep score is overlay-weighted, and a hit
    in a decision is weighted by its EARNED conviction (conviction x calibration)."""
    e = chunk.get("enrich")
    if not isinstance(e, dict):                  # malformed/non-dict enrich -> ignore
        e = {}
    if args.file and not any(args.file.lower() in f.lower() for f in chunk.get("files", [])):
        return False, 0.0
    if args.cmd and not any(args.cmd.lower() in c.lower() for c in chunk.get("cmds", [])):
        return False, 0.0
    if args.tool and args.tool not in chunk.get("tools", []):
        return False, 0.0
    if args.category and args.category.lower() not in [c.lower() for c in e.get("categories", [])]:
        return False, 0.0
    if not args.grep:
        return True, 1.0                         # filter-only match
    g = args.grep.lower()
    score = 0.0
    for field, w in _WEIGHTS:
        v = e.get(field)
        if isinstance(v, str):
            if g in v.lower():
                score += w
        elif isinstance(v, list):
            if any(g in str(x).lower() for x in v):
                score += w
    for d in e.get("decisions", []):             # a decision hit, scaled by earned conviction
        if isinstance(d, dict) and g in str(d.get("text", "")).lower():
            ew = calibrate.effective_weight(_conviction_value(d),
                                            d.get("did"), stats=cal_stats)
            score += 2 + 4 * ew
    # Coerce every field to text/str-list before joining: a poisoned chunk whose `user` is a list
    # (or whose `files` is a bare string) otherwise raises "expected str instance, list found" and
    # crashes the grep - the very recovery path SessionStart tells the operator to run. Same shape
    # as the SessionStart render guard, so one bad row never disables both surfaces at once.
    def _s(v):
        return v if isinstance(v, str) else ""

    def _sl(v):
        if isinstance(v, str):
            return [v]
        return [x for x in v if isinstance(x, str)] if isinstance(v, list) else []

    raw = " ".join([_s(chunk.get("user")), _s(chunk.get("asst")),
                    " ".join(_sl(chunk.get("files"))), " ".join(_sl(chunk.get("cmds")))]).lower()
    if g in raw:
        score += 1
    return (score > 0), score


_CHANNELS = None   # lazy workspace-registry cache: chan id -> display name


def _chan_label(cid):
    global _CHANNELS
    if _CHANNELS is None:
        try:
            _CHANNELS = {k: str(m.get("name") or "") for k, m in
                         (store.load_channels().get("channels") or {}).items()
                         if isinstance(m, dict)}
        except Exception:
            _CHANNELS = {}
    return _CHANNELS.get(cid) or cid


def _fmt(chunk, full, cal_stats=None):
    """Render one chunk, and NEVER raise doing it. store.normalize_import_row keeps a malformed shape
    out of the active store, and _strlist coerces the fields that used to char-split; this wrapper is
    the last backstop, so a row corrupted by a later hand-edit drops to a marked placeholder and every
    sibling row still renders, instead of one bad chunk aborting the whole query with a TypeError. The
    placeholder is a CONSTANT - it interpolates no field of the malformed row, per the output-sink
    discipline (the raw row could carry the very content the fence exists to keep off a sink)."""
    try:
        return _fmt_inner(chunk, full, cal_stats)
    except Exception:
        return "[unreadable chunk - malformed fields, skipped]"


def _fmt_inner(chunk, full, cal_stats=None):
    # display is LOCAL (store.local_stamp); the stored ts stays UTC. No zone label per
    # row - the section header carries store.local_zone_note() once.
    who = [store.local_stamp(chunk.get("ts")), str(chunk.get("sess", ""))]
    if chunk.get("chan"):
        who.append(_s(_chan_label(chunk["chan"])))   # channel name (workspace registry) when tagged
    head = "[" + " · ".join(who) + "]"
    e = chunk.get("enrich")
    if not isinstance(e, dict):
        e = {}
    tags = []
    if e.get("topic"):
        tags.append("topic=" + ",".join(str(x) for x in e["topic"][:6]))
    if e.get("categories"):
        tags.append("cat=" + ",".join(str(x) for x in e["categories"]))
    if chunk.get("files"):
        tags.append("files=" + ",".join(_strlist(chunk["files"])[:4]))
    if chunk.get("cmds"):
        tags.append("cmds=" + ",".join(_strlist(chunk["cmds"])[:4]))
    if chunk.get("tools"):
        tags.append("tools=" + ",".join(_strlist(chunk["tools"])))
    out = [head + ("  " + _s(" | ".join(tags)) if tags else "")]
    if full:
        if e.get("summary"):
            out.append("  sum: " + _s(e["summary"]))
        if e.get("search_queries"):
            out.append("  q: " + " | ".join(_s(str(q)) for q in e["search_queries"]))
        for d in (e.get("decisions") or [])[:4]:
            if not isinstance(d, dict):
                continue
            tr = calibrate.trajectory(_conviction_value(d),
                                      d.get("did"), stats=cal_stats)
            out.append("  decision: " + _s(str(d.get("text", ""))))
            out.append(f"    conviction {tr['conviction_at_creation']} | calibration "
                       f"{tr['calibration']} ({tr['confirmed']:.0f}+/{tr['contradicted']:.0f}-) "
                       f"| effective {tr['effective_weight']}")
        if chunk.get("user"):
            out.append("  U: " + _s(chunk["user"]).replace("\n", "\n     "))
        if chunk.get("asst"):
            out.append("  A: " + _s(chunk["asst"]).replace("\n", "\n     "))
    else:
        gist = _s(e.get("summary") or chunk.get("user") or chunk.get("asst") or "")
        gist = gist.replace("\n", " ")[:120]
        if gist:
            out.append("  " + gist)
    return "\n".join(out)


# --------------------------------------------------------------------------
# display-time collapse - PRESENTATION ONLY. The same content shown twice in one result
# list (a durable record and a warm chunk quoting it, or the same section imported from
# two native files) folds into one copy. Collapsing decides which copy PRINTS; it never
# changes a score, a rank, or a stored record.
# --------------------------------------------------------------------------
_WORD_RE = re.compile(r"\w+")
_DUP_COEFF = 0.9   # near-duplicate floor: overlap coefficient (|A&B| / min(|A|,|B|))

# Polarity words: if two otherwise-overlapping texts DIFFER by one of these, they may be
# opposites ("deploy on Friday" vs "never deploy on Friday"), so a near-dup collapse would
# silently hide a contradiction. A single negation must never dedupe two rules. Word tokenizing
# would split "don't" into {don, t} and hide the negation, so _tok_seq expands the n't family to
# "... not" BEFORE tokenizing (see below); these are the remaining non-contraction negators.
_NEGATORS = frozenset({
    "not", "never", "no", "none", "cannot", "neither", "nor", "without", "avoid",
    "unless", "except", "disable", "disabled", "off", "stop", "deny", "exclude",
    "excluded", "refuse", "prevent", "block", "blocked",
})

#: negation contractions (don't, won't, can't, isn't, shouldn't, ain't ...). The stem is dropped to
#: a harmless stub ("wo", "ca") and the recovered "not" is what the polarity guard reads. Straight
#: (U+0027) and curly (U+2019) apostrophes both count.
_NT_CONTRACTION = re.compile(r"([A-Za-z]+)n['\u2019]t\b", re.IGNORECASE)


def _toks(text):
    return {w.casefold() for w in _WORD_RE.findall(text or "")}


def _tok_seq(text):
    """Ordered casefolded word tokens (a LIST, not a set) - carries token ORDER so a collapse can
    demand a CONTIGUOUS quote, not merely a shared vocabulary. Negation contractions are expanded to
    "... not" FIRST so a "don't"/"can't" surfaces the 'not' the polarity guard reads (word tokenizing
    would otherwise split it to {don, t} and let the negation collapse behind its positive twin)."""
    return [w.casefold() for w in _WORD_RE.findall(_NT_CONTRACTION.sub(r"\1 not", text or ""))]


def _is_contiguous_sublist(short, long):
    """True iff `short` appears as a CONTIGUOUS run inside `long` - a real quote/truncation, not a
    scattered subset or a reordering. Empty `short` never matches (nothing to fold onto)."""
    n, m = len(short), len(long)
    if n == 0 or n > m:
        return False
    for i in range(m - n + 1):
        if long[i:i + n] == short:
            return True
    return False


def _display_key(text):
    """Exact normalized form for duplicate detection: casefolded, whitespace-collapsed. Two texts
    share a key only when they are the SAME text - never a near-duplicate, so a negation or a
    changed number always produces a different key."""
    return " ".join((text or "").casefold().split())


def _overlap_coeff(a, b):
    """Overlap coefficient of two token sets - 1.0 when the smaller is contained in the
    larger, which is exactly the quoted-section case (a truncated warm chunk quoting a
    native section)."""
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def _safe_to_collapse(a_seq, b_seq):
    """A near-dup is safe to fold ONLY when the SHORTER token SEQUENCE appears CONTIGUOUSLY inside the
    longer - a real quote/truncation (one-directional containment). That is the case this path exists
    for: a warm chunk that QUOTES a durable section verbatim.

    Token-SET containment alone is not safe: it clears on a REORDERING ("deploy auth before db" vs
    "deploy db before auth" - identical sets) or a SCATTERED subset, either of which can change meaning
    while still passing the overlap floor. Requiring a contiguous run makes containment a claim about
    the actual text, not just its vocabulary. (W5: an earlier cut used set containment plus a
    hand-listed negator/digit check in the symmetric difference; a reordering slipped through it.)

    The polarity/number guards stay as a SECOND net for the genuinely-contained case (e.g. a durable
    "deploy X" contiguously inside a warm "do NOT deploy X, per policy" - contiguous, but the extra
    'not' inverts it, so reject)."""
    short, long = (a_seq, b_seq) if len(a_seq) <= len(b_seq) else (b_seq, a_seq)
    if not _is_contiguous_sublist(short, long):
        return False                       # not a contiguous quote = not a safe truncation
    # polarity: compare the COUNT of negators per side, not just the set. A set symmetric difference
    # misses a second, meaning-inverting negator when the token is ALREADY present on both sides
    # ("not deploy" vs "we can't not deploy" -> both carry 'not', so the set hides the extra one that
    # flips the meaning) - the exact hole an n't expansion can open. Unequal negator counts => reject.
    # This DELIBERATELY also rejects a benign extra same-polarity clause (a quote plus ", and do not
    # merge to main"): a bare count cannot tell that from an inverting double-negation without
    # negation-SCOPE parsing, and the two are mechanically identical (N vs N+1, contiguous). Over-
    # rejecting is presentation-only and fail-safe (both copies still print; nothing is hidden) and
    # matches the guard's stated bias - never let one negation dedupe two rules. Do NOT relax this to
    # the old set check to recover the fold: that reopens the contradiction-hiding hole.
    if sum(t in _NEGATORS for t in a_seq) != sum(t in _NEGATORS for t in b_seq):
        return False
    diff = set(a_seq) ^ set(b_seq)         # contiguity implies one-sided: diff is the extra context
    if any(any(c.isdigit() for c in t) for t in diff):
        return False
    return True


def _collapse_durables(spirit_hits, warm_chunks):
    """Fold federated durable hits into the display set. A durable hit collapses when its content
    is already on screen in one of three ways: the promoted twin of a warm chunk (id-level,
    `enriched:` prefix); an EXACT duplicate of another durable hit (same section imported from two
    files - never a near-dup, so one negation can't hide a contradiction); or a section (nearly)
    contained in a warm chunk that quotes it, provided they do not disagree on polarity or a number.
    The warm copy wins a contested slot and wears the [both] tag. Returns (kept, both_ids).
    Presentation only - see the section note."""
    both_ids = set()
    kept = []
    kept_keys = set()                      # exact normalized keys of kept durables
    # each warm chunk: (id, ordered token sequence, token set) - the seq for contiguous containment,
    # the set for the cheap overlap-coefficient floor.
    warm = []
    for c in warm_chunks:
        wseq = _tok_seq("%s %s" % (c.get("user") or "", c.get("asst") or ""))
        warm.append((c.get("id"), wseq, set(wseq)))
    for h in spirit_hits:
        srid = h.get("source_record_id") or ""
        cid = srid[len("enriched:"):] if srid.startswith("enriched:") else None
        if cid and any(c.get("id") == cid for c in warm_chunks):
            both_ids.add(cid)              # id-level twin: warm copy wins the slot and wears the tag
            continue
        body = h.get("body")
        key = _display_key(body)
        if key and key in kept_keys:
            continue                       # EXACT duplicate already prints (verbatim, not near-dup)
        hseq = _tok_seq(body)
        hset = set(hseq)
        wid = next((wid_ for wid_, wseq, wset in warm
                    if _overlap_coeff(hset, wset) >= _DUP_COEFF
                    and _safe_to_collapse(hseq, wseq)), None)
        if wid is not None:
            both_ids.add(wid)              # polarity/number-safe near-dup of a warm chunk: warm wins
            continue
        kept.append(h)
        kept_keys.add(key)
    return kept, both_ids


def _print_durable(h, full):
    """One durable (Spirit) hit: "[durable] <kind> · <id> · <sess> · <created>", empties
    omitted - an older parsica_spirit whose hits carry no session/created keys renders
    exactly the two-part line it always did. `created` is the pool clock (store time,
    the TWO-CLOCKS note), rendered LOCAL like every display ts (verbatim when unparseable).
    A native-memory record identifies itself: kind renders as claude-native/<scope> with
    its heading trail, so recall from the agent's own memory files is always attributed
    (that content may also be loaded in the window by the platform itself)."""
    kind = h.get("kind") or "record"
    head_extra = []
    if h.get("source") == "claude-code-memory":
        kind = "claude-native/" + (h.get("source_scope") or "unknown")
        if h.get("heading_path"):
            head_extra.append(str(h.get("heading_path")))
    parts = [kind, h.get("source_record_id") or h.get("id", "")] + head_extra
    parts += [p for p in (h.get("session") or "", store.local_stamp(h.get("created") or "")) if p]
    print(_s("[durable] " + " · ".join(str(p) for p in parts)))
    body = str(h.get("body") or "")
    if full:
        # "  | " quote-prefix: real section headers never wear it, so a stored
        # body containing a literal "[durable] ..." line cannot forge one
        for ln in body.splitlines():
            print("  | " + _s(ln))
    else:
        print("  " + _s(body.replace("\n", " ")[:160]))


def _recency_dt(ts):
    """Parse a chunk ts / `created` label for the cross-layer recency merge. Both spell
    UTC ISO (Z-suffixed); a naive value is treated as UTC, an unparseable one as None."""
    from datetime import datetime, timezone
    try:
        dt = datetime.fromisoformat(str(ts or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")   # Windows defaults cp1252; chunks are UTF-8
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="Retrieve from the Parsica Weave chat mirror.")
    ap.add_argument("--grep")
    ap.add_argument("--file")
    ap.add_argument("--cmd")
    ap.add_argument("--tool")
    ap.add_argument("--category", help="semantic category (e.g. security, testing)")
    ap.add_argument("--since", type=int, help="only the last N active-day files")
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--full", action="store_true")
    ap.add_argument("--archive", action="store_true",
                    help="include rotated archive/ shards (mirror + overlay)")
    ap.add_argument("--workspace", action="store_true",
                    help="union across every project in the workspace (overrides --project)")
    ap.add_argument("--project", help="project key (defaults to current cwd)")
    ap.add_argument("--channel", help="filter to one channel by NAME (registry lookup; "
                                      "no match = no results, stated loudly)")
    ap.add_argument("--list-projects", action="store_true")
    ap.add_argument("--recent", type=int, metavar="N",
                    help="recency retrieval: the N most recent chunks in the window, "
                         "newest first (workspace-wide unless --project is given)")
    ap.add_argument("--hours", type=float, default=12.0,
                    help="window for --recent, in hours (default 12)")
    ap.add_argument("--sess", help="this session's id (8 chars); --recent excludes it "
                                   "unless --all-sessions")
    ap.add_argument("--all-sessions", action="store_true",
                    help="include the caller's own session in --recent")
    args = ap.parse_args()

    # Load the WORKSPACE-WIDE refusal union BEFORE any output path, so every sink - the two early
    # error branches below AND --list-projects (whose keys embed project DIRECTORY names) - is fenced
    # against a term configured in ANY project, not just the current one. (W4: those early error
    # branches echoed the caller's raw --project / --channel value straight to the terminal, bypassing
    # the output sink; _RF must be populated before they can fence.)
    global _RF
    _RF = store.workspace_refusal_terms()

    # --project is joined onto STORE_ROOT at several read sites; a traversal or absolute
    # value (../../secret, C:/Windows/...) would escape the store and read arbitrary files.
    # Validate the key shape + resolved containment BEFORE any path use, and refuse loudly.
    if args.project is not None and not store.validate_project_key(args.project):
        print(_s(f"- invalid --project {args.project!r}: not a store project key; refusing -"))
        return

    if args.list_projects:
        mroot = store.STORE_ROOT / "mirror"
        if not mroot.exists():
            print("(no parsica-weave store yet)")
            return
        # RC5L-J: raw iterdir()/glob() FOLLOWED a junctioned mirror/ or a linked project/archive
        # dir and reported its external shards as this workspace's own - listed through the safe
        # seams instead, the same discipline _load_chunks's --workspace path already had.
        for p in sorted(store.safe_mirror_projects(), key=lambda p: p.name):
            act = {d for d in (_shard_date(f) for f in store.safe_mirror_shard_files(p)) if d}
            arc = {d for d in (_shard_date(f) for f in
                               store.safe_mirror_shard_files(p / "archive")) if d}
            name = _s(p.name)
            line = f"  {name}  ({len(act)} active / {len(arc)} archive day(s))  " \
                   f"working/{name} - Working Set.md"
            print(line)
        return

    # --recent is a cross-session awareness surface: workspace-wide unless the caller
    # scoped it to one project deliberately. Computed HERE so the refusal union below
    # fences every project a recent-hit could come from.
    ws = args.workspace or (args.recent is not None and not args.project)

    # _RF is already the workspace-wide union from store.workspace_refusal_terms() at the top of
    # main(), which unions the global config AND every project's config (project enumeration and
    # each config read are containment-checked there). RC5L-J: this used to ALSO re-read the
    # target project's config.json directly, by pathname, via raw iterdir()/read_text() - a
    # symlinked or hardlinked config.json was therefore read a second time here, unchecked, and an
    # attacker-supplied "refusal" list could alter otherwise-valid query output. workspace_refusal_
    # terms() already carries every project's terms, so the duplicate read added nothing real and
    # is simply removed rather than re-hardened in place.

    chunks = _load_chunks(project=args.project, since=args.since,
                          archive=args.archive, workspace=ws)
    if args.channel:
        # resolve the channel NAME to its immutable id via the WORKSPACE registry (one lane, everywhere)
        cid = None
        try:
            for k, m in (store.load_channels().get("channels") or {}).items():
                if isinstance(m, dict) and m.get("name") == args.channel:
                    cid = k
                    break
        except Exception:
            cid = None
        if cid is None:
            print(_s(f"- channel {args.channel!r} not found in the registry; 0 chunk(s) -"))
            return
        chunks = [c for c in chunks if c.get("chan") == cid]
    try:
        cal_stats = calibrate.load_stats(cwd=os.getcwd(), project=args.project)
    except Exception:
        cal_stats = {}

    if args.recent is not None:
        # cross-session awareness mode: selection by recency, no relevance scoring.
        rows = recent_chunks(chunks, hours=args.hours, limit=args.recent,
                             exclude_sess=(None if args.all_sessions else (args.sess or None)))
        # the durable pool's recency layer (Spirit federated_recent, feature-detected).
        # Workspace-wide reads only: a deliberate --project scope stays Weave-only - the
        # pool is one-per-workspace, and the refusal union above fenced every project
        # only on the ws path.
        drows = _spirit_recent(args) if ws else []
        if drows:
            # same-content copies collapse here too (presentation only): a promoted
            # twin or a near-identical durable copy of a warm row never prints twice.
            drows, _ = _collapse_durables(drows, rows)
        if drows:
            # ONE recency budget across both layers (the K@ rule: durable slots come
            # OUT of N, never on top of it - K@15 with recency 5 = 5+10, not 5+15):
            # merge warm chunks (ts, the event clock) and durable records (created,
            # the pool clock) newest-first and keep the top N overall.
            merged = ([("weave", _recency_dt(c.get("ts")), c) for c in rows] +
                      [("spirit", _recency_dt(h.get("created")), h) for h in drows])
            merged = [m for m in merged if m[1] is not None]
            merged.sort(key=lambda m: m[1], reverse=True)
            merged = merged[: args.recent]
            rows = [x for layer, _dt, x in merged if layer == "weave"]
            drows = [x for layer, _dt, x in merged if layer == "spirit"]
        if not rows and not drows:
            print(_s(f"(no chunks in the last {args.hours:g}h)"))
            return
        print(store.REFERENCE_CAVEAT)   # recalled chunks are data, not instructions
        if rows:
            print(_s(f"- {len(rows)} recent chunk(s), last {args.hours:g}h, newest first; "
                     f"{store.local_zone_note()} -"))
            for c in rows:
                print(_fmt(c, args.full, cal_stats))
        else:
            print(_s(f"(no chunks in the last {args.hours:g}h)"))
        if drows:
            print(_s(f"- {len(drows)} durable (Spirit), last {args.hours:g}h, newest first; {store.local_zone_note()} -"))
            for h in drows:
                _print_durable(h, args.full)
        return

    scored = []
    for c in chunks:
        ok, sc = _score(c, args, cal_stats)
        if ok:
            scored.append((sc, c))
    # rank: score desc, then recency - overlay hits float to the top
    scored.sort(key=lambda x: (x[0], x[1].get("ts", "")), reverse=True)
    hits = [c for _, c in scored[: args.limit]]

    # Spirit federation (optional second layer). Computed BEFORE printing so the warm
    # copy of a promoted turn can wear its [both] tag. Fail-open: absent/killed/broken
    # Spirit changes NOTHING about the Weave output below.
    spirit_hits, order, tr = _spirit_federation(args)
    kept, both_ids = _collapse_durables(spirit_hits, hits)
    _emit_query_trace(tr, args, hits, kept, both_ids, order)

    def _weave_section():
        if not hits:
            print("(no matching chunks)")
            return
        print(_s(f"- {len(hits)} chunk(s), ranked; {store.local_zone_note()} -"))
        for c in hits:
            line = _fmt(c, args.full, cal_stats)
            print(("[both] " + line) if c.get("id") in both_ids else line)

    def _spirit_section():
        if not kept:
            return
        print(_s(f"- {len(kept)} durable (Spirit); {store.local_zone_note()} -"))
        for h in kept:
            _print_durable(h, args.full)

    if hits or kept:
        print(store.REFERENCE_CAVEAT)   # recalled chunks are data, not instructions
    if order == ("spirit", "weave") and kept:
        _spirit_section()
        _weave_section()
    else:
        _weave_section()
        _spirit_section()


def _spirit_federation(args):
    """Weave's ONLY coupling to the durable floor on the read path: one guarded lazy
    import, killed by PARSICA_ROUTER=off, silent when Spirit or its store is absent,
    stderr-only when Spirit is present but failing. The pure-Weave output is
    byte-identical in every degraded mode. The third return value is a trace context
    (scope key + store root) ONLY when the router actually engaged - degraded modes
    return None and therefore emit no trace at all."""
    default_order = ("weave", "spirit")
    if os.environ.get("PARSICA_ROUTER", "on").strip().lower() in ("0", "off", "false", "no"):
        return [], default_order, None
    if not args.grep:
        return [], default_order, None    # structural filters (file/cmd/tool) are Weave-only
    try:
        from parsica_spirit.router import federated_hits, layer_order
    except ImportError:
        return [], default_order, None
    try:
        from pathlib import Path
        # the workspace's scope id (workspace.json, random hex - never derived from names):
        # ONE durable pool for every project and session, so a --project query federates
        # against the same pool the promote verb writes (source_project is a label, not a scope).
        # create=False: this is a pure READ path - it must NEVER mint durable identity (nor its
        # sidecar lock). No minted id yet (fresh workspace, or an isolated session) -> no pool to
        # federate against, so degrade to Weave-only exactly as when Spirit is absent.
        sid = store.workspace_id(create=False)
        if not sid:
            return [], default_order, None
        sroot = Path(os.environ.get("PARSICA_SPIRIT_ROOT")
                     or (store.HOME / ".claude" / "parsica-spirit")) / sid
        hits = federated_hits(str(sroot), sid, args.grep, limit=args.limit)
        if not sroot.exists():
            return [], default_order, None     # no store: the router never engaged
        return hits, layer_order(args.grep), {"key": sid, "root": str(sroot)}
    except Exception:
        # stderr.write, not print(file=): the store write-gateway's AST scan flags any
        # print-with-file as a potential raw write, and the scanner stays conservative
        sys.stderr.write("(Spirit federation unavailable; Weave-only results)\n")
        return [], default_order, None


def _spirit_recent(args):
    """The durable pool's slice of --recent: same guarded boundary as _spirit_federation
    (killed by PARSICA_ROUTER=off, silent when Spirit or its store is absent, stderr-only
    when present but failing - the pure-Weave output is byte-identical in every degraded
    mode). federated_recent itself is feature-detected: an older parsica_spirit without
    it degrades to the warm-only surface silently. Exclusion passes the same sess8 the
    promote verb writes into source_session; unattributed records always retrieve."""
    if os.environ.get("PARSICA_ROUTER", "on").strip().lower() in ("0", "off", "false", "no"):
        return []
    try:
        from parsica_spirit.router import federated_recent
    except ImportError:
        return []
    try:
        from pathlib import Path
        # create=False: a pure READ path, never mint durable identity (see _spirit_federation).
        # No minted id (fresh workspace / isolated session) -> nothing to federate against.
        sid = store.workspace_id(create=False)
        if not sid:
            return []
        sroot = Path(os.environ.get("PARSICA_SPIRIT_ROOT")
                     or (store.HOME / ".claude" / "parsica-spirit")) / sid
        return federated_recent(str(sroot), sid, hours=args.hours, limit=args.recent,
                                exclude_session=(None if args.all_sessions
                                                 else (args.sess or None)))
    except Exception:
        sys.stderr.write("(Spirit federation unavailable; Weave-only results)\n")
        return []


def _emit_query_trace(tr, args, weave_hits, kept, both_ids, order):
    """Passive sensor emission (best-effort, guarded twice: here and inside emit_trace).
    Content-free rows; the grep text is logged as a HASH only."""
    if not tr:
        return
    if store.is_isolated():
        return                 # tracing appends to Spirit's trace dir: a durable side-effect an
                               # isolated (zero-footprint) session must never leave behind
    try:
        from parsica_spirit.trace import emit_trace, exposure, query_hash
        rows = []
        for i, c in enumerate(weave_hits):
            cid = c.get("id") or ""
            rows.append(exposure(cid, "recent", i, source_kind="mirror_chunk",
                                 reason="both_warm" if cid in both_ids else "hit"))
        for i, h in enumerate(kept):
            rows.append(exposure(h.get("id") or "", "durable", i,
                                 source_kind=h.get("kind"),
                                 content_hash=h.get("content_hash") or None))
        deduped = len(both_ids)
        emit_trace(tr["root"], scope_id=tr["key"], route_mode="federated",
                   qhash=query_hash(args.grep),
                   weave_candidates=len(weave_hits),
                   spirit_candidates=len(kept) + deduped,
                   returned=len(weave_hits) + len(kept), deduped=deduped,
                   layer_order=list(order), exposures=rows)
    except Exception:
        pass


if __name__ == "__main__":
    main()
