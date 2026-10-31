#!/usr/bin/env python3
"""
Parsica Weave - semantic enrichment persistence + the scrub-on-output guard.

The chat mirror captures chunks DETERMINISTICALLY (Stop hook). SEMANTIC enrichment is
produced INLINE by the agent at checkpoints and persisted *here*. This module is the
single writer of a chunk's `enrich` overlay - and a NEW credential ingress surface:

  The inline enricher sees RAW conversation context, so its output (summary, keywords,
  search_queries, facts) can echo a secret even though the chunk text was already
  scrubbed at capture. So every enrichment string is scrubbed AGAIN here, before
  storage, with the same hardened scrubber. `build_enrich()` is the chokepoint.

The overlay is VERSIONED (`enrich.spec`) and replaced in place - (re-)enrichment is
read-modify-write by chunk id; it never touches the stable capture fields.

CLI:
  python enrich.py --list-pending [--limit N]   # chunks needing (re-)enrichment, as JSON
  python enrich.py --apply-stdin                # apply a {id: {...}} map from STDIN (preferred - no temp file on disk)
  python enrich.py --apply '<json>'             # same, inline (small batches)
"""
import sys
import os
import re
import json
import math
import uuid
import hashlib
import datetime
import argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store
import scrub

ENRICH_SPEC = store.ENRICH_SPEC

# The five enrichment fields the inline agent produces.
# `decisions` sits outside this tuple on purpose: it carries structured conviction and is
# normalized separately via _norm_decisions, not the generic string/list coercion above.
_AGENT_FIELDS = ("topic", "summary", "keywords", "search_queries", "facts")
_LIST_FIELDS = ("topic", "keywords", "search_queries", "facts")


def _spec_version(spec):
    m = re.search(r"/v(\d+)\b", spec or "")
    return int(m.group(1)) if m else 0


_CURRENT_VER = _spec_version(ENRICH_SPEC)

# Deterministic starter category rules - a simple keyword->category mapping (grow as needed).
_CATEGORY_RULES = {
    "security":    ["secur", "auth", "credential", "token", "secret", "encrypt", "redact",
                    "scrub", "permission", "vulnerab", "exploit", "cve", "identity", "signing"],
    "testing":     ["test", "pytest", "assert", "regression", "fixture", "coverage", "adversarial review"],
    "infra":       ["deploy", "docker", "kubernet", "k8s", "terraform", "ansible", "ci/cd",
                    "pipeline", "provision"],
    "cloud":       ["aws", "azure", "gcp", "s3", "lambda", "ec2", "bedrock"],
    "data":        ["database", "postgres", "sqlite", "redis", "mongo", "schema", "migration",
                    "index", "query"],
    "memory":      ["memory", "enrich", "retrieval", "chunk", "embedding", "recall", "provenance"],
    "build":       ["build", "compile", "bundle", "package", "npm", "cargo", "make", "webpack"],
    "git":         ["git ", "commit", "branch", "merge", "rebase", "pull request"],
    "frontend":    ["react", "css", "html", "component", "frontend", "jsx", "tsx"],
    "api":         ["api", "endpoint", "rest", "graphql", "webhook", "request", "response"],
    "performance": ["performance", "latency", "throughput", "optimiz", "cache", "profil"],
    "refactor":    ["refactor", "cleanup", "rename", "restructure", "dead code", "tech debt"],
    "docs":        ["readme", "documentation", "docstring"],
}


def categorize(text):
    t = (text or "").lower()
    return sorted(c for c, kws in _CATEGORY_RULES.items() if any(k in t for k in kws))


def _now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# A long opaque-ish token (>=12 non-whitespace chars). The SECRET NOUN nearby - not the
# token's entropy - is what triggers redaction, so the floor can be low without
# false-positives on bare ids. We match the WHOLE whitespace-delimited token (punctuation
# included), so a punctuation-bearing password next to a noun ("a8F)kQ2zR7wL1pM4") is
# redacted whole instead of leaving its prefix behind once the alnum suffix is masked.
_LONG_TOKEN = re.compile(r"\S{12,}")
_MARKER_RE = re.compile(r"\[REDACTED(?::[^\]]*)?\]")   # in-place redaction marker (colon part optional)
_SECRET_NOUN = re.compile(
    r"(?i)\b(?:password|passwd|passphrase|secret|secrets|token|tokens|api[\s_-]?key|apikey|"
    r"credential|credentials|bearer|access[\s_-]?key|private[\s_-]?key|secret[\s_-]?key|"
    r"client[\s_-]?secret|auth[\s_-]?token|access[\s_-]?token|refresh[\s_-]?token)\b")
# Positive "this is code/a hash/a path, not a secret" evidence - used ONLY to spare a token
# that sits next to a secret noun. Near a secret noun we redact by DEFAULT (the inverted
# rule); a token is kept ONLY on evidence of its OWN shape. We deliberately do NOT keep on an
# incidental code WORD in the surrounding window: a real rotated credential routinely sits
# next to 'commit'/'version'/'config', and one stray nearby word must never spare a secret.
_SAFE_SNAKE_ID = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)+$")
_HASH_PREFIX = ("sha256:", "sha512-", "sha1:", "md5:")
_GIT_SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$", re.IGNORECASE)   # SHA-1 / SHA-256 digest
_WIN_PATH = re.compile(r"^[A-Za-z]:[\\/]")
# sentence punctuation/quotes that merely WRAP a token in prose - stripped to the token's
# CORE before the keep-checks, so a trailing '.' or ')' can't defeat an exception (we still
# redact the whole matched token, just judge it by its core).
_CORE_STRIP = "\"'`()[]{}<>.,;:!?"


def _safe_under_secret_noun(tok, text, s, e):
    """KEEP a token next to a secret noun ONLY on positive evidence of its OWN shape: a hash
    prefix (any case), a bare git SHA / sha256 digest, a snake_case identifier, or a file
    path (priority #2 - SHAs and paths MUST survive). No surrounding-word heuristic."""
    core = tok.strip(_CORE_STRIP)
    if not core:
        return True
    if core.lower().startswith(_HASH_PREFIX):      # sha256:.. / SHA512-.. (case-insensitive)
        return True
    if _GIT_SHA.match(core):                        # 40/64-hex git SHA or sha256 digest
        return True
    if _SAFE_SNAKE_ID.match(core):                  # snake_case identifier / tag
        return True
    # file path - SEGMENT-VALIDATED (every segment short + path-shaped), NOT merely a leading
    # '/' or '~'. A long opaque secret wrapped in slashes (/LIVExy9kq2zr7wl1pm/) is NOT a path.
    pcore = core[1:] if core.startswith("~") else core
    if _WIN_PATH.match(core) or scrub._looks_like_path(pcore):
        return True
    return False


def _redact_opaque(text, counter):
    """Catch a secret the inline enricher echoed as PROSE (no key=value framing, no vendor
    prefix). SIGNAL = a secret NOUN (password/token/secret/api-key/credential/...) within
    ~40 chars of a long opaque token. Near a noun we redact BY DEFAULT and keep only on
    positive evidence the token is code/a hash - so dotted/hyphenated/hex secrets next to a
    noun ARE caught, while git SHAs ('commit <sha>'), `sha512-` hashes, and lowercase
    snake_case tags survive. No noun nearby -> kept. Runs ONLY on enrichment output, never
    capture text - a false positive costs a redacted tag, not gutted transcript."""
    if not text:
        return text
    nouns = [(m.start(), m.end()) for m in _SECRET_NOUN.finditer(text)]
    if not nouns:
        return text

    def _near(s, e):
        for ns, ne in nouns:
            if (ne <= s and s - ne <= 40) or (ns >= e and ns - e <= 40):
                return True
        return False

    def repl(m):
        tok = m.group(0)
        s, e = m.start(), m.end()
        if not _near(s, e):
            return tok
        # the marker branch runs BEFORE the placeholder check: a double-marker token (starts '['
        # AND ends ']' - e.g. [REDACTED]/live-secret/[REDACTED]) would otherwise read as a
        # placeholder and skip residue judgment, leaking the live inter-marker secret.
        if _MARKER_RE.search(tok):
            # an in-place marker is glued into this \S+ token; judge the marker-STRIPPED residue
            # as a WHOLE (exactly like the no-marker branch below) so a glued opaque secret
            # (sig=[REDACTED]&u=<secret>) is still caught and a glued path/SHA still survives - the
            # marker branch is never WEAKER (leak a shattered secret) nor STRONGER (gut a path).
            residue = _MARKER_RE.sub(" ", tok).strip()
            if (not residue or scrub._is_placeholder(residue)
                    or _safe_under_secret_noun(residue, residue, 0, len(residue))):
                return tok
            counter[0] += 1
            return "[REDACTED:secret]"
        if scrub._is_placeholder(tok):                  # a (non-marker) placeholder is not a secret
            return tok
        if _safe_under_secret_noun(tok, m.string, s, e):
            return tok
        counter[0] += 1
        return "[REDACTED:secret]"

    return _LONG_TOKEN.sub(repl, text)


def _scrub_str(v, scrub_pii, counter):
    s, n = scrub.scrub(v, scrub_pii=scrub_pii)
    counter[0] += n
    return _redact_opaque(s, counter)


def _scrub_value(v, scrub_pii, counter):
    """Scrub a value of any shape. Strings pass scrub + the opaque-token probe; dict KEYS
    are scrubbed too (a secret can ride in a key); non-container scalars are scrubbed via
    their string form, keeping the original type when nothing was redacted."""
    if isinstance(v, str):
        return _scrub_str(v, scrub_pii, counter)
    if isinstance(v, list):
        return [_scrub_value(x, scrub_pii, counter) for x in v]
    if isinstance(v, dict):
        return {_scrub_str(str(k), scrub_pii, counter): _scrub_value(x, scrub_pii, counter)
                for k, x in v.items()}
    if isinstance(v, bool) or v is None:
        return v
    s = _scrub_str(str(v), scrub_pii, counter)
    return s if s != str(v) else v


def _refuse_deep(obj, terms, counter):
    """Final overlay pass: redact user refusal terms in every string of the built enrichment
    (agent content + decision text/why). Metadata (did/spec/run_id/at/mode/categories) holds no
    such terms, so a blanket walk is harmless. Runs AFTER dids are computed, so a did stays stable
    and refusal-list-independent (it is a hash; it never leaked the term anyway). Keys are field
    names (not walked); only values are fenced."""
    if not terms:
        return obj
    if isinstance(obj, str):
        s, n = scrub.apply_refusal(obj, terms)
        counter[0] += n
        return s
    if isinstance(obj, list):
        return [_refuse_deep(x, terms, counter) for x in obj]
    if isinstance(obj, dict):
        return {k: _refuse_deep(v, terms, counter) for k, v in obj.items()}
    return obj


def _normalize(raw):
    """Coerce off-schema shapes so a stray type can't garble storage/retrieval:
    list fields wrap a lone str; summary joins a stray list."""
    out = {}
    if not isinstance(raw, dict):
        return out
    for k in _AGENT_FIELDS:
        v = raw.get(k)
        if v in (None, "", []):
            continue
        if k in _LIST_FIELDS:
            if isinstance(v, str):
                v = [v]
            elif isinstance(v, list):
                v = [x if isinstance(x, str) else str(x) for x in v]   # list[str] always
            else:
                v = [str(v)]
        elif k == "summary" and not isinstance(v, str):
            v = " ".join(str(x) for x in v) if isinstance(v, list) else str(v)
        out[k] = v
    return out


_CONVICTION_WORDS = {"high": 0.85, "medium": 0.5, "med": 0.5, "low": 0.2}


def _norm_conviction(c):
    """A conviction-at-creation block: value clamped to [0,1] + typed sub-fields. When the
    agent supplied NO usable value, mark `defaulted` - a fallback 0.5 is "no first-person
    stance was captured", NOT a real conviction, and keeping that distinction honest is the
    whole point of the primitive (a defaulted 0.5 must never read as earned confidence).

    Accepts the autonomous-enrichment shorthand: a bare "high"/"medium"/"low" string, or a
    dict whose `value` is one of those words, maps to a numeric stance - so swarm-captured
    decisions keep their confidence instead of silently defaulting to 0.5."""
    if isinstance(c, str):
        w = _CONVICTION_WORDS.get(c.strip().lower())
        c = {"value": w} if w is not None else {}
    if not isinstance(c, dict):
        c = {}
    raw_val = c.get("value")
    if isinstance(raw_val, str):
        raw_val = _CONVICTION_WORDS.get(raw_val.strip().lower())
    defaulted = False
    try:
        if raw_val is None:
            raise ValueError
        fv = float(raw_val)
        if not math.isfinite(fv):          # NaN/Inf is NOT a captured stance -> defaulted, not max
            raise ValueError
        val = max(0.0, min(1.0, fv))
    except Exception:
        val, defaulted = 0.5, True
    dom = c.get("domain")
    dom = [dom] if isinstance(dom, str) else (dom if isinstance(dom, list) else [])
    wwi = c.get("would_weaken_if")
    wwi = [wwi] if isinstance(wwi, str) else (wwi if isinstance(wwi, list) else [])
    block = {"kind": "conviction_at_creation", "value": val,
             "domain": [str(x) for x in dom][:8], "why": str(c.get("why", "")),
             "would_weaken_if": [str(x) for x in wwi][:6], "at": c.get("at") or _now()}
    if defaulted:
        block["defaulted"] = True      # 0.5 is a fallback, not a captured stance
    return block


def _canonical_text(text, counter=None):
    """Decision text, SCRUBBED canonically (vendor tokens + assignments + opaque prose
    tokens + PII) so neither the stored text NOR the did derived from it is a secret-bearing
    artifact. scrub_pii=True always: a did must be stable and secret-free regardless of the
    per-project PII config, so its hash input can't drift with config. Threads the caller's
    redaction counter so `red` reflects redactions performed in decision text too."""
    clean, n = scrub.scrub(str(text), scrub_pii=True)
    if counter is not None:
        counter[0] += n
    return _redact_opaque(clean, counter if counter is not None else [0])


def _norm_decisions(raw_decisions, prior_convictions, counter=None):
    """Structured decisions, each with a stable id and conviction-at-creation. A REAL captured
    conviction is NEVER overwritten (preserved by did) - the agent's in-the-moment stance is
    unrecoverable later. But a `defaulted` placeholder ("no stance captured") must NOT shadow
    a genuine conviction supplied on a later re-enrichment. did = hash of the SCRUBBED text."""
    out = []
    if not isinstance(raw_decisions, list):
        return out
    for d in raw_decisions:
        if isinstance(d, str):
            d = {"text": d}
        if not isinstance(d, dict) or not d.get("text"):
            continue
        text = _canonical_text(d["text"], counter)
        did = hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]
        prior = (prior_convictions or {}).get(did)
        incoming = _norm_conviction(d.get("conviction"))
        if isinstance(prior, dict) and not prior.get("defaulted"):
            conv = prior                              # real prior stance -> never overwrite
        elif isinstance(prior, dict) and incoming.get("defaulted"):
            conv = prior                              # both defaulted -> keep the earliest stamp
        else:
            conv = incoming                           # prior defaulted + incoming real (or no prior)
        out.append({"did": did, "text": text, "conviction": conv})
    return out


def build_enrich(raw, *, category_text="", scrub_pii=True, mode="llm", prior_convictions=None, refusal=None):
    """Agent's raw enrichment dict -> normalized, SCRUBBED, categorized, stamped overlay.
    The scrub-on-output guard: nothing reaches storage unscrubbed. Structured `decisions`
    carry conviction-at-creation, preserved across re-enrichment by did."""
    counter = [0]
    enrich = {}
    for k, v in _normalize(raw).items():
        enrich[k] = _scrub_value(v, scrub_pii, counter)
    decisions = _norm_decisions(raw.get("decisions") if isinstance(raw, dict) else None,
                                prior_convictions, counter)
    if decisions:
        enrich["decisions"] = _scrub_value(decisions, scrub_pii, counter)   # text/why scrubbed too
    topic_text = " ".join(t for t in enrich.get("topic", []) if isinstance(t, str))
    cats = categorize(category_text + " " + topic_text)
    if cats:
        enrich["categories"] = cats              # deterministic, not LLM
    enrich["spec"] = ENRICH_SPEC
    enrich["run_id"] = uuid.uuid4().hex[:12]
    enrich["at"] = _now()
    enrich["mode"] = mode
    if refusal:
        enrich = _refuse_deep(enrich, refusal, counter)   # final pass: fence the caller's own terms
    if counter[0]:
        enrich["red"] = counter[0]               # enrichment-output redaction count
    return enrich


def _chunk_text(chunk):
    parts = [chunk.get("user", ""), chunk.get("asst", "")]
    parts += chunk.get("files", []) + chunk.get("cmds", [])
    parts += chunk.get("tools", []) + chunk.get("skills", [])   # so --category catches them
    return " ".join(p for p in parts if p)


def apply_enrichments(cwd, mapping, *, scrub_pii=None, mode="llm"):
    """Persist {chunk_id: raw_enrich_dict} as an APPEND-ONLY overlay. Capture files are
    read READ-ONLY to locate each chunk (for category context); the overlay is appended to
    enrich/<date>.jsonl - so the append-only capture mirror is NEVER rewritten, and the
    Stop-hook-append vs enrich race is eliminated by construction (disjoint files)."""
    if not mapping:
        return 0
    cfg = store.load_config(cwd)
    if scrub_pii is None:
        scrub_pii = cfg["scrub_pii"]
    # the overlay is derived from a worker's output and can echo a term refused in ANOTHER project,
    # so its refusal fence is the WORKSPACE union - not just this project's list.
    refusal = store.workspace_refusal_terms()
    md = store.mirror_dir(cwd)
    if not md.exists() or store.is_reparse_point(md):
        return 0
    remaining = dict(mapping)
    prior_overlay = load_overlay(cwd)             # for conviction-at-creation preservation
    by_overlay = {}                               # overlay Path -> [{id, enrich}, ...]
    # _safe_iterdir + no-follow read (RC5L-H), not glob()+read_text(): both FOLLOWED a junctioned
    # mirror/<key> and a symlinked shard leaf, feeding external content into enrichment as if it
    # were the workspace's own captured mirror.
    for f in sorted(p for p in store._safe_iterdir(md, store.STORE_ROOT) if p.suffix == ".jsonl"):
        if not remaining:
            break
        try:
            lines = store._read_text_no_follow(f, store.STORE_ROOT).splitlines()   # read-only
        except Exception:
            continue
        for ln in lines:
            ln = ln.strip()
            if not ln:
                continue
            try:
                chunk = json.loads(ln)
            except Exception:
                continue
            if not isinstance(chunk, dict):
                continue
            cid = chunk.get("id")
            if cid and cid in remaining:
                # belt-and-braces (do-not-index fence): never derive an enrichment overlay from a
                # human_only source chunk - one that slipped into the mirror before the fence
                # existed. Drop it from the queue instead of indexing it.
                if store.is_human_only_record(chunk, cfg):
                    remaining.pop(cid, None)
                    continue
                prior = prior_overlay.get(cid) or {}
                prior_convs = {d["did"]: d.get("conviction") for d in prior.get("decisions", [])
                               if isinstance(d, dict) and d.get("did")}
                ov = build_enrich(remaining.pop(cid), category_text=_chunk_text(chunk),
                                  scrub_pii=scrub_pii, mode=mode, prior_convictions=prior_convs, refusal=refusal)
                # per-PID shard: two concurrent windows write DIFFERENT files (never the
                # same), so the append is race-free with no lock and no rewrite. Named by
                # the DATE, not f.stem - a mirror shard's stem carries its writer's pid,
                # which would compound the name and dodge rotation's day-file matcher.
                date = store.mirror_date(f) or f.stem
                f_ov = store.enrich_dir(cwd) / f"{date}.{os.getpid()}.jsonl"
                by_overlay.setdefault(f_ov, []).append({"id": cid, "enrich": ov})
    if not by_overlay:
        return 0
    written = 0
    for ov_path, entries in by_overlay.items():
        for e in entries:
            try:
                store.append_record(ov_path, e, cfg)    # SOLE write path: scrubs values, serializes, appends
                written += 1
            except Exception:
                pass
    return written


def _ov_key(e):
    # deterministic resolution on CONTENT, not file/append order: spec, then ISO `at`
    # (lexicographic == chronological), then run_id as a final stable tiebreak.
    return (_spec_version(e.get("spec")), e.get("at") or "", e.get("run_id") or "")


def _coerce_overlay(e):
    """Shape-normalize a stored overlay at the JOIN boundary so a cross-version / off-shape
    overlay can never crash retrieval: list fields -> list[str], summary -> str."""
    if not isinstance(e, dict):
        return {}
    out = dict(e)
    for k in ("topic", "keywords", "search_queries", "facts", "categories"):
        v = out.get(k)
        if v is None:
            continue
        if isinstance(v, str):
            out[k] = [v]
        elif isinstance(v, list):
            out[k] = [x if isinstance(x, str) else str(x) for x in v]
        else:
            out[k] = [str(v)]
    if "summary" in out and not isinstance(out["summary"], str):
        out["summary"] = str(out["summary"])
    return out


def load_overlay(cwd=None, project=None, archive=False):
    """Map chunk id -> latest enrich overlay across ALL shards (archive=True adds the rotated
    archive/ shards, so an --archive read joins overlays onto archived chunks). Resolution is
    deterministic on content ((spec, at, run_id)), not file/append order; each result is
    shape-coerced."""
    if project:
        if not store.validate_project_key(project):   # never let a traversal escape STORE_ROOT
            return {}
        ed = store.STORE_ROOT / "projects" / project / "enrich"
    else:
        ed = store.enrich_dir(cwd or os.getcwd())
    # _safe_iterdir + no-follow read (RC5L-H): the federated project= branch's validate_project_key
    # guards only the <project> COMPONENT, not the enrich/ subdir beneath it, and the default cwd
    # branch had no check at all - a junctioned enrich/ or enrich/archive was enumerated and its
    # external overlays joined onto retrieval as if they were the workspace's own.
    files = ([p for p in store._safe_iterdir(ed, store.STORE_ROOT) if p.suffix == ".jsonl"]
             if ed.exists() and not store.is_reparse_point(ed) else [])
    adir = ed / "archive"
    if archive and adir.exists() and not store.is_reparse_point(adir):
        files += [p for p in store._safe_iterdir(adir, store.STORE_ROOT) if p.suffix == ".jsonl"]
    files.sort()
    if not files:
        return {}
    chosen = {}                                   # id -> (key, enrich)
    real_conv = {}                                # did -> (at, conviction): earliest REAL stance
    for f in files:
        try:
            lines = store._read_text_no_follow(f, store.STORE_ROOT).splitlines()
        except Exception:
            continue
        for ln in lines:
            ln = ln.strip()
            if not ln:
                continue
            try:
                o = json.loads(ln)
            except Exception:
                continue
            if not (isinstance(o, dict) and o.get("id") and isinstance(o.get("enrich"), dict)):
                continue
            enr = o["enrich"]
            # conviction-at-creation is NEVER lost: track the earliest non-`defaulted` stance per
            # did across ALL shards, so a concurrent defaulted write can't shadow it at the join
            # (the in-process preserve in _norm_decisions can't see a sibling shard's race).
            for d in (enr.get("decisions") or []):
                if isinstance(d, dict) and d.get("did") and isinstance(d.get("conviction"), dict):
                    c = d["conviction"]
                    if not c.get("defaulted"):
                        at = str(c.get("at") or enr.get("at") or "")
                        rk = (o["id"], d["did"])    # PER-CHUNK: did hashes decision TEXT, so two
                        cur = real_conv.get(rk)      # distinct chunks can share a did - don't bleed
                        if cur is None or at < cur[0]:
                            real_conv[rk] = (at, c)
            k = _ov_key(enr)
            cur = chosen.get(o["id"])
            if cur is None or k >= cur[0]:
                chosen[o["id"]] = (k, enr)
    result = {}
    for cid, (_k, e) in chosen.items():
        e = _coerce_overlay(e)
        for d in (e.get("decisions") or []):       # restore a real conviction over a defaulted placeholder
            if (isinstance(d, dict) and isinstance(d.get("conviction"), dict)
                    and d["conviction"].get("defaulted") and (cid, d.get("did")) in real_conv):
                d["conviction"] = real_conv[(cid, d["did"])][1]
        result[cid] = e
    return result


def list_pending(cwd, limit=50):
    """Capture chunks with no overlay, or an overlay below the current spec. Iterates by
    calendar DATE (newest first), reading all of a date's shards before checking the limit -
    so the truncation lands on a date boundary, as it did pre-sharding, and a date's shards
    are treated as one unit rather than racing each other for the limit."""
    md = store.mirror_dir(cwd)
    overlay = load_overlay(cwd)
    # OUTPUT sink: pending chunks are printed for a worker (and staged into its inbox), so they fence
    # with the WORKSPACE refusal union, not just this project's list - a term refused in ANOTHER
    # project must not surface here. Computed ONCE (workspace_refusal_terms re-reads every config).
    cfg = store.load_config(cwd)
    wterms = store.workspace_refusal_terms()

    def _out(text, cap):
        s, _ = scrub.scrub((text or "")[:cap], scrub_pii=cfg["scrub_pii"], refusal=wterms)
        return s
    pend = []
    if not md.exists() or store.is_reparse_point(md):
        return pend
    # _safe_iterdir + no-follow read (RC5L-H): the old glob()+read_text() enumerated and read
    # through a junctioned mirror/<key> unfenced; --list-pending is a PUBLIC command that prints and
    # stages its results, so a linked mirror dir was both a read AND a disclosure sink.
    md_files = list(store._safe_iterdir(md, store.STORE_ROOT))
    dates = sorted({d for d in (store.mirror_date(f) for f in md_files
                                if f.suffix == ".jsonl") if d}, reverse=True)
    for date in dates:
        day_files = sorted(f for f in md_files if f.name == date + ".jsonl"
                           or (f.name.startswith(date + ".") and f.name.endswith(".jsonl")))
        for f in day_files:
            try:
                lines = store._read_text_no_follow(f, store.STORE_ROOT).splitlines()
            except Exception:
                continue
            for ln in lines:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    c = json.loads(ln)
                except Exception:
                    continue
                if not isinstance(c, dict) or not c.get("id"):
                    continue                      # an id-less chunk can't be targeted
                e = overlay.get(c["id"])
                # pending only if below the current spec - never re-enrich a NEWER overlay.
                if not e or _spec_version(e.get("spec")) < _CURRENT_VER:
                    pend.append({"id": c.get("id"), "ts": c.get("ts"),
                                 "user": _out(c.get("user"), 600),
                                 "asst": _out(c.get("asst"), 1800)})
        if len(pend) >= limit:
            break
    return pend[:limit]


_APPLY_JSON_OBJ = re.compile(r"\{.*\}", re.DOTALL)


def _parse_apply_json(raw):
    """Parse an apply-mapping, tolerating a stray ```json fence / trailing prose (the same
    tolerance enrich_apply._load_overlay gives worker output). Raises json.JSONDecodeError
    if there is no parseable object at all."""
    raw = raw.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        m = _APPLY_JSON_OBJ.search(raw)
        if m:
            return json.loads(m.group(0))   # a fenced/prefixed object; may still raise, caller catches
        raise


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")   # Windows defaults cp1252; chunks are UTF-8
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="Parsica Weave enrichment persistence.")
    ap.add_argument("--list-pending", action="store_true")
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--apply-stdin", action="store_true",
                    help="read the {chunk_id: {...}} map from STDIN (preferred - no temp file on disk)")
    ap.add_argument("--apply", help="inline JSON: {chunk_id: {topic, summary, ...}}")
    # There is no file-path argument. Until October 2026 a hidden one, enabled by an environment
    # variable, read ANY path given on the command line as enrichment JSON with no containment
    # check (REVIEW-2026-10.md, pile one, class (d), enrich.py 607 and 631-635). Enrichment JSON
    # arrives on stdin or inline; nothing is opened by path.
    ap.add_argument("--cwd", default=os.getcwd())
    args = ap.parse_args()

    # applying enrichments is a durable write: an isolated (zero-footprint) session refuses it.
    # --list-pending stays available (a pure read).
    if store.is_isolated() and (args.apply_stdin or args.apply):
        sys.stderr.write("parsica enrich refused: PARSICA_ISOLATED=1 (isolated session, "
                         "zero footprint - nothing enriched)\n")
        sys.exit(3)

    if args.list_pending:
        # caveat on stderr so the JSON on stdout stays machine-parseable for the enrich pipeline.
        # stderr.write, not print(file=): the store-gateway AST scan flags any print(file=) as a write.
        sys.stderr.write(store.REFERENCE_CAVEAT + "\n")
        print(json.dumps(list_pending(args.cwd, args.limit), ensure_ascii=False, indent=2))
        return
    mapping = None
    try:
        if args.apply_stdin:
            mapping = _parse_apply_json(sys.stdin.buffer.read().decode("utf-8", "replace"))  # UTF-8, not cp1252
        elif args.apply:
            mapping = _parse_apply_json(args.apply)
    except (json.JSONDecodeError, OSError) as exc:
        # a friendly line, not a raw traceback: the agent pipes LLM output through here at every
        # checkpoint and an LLM commonly wraps JSON in a ```json fence or trailing prose.
        store.emit_text(f"could not parse the enrichment JSON to apply: {exc}", stream=sys.stderr)
        sys.exit(1)
    if not isinstance(mapping, dict):
        sys.stderr.write("nothing to apply (need --apply-stdin or --apply with a {id: {...}} map)\n")
        return
    n = apply_enrichments(args.cwd, mapping)
    store.emit_text(f"enriched {n} chunk(s) at spec {ENRICH_SPEC}")


if __name__ == "__main__":
    main()
