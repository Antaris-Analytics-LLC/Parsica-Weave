---
name: parsica-weave
description: >-
  Workspace continuity for Claude Code: an agent-maintained working set per project
  (pinned facts + open loops), an enriched, queryable chat mirror, and a permanent
  workspace-level journal + lessons file shared across every project. Use when you
  need to recall earlier context, orient at the start of a session, retrieve what
  happened across days of work in any project, or record a lesson learned.
  Continuity survives compaction, new sessions, and project switches.
---

# Parsica Weave

_Installs as the `parsica-weave` Claude Code plugin; Parsica Weave is the product name._

A mini-Parsica for the harness. Claude Code already persists the full chat to a
transcript JSONL; this skill chunks it, enriches it (deterministic tags), scrubs
it (credentials/PII), and re-injects the right pieces at the right moments. The
philosophy is **retrieval over dumping**: granular, tagged, provenance-stamped
recall beats re-reading history.

_First time living on this system? Read `ONBOARDING.md` before this reference:
it carries the retrieval habit that makes everything below worth having._

## The vocabulary

- **Workspace** - the entire Claude Code presence. The top-level container; the store.
- **Project** - one repo, keyed by its git root. Scopes the focus, never the self.
- **Channel** - a named lane of sessions (workspace-level registry).
- **Session** - one live conversation. The only boundary for live context.

You are one agent across all of them. The durable self (lessons, journal) is
workspace-level; the working set and mirror are per-project; only live context is
per-session.

## The three layers

1. **Enriched chat mirror** (`<ws>/mirror/<project-key>/<date>.<pid>.jsonl` - one
   shard per writing window, so concurrent windows never share an append target;
   legacy `<date>.jsonl` files stay readable) - every turn, chunked and tagged (files
   touched, commands run, tools used, skill attribution, timestamp, session).
   Scrubbed before write. Retained by **active days** per calendar date
   (default 14 - quiet days don't age out a project); older shards rotate to
   `archive/`, never deleted. This is what you *query*.
2. **Working set** (`<ws>/working/<project-key> - Working Set.md`) - **PINNED FACTS**
   (durable, never let them drift) + **OPEN LOOPS** (the live thread list), per
   project. You maintain this. It is re-injected at every SessionStart in that
   project - the part that survives everything.
3. **Permanent journal + lessons** (`<ws>/journal/<date>.md`, `<ws>/lessons.md`) -
   the daily summary and your self-authored **scars** (lessons learned, re-read at
   boot, never auto-pruned). Workspace-level: one journal and one lessons file for
   every project you work in, each appended entry stamped
   `(YYYY-MM-DD HH:MM UTC · session <sess8> · <project-key>)`. Journal pages past the
   active window (default 7 days) rotate to `journal/archive/`; nothing is deleted.

`<ws>` = `~/.claude/parsica-weave/`, one workspace for everything. Projects key on
the git root, so any subdirectory of a repo resolves to the same project.

## Your responsibilities (the discipline)

The hooks do the capture; **you** keep the human-judgment layers sharp. **Never edit
the working set, `lessons.md`, or the journal directly** - always pipe your content through the
`note` wrapper, which scrubs it for credentials/PII *before* it lands on disk. Hand-editing
these files bypasses the safety layer and writes a pasted secret straight to disk.
`journal` and `lesson` appends are stamped with time, session, and project; pass
`--sess <sess8>` so the stamp names your session (the checkpoint directive embeds it).

- **Maintain the working set** (pinned facts + OPEN LOOPS). When the branch, posture, a key
  decision, or a hard constraint changes - or when you start/finish a thread - refresh it. Keep
  it terse and *true*; stale pinned facts are worse than none. Pipe the **full refreshed doc**
  (the wrapper replaces it in place):
  ```
  echo '<markdown>' | python "${CLAUDE_PLUGIN_ROOT}/scripts/pcx.py" note --kind working --cwd "<C>"
  ```
- **Write lessons (scars).** When you hit a wall, get something wrong, or learn something
  load-bearing, record a dated entry - what happened + the rule learned, one scar per entry.
  This is what feeds judgment over time:
  ```
  echo '<markdown>' | python "${CLAUDE_PLUGIN_ROOT}/scripts/pcx.py" note --kind lesson --cwd "<C>"
  ```
- **Keep a journal.** At each ~20-turn checkpoint (the hook prompts you), record a tight
  ~150-word entry - key decisions, current task/phase, open threads, next step:
  ```
  echo '<markdown>' | python "${CLAUDE_PLUGIN_ROOT}/scripts/pcx.py" note --kind journal --cwd "<C>"
  ```

## Retrieving (retrieval-first)

Don't re-read history - query the mirror. Run from the project dir:

```
python "${CLAUDE_PLUGIN_ROOT}/scripts/query.py" --grep "<term>"
python "${CLAUDE_PLUGIN_ROOT}/scripts/query.py" --file effects.py --full
python "${CLAUDE_PLUGIN_ROOT}/scripts/query.py" --cmd "git commit"
python "${CLAUDE_PLUGIN_ROOT}/scripts/query.py" --tool Edit --since 3
python "${CLAUDE_PLUGIN_ROOT}/scripts/query.py" --grep auth --full --limit 5
python "${CLAUDE_PLUGIN_ROOT}/scripts/query.py" --grep auth --workspace
python "${CLAUDE_PLUGIN_ROOT}/scripts/query.py" --grep auth --archive
```

Flags combine (AND). Each hit carries its timestamp + session id (+ channel when
tagged), so you can jump back to the exact transcript slice if you need the full
detail. `--workspace` searches every project's mirror; `--archive` folds in rotated
shards. Prefer pulling the relevant 2-3 chunks over reloading the whole conversation.

## Enriching the mirror (semantic layer)

The mirror captures each turn deterministically (text + files/commands/tools). On top of
that, add a **semantic overlay** so retrieval matches how a person actually searches - the
difference between "I tagged the files" and "I can find the turn by what it was *about*."

**When:** at each ~20-turn checkpoint (the directive reminds you). The checkpoint way is
autonomous: do NOT hand-author overlays - spawn the background enrichment swarm via the
Workflow tool (see `AUTONOMOUS_ENRICHMENT.md`) and let it work the pending queue. Enrich
in passes, never per turn.

**How (the manual fallback, for when the swarm is unavailable):**
1. `python "${CLAUDE_PLUGIN_ROOT}/scripts/enrich.py" --list-pending` -> chunks
   needing enrichment (id + text). Enrich from the returned chunk **text** - the stored,
   already-scrubbed record - not from raw session recall, so the overlay stays faithful to
   what was captured and cannot reintroduce content the capture scrub removed.
2. For each chunk, produce this JSON:
   ```json
   {
     "topic":          ["lowercase_snake_case", "..."],
     "summary":        "one sentence, rephrased in DIFFERENT words than the turn",
     "keywords":       ["natural search phrase", "..."],
     "search_queries": ["a specific question only THIS turn answers", "..."],
     "facts":          ["atomic standalone statement", "..."],
     "decisions":      [{"text": "a decision/stance you took, one line",
                         "conviction": {"value": 0.0, "domain": ["security"],
                                        "why": "...", "would_weaken_if": ["..."]}}]
   }
   ```
3. Persist by **piping** the `{chunk_id: {...}}` map through stdin - never write raw
   enrichment JSON to a workspace temp file (that's an un-scrubbed disk surface):
   ```
   echo '<json>' | python "${CLAUDE_PLUGIN_ROOT}/scripts/enrich.py" --cwd "<C>" --apply-stdin
   ```
   It scrubs the output, adds deterministic categories, stamps the spec version, and writes
   it in place.

**What makes a good overlay:**
- **topic** - 5-10 concept tags, lowercase snake_case. Tag the *concept and its synonyms*,
  not just the literal token: `authentication` for a JWT change, `deployment` for
  Kubernetes, `cost_tracking` for a billing fix. Breadth over depth.
- **summary** - one sentence that *rephrases* the turn in different vocabulary (adds search
  surface, doesn't echo the text). Not a truncation.
- **keywords** - 8-15 phrases the way a person would actually type them into search.
- **search_queries** - 3 SPECIFIC questions that *only this turn* answers; embed the exact
  names (functions, files, decisions). Discriminative - they should NOT match a hundred
  other turns. This is the highest-leverage field for retrieval.
- **facts** - 1-5 atomic, standalone statements, or `[]`.
- Put any exact proper noun somewhere in `topic` OR `search_queries`.

**Decisions + conviction.** When a turn contains a real *decision* or stance,
add it to `decisions` with your **conviction-at-creation** - `value` (0-1, how strongly
this should shape future behavior), `domain`, `why`, and `would_weaken_if`. This is your
authentic first-person signal, captured *once* and **never overwritten** on re-enrichment.
Later, when a decision proves out or fails, record it against its `did` (visible in the
stored overlay / `query.py --full`):

```
python "${CLAUDE_PLUGIN_ROOT}/scripts/calibrate.py" --cwd <C> --target <did> --event confirmed|contradicted|eroded --source <test/file/why>
```

Retrieval then weights a decision by its **earned** conviction (conviction x calibration)
and shows the trajectory - conviction at creation, calibration, the ok/x tally, effective
weight - never a single collapsed number. Don't re-rate an old decision; record events and
let the calibration move.

**Never** hand-write a secret into an enrichment field (or a decision/scar) - but if one
rides in from raw context, `enrich.py` scrubs every field before storage as defense-in-depth.

## Safety

Everything is **scrubbed before storage** - credentials (key prefixes, PEM blocks,
assignment-style secrets) always; PII (emails, phones, SSNs, cards) by default
(configurable). Entropy-based redaction is deliberately **off** so git SHAs, file
hashes, and version strings survive intact. Never paste a raw secret into
the working set / `lessons.md` / the journal - but if one slips through, the
SessionStart re-inject is scrubbed again as defense-in-depth. Every producer (mirror,
notes, enrichment, calibration) reaches disk through a single write gateway in `store.py`,
so scrubbing happens by construction, not by each caller remembering to call it.

**Refusal list (your own terms).** Beyond the automatic secret/PII scrub, set `refusal` in config
to a list of words or phrases to fence: `"refusal": ["ProjectX", "internal codename"]`. Every match
renders as `<redacted>`, at two scopes: write-time scrub (capture, the `note` wrapper, enrichment
overlays, the calibration ledger) applies the union of the global and current-project refusal
lists; every OUTPUT sink (the SessionStart re-inject, hook injections, and all CLI output) applies
the workspace-wide union of every project's refusal terms, so a term configured anywhere in the
workspace never surfaces anywhere. Matching is
case-insensitive and by SUBSTRING, so a term is caught anywhere it appears, including welded into a
word (`media_ProjectX`, `codenamedProjectX`, `ProjectXClient`). This deliberately over-redacts
(refuse `mars` and `marsupial` also goes) on the principle that a secrecy fence must never leak a
listed term; choose specific terms to limit collateral. This keeps capture running through
sensitive work with your terms held out, instead of skipping continuity entirely.

## Configuration

`~/.claude/parsica-weave/config.json` (global) or
`<ws>/projects/<project-key>/config.json` (per-project override). Keys:
`mirror_active_days` (default 14; older shards rotate
to `archive/`), `journal_active_days` (7), `checkpoint_every_n` (20, per session),
`scrub_pii` (true), `max_inject_bytes` (12000), `refusal` (a list of your own
words/phrases to redact; see Safety), `boot_recent_elsewhere` (false; see below), and
`agent_name` + `byline` - set through their front door, the identity verb:
`parsica-weave name --name <name>` (show: bare `name`; states: `--byline on|name-only|off`;
clear: `--reset`; one name per workspace, config-only, never a store key). When a name is
set, open every response with `[Name · <session-tag>]` - the chosen name, visible on every
reply; `byline_session_tag: false` for name-only.
By default, Weave keeps live boot context project-scoped. The same agent can query
workspace memory on demand, but other projects' recent live work is not automatically
injected. Set `boot_recent_elsewhere=true` to enable a small "Recent elsewhere" block at
SessionStart: a recap of recently captured turns from your other sessions (not awareness of
anything live - a recap of the recent past). Even enabled, it
is a snapshot, not a stream: a one-time boot digest of already-captured, already-scrubbed
chunks, never another session's live context. Weave has no mode that shares a session's live
window across projects. The on-demand capability (`query.py --recent N`) stays available
regardless of this setting.
Channels: the `PARSICA_CHANNEL` env var names the session's lane; the registry is
workspace-level, chunks tag with an immutable channel id. Catch-up is deliberate:
`pcx.py sync --hours N` digests the whole workspace; registry: `pcx.py channel --list`.
`PARSICA_ISOLATED=1` runs a zero-footprint session: full recall, nothing durable
written; every standalone writer refuses (`note`/`promote`/`migrate`, `calibrate`
event recording, and the enrichment pipeline `enrich`/`enrich_prep`/`enrich_apply`),
and query federation neither mints workspace identity nor emits a router trace. See
`README.md` for install + hook wiring.
