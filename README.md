# Parsica Weave

One continuous agent across every project and session. Weave captures every Claude Code
session automatically, scrubs it for secrets and PII before anything touches disk, and
gives the agent real recall of days and weeks of work - not by re-reading old
transcripts, but by querying an enriched, provenance-stamped mirror of everything that
happened. Continuity survives compaction, restarts, new sessions, and project switches:
compaction just happens, and you carry on like nothing did.

## At a glance

- **Automatic capture** - every turn chunked, tagged (files, commands, tools, session,
  time), scrubbed, and stored; nothing to remember to do
- **Scrub before store** - credentials and PII redacted at a single tested write
  gateway no code path can skip
- **Retrieval over dumping** - `query --grep` returns the right three chunks instead of
  reloading a conversation
- **Working set, journal, lessons** - a living per-project scratchpad plus
  workspace-level history and hard-won lessons, re-injected at every session start
- **Compaction survival** - complete turns flushed before compaction, orientation
  re-injected after; the full record stays queryable forever
- **Semantic enrichment** - topics, summaries, search queries, atomic facts, and
  decisions with conviction layered over the raw mirror
- **Refusal and human-only lists** - your own never-to-appear terms, managed by CLI or
  slash command, fenced everywhere content reaches the store
- **Channels and isolation** - named session lanes, plus `PARSICA_ISOLATED=1` for full
  recall with zero footprint
- **Local-first, stdlib-only** - no pip dependencies, no network calls, no telemetry;
  plain-text files under `~/.claude/parsica-weave/` that are yours
- **Retention rotates, never deletes** - aged shards move to `archive/`, still
  queryable; no code path unlinks user data

## Quick start

```
# 1. install + enable the parsica-weave plugin, then verify:
parsica-weave doctor

# 2. work a few turns in any project, then ask your history:
parsica-weave query --grep "something you said"

# 3. name your agent (optional, recommended):
parsica-weave name --name Aria
```

From here capture is automatic; point your agent at `ONBOARDING.md` for its first thread.

## Contents

- [What it does](#what-it-does)
- [What's inside](#whats-inside)
- [Requirements](#requirements)
- [How to install](#how-to-install)
- [How to use it](#how-to-use-it) - naming, querying, notes, enrichment
- [Features you should not overlook](#features-you-should-not-overlook)
- [Channels](#channels-scope-sessions-into-named-lanes)
- [Configuration](#configuration)
- [Data, retention, and uninstall](#data-retention-and-uninstall)
- [Pairs with Parsica Spirit](#pairs-with-parsica-spirit)

**The trust model, in one paragraph.** Weave is a Claude Code tool, not a sealed vault.
Hook-injected recall becomes Claude Code context, and the optional enrichment workflow
asks Claude workers to summarize staged chunks, so recalled or enriched text may be sent
to the model according to your Claude Code and Anthropic settings. Use `[[human-only]]`,
refusal terms, and project policy for anything that must never be re-injected or
enriched.

---

## What it does

Four words, used precisely throughout to describe how Parsica breaks Claude Code into tiers:

- **Workspace** - your entire Claude Code presence. The top-level container; the store.
- **Project** - one repo, keyed by its git root. Scopes the focus, never the self.
- **Channel** - a named lane of sessions, registered at the workspace level.
- **Session** - one live conversation. The only boundary for live context.

The durable self persists across sessions: lessons and journal are workspace-level and
shared by every session, while the working set is scoped to its project; only live context
is per-session. Three layers, one per kind of memory:

1. **Enriched chat mirror**, the first layer of continuity, `mirror/<project-key>/<date>.<pid>.jsonl`.
   Live context is dual-written to this file as the session runs. Every turn is chunked and
   tagged with the files touched, commands run, tools used, timestamp, and session id, then
   scrubbed and stored. Every session stores its shards split by project, so this is the layer
   you query. Each record inside a shard carries its own session tag plus provenance, so if one
   process handles multiple sessions over its lifetime, they land in the same shard file but
   stay distinguishable - every hit you pull back via `query.py` carries its session tag. It
   retains by **active days** (days with activity, default 14), so a quiet week does not age out
   a project; older shards rotate to `archive/`, still queryable indefinitely, just separated
   to keep the main store uncluttered - never deleted.
2. **Working set**, `working/<project-key> - Working Set.md`. Pinned facts and open
   loops for one project, load-bearing details the agent maintains, re-injected at every
   session start in that project. This is the part that survives everything.
3. **Journal and lessons**, `journal/<date>.md` and `lessons.md`. Daily summaries and
   self-authored lessons learned and mistakes made, kept at the workspace level: one journal and
   one lessons file for all your work, every entry stamped with its time, session, and project.
   Kept forever, never auto-pruned; journal pages past the active window (default 7 days) rotate
   to `journal/archive/`.

   **Note:** it's worth having your agent keep note of the good and the bad, with the
   consequences either direction had - an agent with provenance to refer back to is one that
   improves over time. These small habits become primary differentiators the better you use them.

On top of the mirror sits a **semantic enrichment overlay**: topics, summaries in
different vocabulary, search queries only that turn answers, atomic facts, and
**decisions with conviction**, each decision recorded with how strongly it should shape
future behavior, then calibrated over time as it proves out or fails.

The store is one workspace at `~/.claude/parsica-weave/`:

```
~/.claude/parsica-weave/
  lessons.md                         workspace lessons, append-only, stamped
  journal/<date>.md                  active pages; archive/ holds rotated ones
  working/<key> - Working Set.md     one visible scratchpad per project
  mirror/<key>/<date>.<pid>.jsonl    captured chat; archive/ holds rotated shards
  channels.json                      workspace-level channel registry
  projects/<key>/                    machine state: cursors, config, enrichment, calibration
  config.json / workspace.json       global config; the workspace id
```

Projects key on the git root, so every subdirectory of a repo resolves to the same
project. A project scopes what the agent is focused on; it never fences off what the
agent knows.

**Data transparency is important to us.** We strongly believe you should always know where
everything is, as well as have the ability to remove it if you want to.

## What's inside

```
parsica-weave/
  .claude-plugin/
    plugin.json               # plugin manifest (name, version, metadata)
  bin/
    parsica-weave.py          # CLI dispatcher (per-platform shims beside it)
  hooks/
    hooks.json                # auto-wires the five continuity hooks when the plugin is enabled
    weave-run.sh              # hook launcher: probes python3, python, py -3
  commands/
    name.md                   # /name  - the naming ceremony for this workspace's agent
    whoami.md                 # /whoami - the agent's identity: name, byline state, live preview
  skills/
    weave/
      SKILL.md                # agent-facing discipline + reference
  README.md                   # this file
  ONBOARDING.md               # the agent's first thread: habits, the router, first 15 minutes
  TO_THE_NEXT_AGENT.md        # one agent's own account of a real release night - what it felt like to use this for real
  AUTONOMOUS_ENRICHMENT.md    # the background enrichment swarm: what, why, setup, verification
  config.example.json         # every config key, real defaults, valid JSON - copy, don't retype
  scripts/
    pcx.py                    # hook engine (Stop / UserPromptSubmit / SessionStart / PreCompact / PostToolUse) + `name` / `note` / `promote` / `channel` / `sync` / `migrate`
    scrub.py                  # credential/PII redactor + user refusal list (scrub-before-store)
    store.py                  # paths, project keying, config, and the sole-path write gateway
    enrich.py                 # semantic overlay: scrub-on-output guard, categories, decisions + conviction
    enrich_prep.py            # enrichment swarm, scan stage: stage pending chunks to an inbox
    enrich_apply.py           # enrichment swarm, apply stage: validate + apply through the gateway
    enrich-swarm.workflow.js  # the swarm itself: scan, cheap-worker fan-out, apply
    calibrate.py              # conviction calibration ledger: confirm/contradict events earn weight
    doctor.py                 # install self-check: layout, scrub gate, capture
    verify_salt.py            # recovery check: does a restored salt match the ledger fingerprint?
    query.py                  # retrieval (--grep / --file / --cmd / --tool / --since), overlay-ranked
    test_*.py                 # test suites: scrub, engine, gateway, enrichment, calibration, concurrency
```

## Requirements

- Requires Python 3 on PATH (the launcher probes `python3`, then `python`, then `py -3`,
  skipping the Windows Store `python3` stub)
- A `bash` for the hooks: standard on macOS and Linux; on Windows, Git Bash (install Git
  for Windows)
- Claude Code with hooks enabled

## How to install

Weave ships as a Claude Code plugin. Install it as a plugin (see the Parsica site /
your plugin marketplace) and enable it - that is the whole install. The whole package -
its code, GitHub issues, and documentation - can all be found in the repo itself.

> **Before you enable:** once enabled, the plugin's hooks run in **every** Claude Code
> project, all feeding the same workspace store - not only this one. That is the point:
> one agent, everywhere. If you want it off for a session, set `PARSICA_WEAVE=off`; for
> a session that recalls everything and records nothing, set `PARSICA_ISOLATED=1`.

1. Install the `parsica-weave` plugin and enable it.

   The plugin bundles its own `hooks/hooks.json`, so the five continuity hooks (Stop,
   UserPromptSubmit, SessionStart, PreCompact, PostToolUse) **auto-wire the moment the
   plugin is enabled**. There is no `~/.claude/settings.json` block to merge by hand.

2. Verify the install and its guarantees:

```
parsica-weave doctor
```

`parsica-weave` is the dispatcher in the plugin's `bin/`; put `bin/` on your PATH to call
it by name, and it resolves a working Python for you. Without it on PATH, run the script
directly, using whichever name is your Python 3:
`python "${CLAUDE_PLUGIN_ROOT}/scripts/doctor.py"`.

`doctor` checks the layout, that the scrub gate redacts secrets and PII, that capture
works, and - the one worth seeing for yourself - that `PARSICA_ISOLATED=1` writes zero
files. It exits non-zero if anything fails. Then work a few turns in any project and query
what you said:

```
parsica-weave query --grep "something you said"
```

From here, capture is automatic. Every session in every project feeds the same
workspace, and the agent wakes up in any of them already knowing all of it.

3. Point your agent at `ONBOARDING.md`. It is the agent's first thread: the retrieval
   habit that makes the store worth having, the two-floor router, and a first-fifteen-
   minutes checklist. Written by the first agent onboarded, for the next one.

## How to use it

### Naming your agent

Likely the first real thing you do after install - the naming ceremony:

```
parsica-weave name --name Aria       name this workspace's agent
parsica-weave name                   show the name, byline state, and a live preview
parsica-weave name --byline on|name-only|off
parsica-weave name --reset           back to no name, no byline
```

(`parsica-weave agent ...` was the v1.0 spelling and still works, unchanged.)

Inside Claude Code the same two things are slash commands: `/name <name>` runs the
ceremony, and `/whoami` shows the current identity, byline state, and live preview.

One name per workspace, by design. The name is a display label, never a store key, so
renaming or resetting never touches a single memory. Once named, the agent is instructed to
open every reply with `[Name · <session-tag>]` - the byline - so the name you chose is
visible on every response. This is a prompt-layer convention the agent honors, not something
the plugin enforces on the reply text. `agent_name` in config still works; the verb is just
the front door.

### Query your history

Run from the project directory. Flags combine (AND):

```
python "${CLAUDE_PLUGIN_ROOT}/scripts/query.py" --grep "auth bug"
python "${CLAUDE_PLUGIN_ROOT}/scripts/query.py" --file effects.py --full
python "${CLAUDE_PLUGIN_ROOT}/scripts/query.py" --cmd "git commit" --since 3
python "${CLAUDE_PLUGIN_ROOT}/scripts/query.py" --tool Edit --grep retry
python "${CLAUDE_PLUGIN_ROOT}/scripts/query.py" --grep "auth bug" --workspace
python "${CLAUDE_PLUGIN_ROOT}/scripts/query.py" --grep "auth bug" --archive
python "${CLAUDE_PLUGIN_ROOT}/scripts/query.py" --list-projects
```

Every hit carries its timestamp and an opaque per-session tag (and channel, when
tagged), so you can jump back to the exact moment; filter with the 8-character id
shown in the byline. `--full` shows the whole chunk plus its semantic
overlay. `--workspace` searches every project's mirror; `--archive` folds in the
rotated shards. More flags: `--category` filters by the enrichment overlay's semantic
category (e.g. `security`, `testing`), `--limit` caps results, `--project` targets
another project's store, and `--all-sessions` includes the current session in
`--recent N`.

`--recent` has two dials of its own: `--hours` sets the window (a float, 12 by default)
and `--sess <id>` names the current session so `--recent` can leave it out, which is what
makes the view "what happened elsewhere" rather than an echo of the session you are in.
`--all-sessions` overrides that exclusion.

### Maintain the working set, journal, and lessons

Never edit these files by hand. Pipe content through the `note` wrapper, which scrubs
it before it lands:

```
echo '<markdown>' | python "${CLAUDE_PLUGIN_ROOT}/scripts/pcx.py" note --kind working --cwd "<project>"
echo '<markdown>' | python "${CLAUDE_PLUGIN_ROOT}/scripts/pcx.py" note --kind journal --cwd "<project>"
echo '<markdown>' | python "${CLAUDE_PLUGIN_ROOT}/scripts/pcx.py" note --kind lesson  --cwd "<project>"
```

`working` replaces that project's living document in full. `journal` and `lesson`
append to the workspace journal and lessons file, each entry stamped
`(YYYY-MM-DD HH:MM UTC · session <sess8> · <project-key>)`; pass `--sess <sess8>` so
the stamp names your session (the checkpoint directive embeds it). SKILL.md teaches
the agent this discipline; the hook prompts a journal checkpoint every ~20 turns of
each session. Mark a note human-only to keep it out of the store entirely (classified,
never written): add `--human-only` to any `note` command.

### Enrich the mirror (semantic recall)

Deterministic capture finds what you typed. Enrichment finds what you *meant*:

```
python "${CLAUDE_PLUGIN_ROOT}/scripts/enrich.py" --list-pending
echo '{"<chunk-id>": {"topic": ["auth"], "summary": "...", "keywords": ["..."],
  "search_queries": ["..."], "facts": ["..."],
  "decisions": [{"text": "retries use exponential backoff",
                 "conviction": {"value": 0.8, "domain": ["architecture"],
                                "why": "...", "would_weaken_if": ["..."]}}]}}' \
  | python "${CLAUDE_PLUGIN_ROOT}/scripts/enrich.py" --cwd "<project>" --apply-stdin
```

Decisions carry **conviction at creation**, captured once and never overwritten. When
a decision later proves out or fails, record the event and let its earned weight move:

```
python "${CLAUDE_PLUGIN_ROOT}/scripts/calibrate.py" --cwd <project> --target <did> --event confirmed --source test_retry
python "${CLAUDE_PLUGIN_ROOT}/scripts/calibrate.py" --cwd <project> --stats
```

Retrieval then weights each decision by conviction times calibration and shows the
trajectory, never a single collapsed number. `--event` takes `confirmed`, `contradicted`,
or `eroded` (a soft contradiction, weighted half). `--weight` (a float, `1.0` by default)
scales how much one event counts, for when a piece of evidence deserves more or less than
a full vote; non-finite and negative values are clamped rather than trusted.

## Features you should not overlook

- **Refusal list.** Your own words and phrases, redacted to `<redacted>` everywhere
  content reaches the store. Manage it with `parsica-weave refusal` (or `/refusal`):

  ```
  parsica-weave refusal                 how many terms, and a digest per term
  parsica-weave refusal --add TERM      add it (idempotent)
  parsica-weave refusal --remove TERM   remove it, with a readback of what changes
  parsica-weave refusal --project       this project's list instead of the workspace one
  ```

  `parsica-weave human-only` is the same verb over the stronger list. **Neither verb
  ever prints a term** - the list holds the exact words you marked as never-to-appear,
  and printing them would put them on your terminal, in the transcript, and into
  Weave's own chat mirror. The listing shows a count and a per-term digest keyed to
  your workspace, which is enough to count and tell entries apart. To check whether a
  term is listed, run `--add` on it: it is idempotent and says when it is already
  there.

  You can still set the lists by hand in `~/.claude/parsica-weave/config.json` (global)
  or `~/.claude/parsica-weave/projects/<project-key>/config.json` (per-project); the
  file does not need to exist yet or hold anything else: `{"refusal": ["ProjectX"]}`.
  No restart needed - it's picked up on the next hook. Matching is case-insensitive
  substring, so a term is caught even welded inside another word (`media_ProjectX`,
  `ProjectXClient`). Global and per-project lists are unioned. This lets capture keep
  running through sensitive work with the sensitive terms fenced, instead of turning
  continuity off.
- **Scrub before store, by construction.** Credentials (key prefixes, PEM blocks,
  assignment-style secrets) are always redacted; PII (emails, phones, SSNs, cards) by
  default. Every writer reaches disk through a single gateway in `store.py`, enforced
  by tests, so no code path can skip the scrub. Git SHAs, file hashes, and version
  strings deliberately survive.
- **Compaction survival.** The PreCompact hook flushes complete turns before context
  compaction; SessionStart re-injects the working set, recent index, lessons, and
  journal on the other side. The agent wakes up already oriented. Despite compaction, your
  data is never at risk of being lost: even if a piece of pre-compaction context is not
  re-injected, it's still permanently stored and queryable through `query.py` - the
  auto-injected slice is deliberately budget-capped, but the full record behind it never is.
  Context is maintained elsewhere, permanently.
- **Provenance on every shared write.** Journal and lesson entries carry a stamp with
  time (UTC), session id, and project key, so the shared docs always show where each
  entry came from.
- **Retention rotates, never deletes.** The mirror keeps the most recent N days *with
  activity* live (default 14); the journal keeps a 7-day active window. Older files
  move to `archive/` and stay there. Nothing in Weave unlinks user data; lessons and
  working sets are permanent.
- **Concurrency-safe where it counts.** Mirror, enrichment, and calibration write
  per-process shards; two Claude Code windows on one project cannot drop each
  other's records. The human-facing note files (working set, lessons, journal) are
  single-writer surfaces: replaced atomically, appended best-effort.
- **Grep-pairing (on by default).** Grep is one of the most reliable tools an agent reaches
  for when orienting in a codebase. When the agent greps the codebase, a `PostToolUse`
  hook queries the store for the same term and injects the top hits - so the agent's most
  reliable orientation reflex also searches its own memory. Needs the `PostToolUse` hook from
  the install step; tuned by the `pairing_*` config keys (`pairing_enabled: false` turns it off).
- **Heartbeat + snap-back re-grounding.** On a long session, identity plus a working-set slice
  are re-injected every `heartbeat_every_n` turns; after a heavy sub-agent return the next turn
  re-grounds off-cadence. Both slices are small by design, so context never bloats.
- **Fail-silent hooks.** A continuity helper never breaks a session. That silence is the
  passive hook path only: setup (`doctor`) and the explicit verbs (`note`, `promote`) are
  loud, and exit non-zero on failure.

## Channels (scope sessions into named lanes)

A channel is a named lane of sessions - one per surface or long-running workstream
(a "release" terminal, a "research" desktop window). The registry is workspace-level
(`channels.json`), so one lane can span projects. Channels get an **immutable
assigned id** at creation: the name is a human label you can rename at will, and
history keys on the id, so a rename never orphans a chunk.

- **Tag capture:** set `PARSICA_CHANNEL=release` in the profile that launches a
  surface, and every chunk that session mirrors carries the channel. Unset means
  untagged, fully compatible.
- **Filter recall:** `query.py --channel <name>` scopes a query to one lane.
- **Catch up deliberately:** `python "${CLAUDE_PLUGIN_ROOT}/scripts/pcx.py" sync --hours 12` prints a compact
  digest of the last N hours across the whole workspace, every project and every other lane,
  each entry labeled `[<project-key> · <channel-or-sess>]`. Cross-lane context is a
  choice you make, not a default you inherit.
- **Manage the registry:** `python "${CLAUDE_PLUGIN_ROOT}/scripts/pcx.py" channel --list | --name <name> |
  --rename <old> <new>`.

Channels scope attention, not identity: the working set, journal, and lessons stay
shared - they are the deliberate coordination surfaces between lanes. For a session
that must leave no trace at all, use isolation instead:

- **Session isolation:** `PARSICA_ISOLATED=1` (set per launch profile) gives a session
  full recall and zero footprint. It reads everything (boot inject, queries, and grep
  pairing all work, under a loud `[isolated session - zero footprint]` banner) and
  writes nothing durable: no capture, no cursor movement, no turn counting, no
  rotation. `note` and `promote` refuse with a clear message.

## Configuration

Copy `config.example.json` (in the plugin root) to `~/.claude/parsica-weave/config.json` for a
global override, or to `~/.claude/parsica-weave/projects/<project-key>/config.json` to override
just one project - then edit only the keys you want to change. Neither file needs to exist until
you want to change something; every key already has a safe default, and a config edit is picked up
on the next hook, no restart needed. What each key does:

- `agent_name` - Your agent's display name. Set with `/name` or the `name` verb - see "Naming
  your agent." Never a store key, so renaming it never touches your data.
- `byline` - Once named, the agent opens every reply with `[Name · session-tag]` (prompt-layer:
  honored by the agent, not enforced by a gate). Set `false` to turn it off entirely.
- `byline_session_tag` - Set `false` to keep the warm, name-only byline instead of the session tag.
- `mirror_active_days` - The duration your context mirror stays active before being moved to
  archive.
- `journal_active_days` - Your journal is rotating, and by default, the day 7 page ends up being
  moved to archive to make room for the new day 1 page. Timing is configurable.
- `checkpoint_every_n` - Your journal is maintained by adding a summary of the last n
  conversational turns. By default, a summary is written (per session) every 20. Configurable.
- `scrub_pii` - PII such as keys, identifying information, secrets, etc, is scrubbed by default.
  Toggle this on or off.
- `max_inject_bytes` - The amount of characters injected at session start, if you want it lower
  than the platform ceiling. That ceiling is 9,500 characters - a value above it, including the
  `12000` shipped as the default, never actually takes effect; only lowering this number changes
  anything.
- `mirror_user_chars` / `mirror_asst_chars` - Per-turn character cap on what gets captured from
  your own messages and the agent's replies (higher for replies, since they tend to run longer).
- `mirror_index_chunks` - How many recent chunks the boot inject lists at session start.
- `promote_reminder` - Nudges you at boot when items are waiting to promote to Spirit's durable
  pool. Only appears if Spirit is installed; set `false` for a quieter start.
- `refusal` - Enter your chosen refusal terms in this list for them to be included and refused as
  designed. Managed by `parsica-weave refusal` / the `/refusal` slash command - see
  "Features you should not overlook."
- `human_only_terms` - Same idea as `refusal`, but stronger: a match means the content is never
  stored at all, not just redacted. Managed by `parsica-weave human-only` / `/human-only`.
- `pairing_enabled` - When the agent greps your code, it also searches its own memory for the
  same term and surfaces hits. On by default.
- `pairing_threshold` - How good a match needs to be before it's surfaced during grep-pairing.
  `1.0` means any match counts.
- `pairing_max_chunks` - Caps how many memory hits ride along with one grep-pairing injection.
- `pairing_session_cap` - Caps total grep-pairing injections per session, so a long grep hunt
  doesn't flood your context.
- `pairing_min_term` - Ignores grep searches shorter than this many characters, so tiny or common
  patterns don't trigger noise.
- `heartbeat_every_n` - Every N turns on a long session, your identity and a slice of the working
  set are re-injected so the agent can't drift. `0` turns this off.
- `heartbeat_slice_chars` - How much of the working set rides along on each heartbeat.
- `snapback_tools` - After one of these tools runs (they tend to flood your context), the very
  next turn re-grounds early instead of waiting for the next heartbeat.
- `native_memory_import` - Mirrors your own `CLAUDE.md`/`MEMORY.md` files into Spirit's durable
  pool at boot, when Spirit is installed. Read-only on your files either way - see "Pairs with
  Parsica Spirit."

Environment variables. These six are the only ones the scripts read; a test
(`scripts/test_env_documented.py`) fails if a script reads one this list does not name.

- `PARSICA_WEAVE=off` (or `0`/`false`/`no`) disables capture and rehydration for a session
  without uninstalling. The hooks fire in every project once installed, so this is the
  per-session kill switch.
- `PARSICA_ISOLATED=1` runs a zero-footprint session: full recall, no durable writes (see
  Channels).
- `PARSICA_WEAVE_ROOT` relocates the workspace (default `~/.claude/parsica-weave`).
- `PARSICA_SPIRIT_ROOT` relocates the Spirit store root that `promote`, the native-memory
  import and federated queries read and write (default `~/.claude/parsica-spirit`); the
  workspace id is appended to it.
- `PARSICA_CHANNEL` (set per launch profile) names the session's channel.
- `PARSICA_ROUTER=off` (or `0`/`false`/`no`) turns off Spirit federation for a session: queries
  return Weave's own results and nothing else, byte-identical to a Weave-only install. It is the
  switch to reach for when you want to isolate whether the durable tier is involved in a result.

The human-only fence is the built-in `[[human-only]]` sentinel and the configured
`human_only_terms`, and nothing else; no module is loaded from the environment. There is no
switch that opens a file by path: enrichment JSON arrives on stdin or inline.

## Data, retention, and uninstall

Everything Weave captures is stored **locally**. The store is a single workspace at
`~/.claude/parsica-weave/`, and the plugin's own code opens no outbound network connections and
sends no telemetry. Weave is a Claude Code tool, not an air-gapped vault, though: text
injected by hooks becomes Claude Code context, and optional enrichment workers process staged
chunks through the configured Claude deployment, so that text may be included in model requests
according to your Claude Code and Anthropic settings.
Captured chat is scrubbed for secrets and PII **before** it is written, at the single store-write
seam every writer crosses.

Retention never deletes. When mirror shards or journal pages age past their active
window they **move** to an `archive/` subdirectory; no code path in Weave unlinks user
data. The files are yours, plain text, and visible; archives are permanent until you
remove them yourself.

- **Pause it:** `PARSICA_WEAVE=off` disables capture and rehydration for a session without
  uninstalling. `PARSICA_ISOLATED=1` keeps recall and writes nothing durable.
- **Delete one project's history:** remove `mirror/<project-key>/` and
  `projects/<project-key>/`, plus that project's file under `working/`. The shared
  journal and lessons keep that project's entries; each one is stamped with its project
  key, so you can find and excise them with a grep.
- **Uninstall:** disable or remove the `parsica-weave` plugin (the hooks stop firing everywhere),
  then delete `~/.claude/parsica-weave/` to erase the workspace.

**Upgrading from the per-project layout:** run
`python "${CLAUDE_PLUGIN_ROOT}/scripts/pcx.py" migrate` once. It re-homes each legacy
`<project-key>/` silo into the workspace tree, appends its lessons to the shared docs each
stamped `*(migrated · <key>)*` and its journal pages under a `### migrated from <key>`
header, merges its channel registry, and prints a manifest of every move. Nothing is
deleted: the legacy directory stays in place with a `.migrated-to-workspace` marker until
you remove it yourself. Safe to re-run; already-migrated silos are skipped.

Two things it refuses rather than migrates, each named in the manifest on a `REFUSED` line:
a silo whose directory is a link (junction or symlink), or that does not resolve inside the
store, is never read at all; and a `working.md` whose content trips the human-only fence is
left where it is and never indexed. A silently skipped silo is indistinguishable from one
that was never there, so migrate says which it was.

**Running the scripts:** the `python "${CLAUDE_PLUGIN_ROOT}/scripts/..."` commands above rely on
the `CLAUDE_PLUGIN_ROOT` that Claude Code sets for the plugin. To run one yourself outside a hook,
substitute the plugin's install path for `${CLAUDE_PLUGIN_ROOT}`, and use whichever interpreter name
your system has - `python` or `python3` (the hooks try `python3`, then `python`, then `py -3`).

## Pairs with Parsica Spirit

Weave is complete on its own. **Parsica Spirit** is the optional memory engine from
the same family: sharded storage, 11-layer BM25+ retrieval, and a model-free
accumulating substrate (entity graph, co-occurrence, quality signals) that compounds
with use. Weave is the capture and continuity layer; Spirit is the deep-memory tier
for applications that want programmatic, engine-grade recall. See the Parsica Spirit
repository (`Antaris-Analytics-LLC/parsica-spirit`) for installation; it expects Weave
(or an equivalent capture layer) to be feeding it in Claude Code use.

`promote` is the on-ramp. It is an explicit verb, never wired to a hook, and it is the
only thing that writes to the durable pool:

```
python "${CLAUDE_PLUGIN_ROOT}/scripts/pcx.py" promote --dry-run
python "${CLAUDE_PLUGIN_ROOT}/scripts/pcx.py" promote
python "${CLAUDE_PLUGIN_ROOT}/scripts/pcx.py" promote --verify
python "${CLAUDE_PLUGIN_ROOT}/scripts/pcx.py" promote --migrate-identity
```

Every run prints its plan first - the scope, the source project, a line per action naming
what it is and an excerpt of its text, and the candidate counts - before anything is
written. `--dry-run` prints exactly the same per-item list the real run does, so what you
review is what lands; it stops there and performs no writes at all, not even creating a
pool that does not exist yet. Nothing is elided: a long plan prints long. `--verify` compares the
store against the sync ledger, printing a `DRIFT:` line per mismatch and exiting `3` when
it finds any. `--migrate-identity` is the one-time, additive, receipted re-key of a
pre-release store; back the store up first. `--cwd` picks the project directory and
`--sess` tags the promoting session.

It refuses rather than half-acting: exit `3` under `PARSICA_ISOLATED=1` (a zero-footprint
session promotes nothing), exit `2` if Spirit is not installed, and exit `4` when a
preflight blocks the run - a store still on the pre-release identity scheme, a salt
fingerprint that does not match the ledger, or a store written by a newer Spirit than the
one installed. If a salt has been restored from backup, check it with `verify_salt.py`
before promoting: it prints the fingerprint of a salt file, and given the `salt_fp` from
the store's `promotion_sync.json` it exits `0` on a match and `1` on a mismatch.

**Native memory.** With Spirit installed and a durable pool in place (created by your
first `promote`), each session start also mirrors the agent's own memory files -
project `CLAUDE.md` / `CLAUDE.local.md`, the user-level `CLAUDE.md`, and auto-memory
`MEMORY.md` + topic files - into the pool. The import is read-only on the source files
(they are never written, and uninstalling changes nothing about them), idempotent per
section, and skipped under isolation. Imported sections form the **native permanence
class**: nothing in the pool decays them, retrieval ranks them boosted-but-beatable (a
fixed weight an operator can read in the source - a dead-on session hit still wins),
and every hit identifies itself as `claude-native/<scope>` with its heading trail.
Duplicates collapse at display time only when they are truly the same: a
near-duplicate is folded (and the warm chunk quoting it wins the slot) only when one
text is a contiguous quote of the other and the extra context carries no negator and
no digit. A reordering, a one-word substitution, or a copy whose surrounding text
differs by a negation or a number keeps both on screen, by design. A contradiction is
never hidden. Weave's fences apply at import: `[[human-only]]` and `human_only_terms`
sections are never imported, an already-imported copy is retired by id when the fence
tightens, and the workspace refusal union is redacted from every imported section.
`native_memory_import: false` turns the boot import off.

## License

Apache-2.0. See `LICENSE` and `NOTICE`.
