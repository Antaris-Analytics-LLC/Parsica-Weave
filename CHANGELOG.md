# Changelog

All notable changes to Parsica Weave are documented in this file.
This project follows [Semantic Versioning](https://semver.org).

## [1.0.1] - 2026-07-30

Every fix below shipped with tests proving both directions - the failure refused and the
legitimate case still passing.

### Fixed

- **Stop-hook capture death.** The capture dedup pre-scan enumerated the mirror directory
  many times per Stop and parsed every active shard, so a store past roughly 1,400 shards
  blew the hook timeout and capture stopped permanently and silently. Dedup now reads a
  seen-id index in `.state.json` (maintained at the append site, unioned under the state
  lock, fail-open at every edge): measured at 1,500 shards, the pre-scan went from 11.5s
  to 0.9s cold and 0.012s warm, with zero directory walks on the warm path.
- **A config typo can no longer wipe your lists.** Rewriting a config file whose existing
  JSON does not parse used to publish an empty config over it - refusal and human-only
  lists included. A rewrite of an unreadable file now refuses loudly and writes nothing;
  an absent file still starts empty.
- **A torn write no longer destroys the next record.** A mirror append that died mid-line
  left a partial record with no trailing newline, and the next append glued onto it, so
  neither parsed. The appender now isolates a torn tail on its own line; the fragment
  stays lost to its own fault, the new record survives.
- **The working set cannot be emptied by accident.** Replacing a working set with
  empty/whitespace content is refused, and every replace leaves the previous version
  beside it as `.prev`.
- **A lock that silently is not a lock now says so.** A junction or non-regular file
  squatting on a `.lock` sidecar degrades locking exactly like a transient timeout, so it
  was invisible. `doctor` now discovers every lock sidecar under the store and names the
  unhealthy ones.
- **Staged enrichment cannot strand.** The enrichment inbox is swept for staged-but-
  unapplied overlay files, so a killed apply step no longer loses paid-for model work.
- **The calibration loop's front door.** `query.py --full` now prints a decision's `did`,
  which the documented calibrate workflow needs and previously never showed.
- **Honest clocks in query output.** A durable hit's header renders `occurred` at the
  precision its `clock_source` supports - a synthetic date never converts to a fake
  wall-clock time, and pool time is labeled as pool time.
- The append gateway now refuses a redirect by its own containment clause on every
  platform. The check sat inside `except FileExistsError:`, but a Windows directory
  junction fails the exclusive create with EACCES, so the clause was never reached and
  the OS was holding the property instead. Nothing was exploitable either way; the guard
  now refuses for the reason it names, and the test asserts the refusal sentence rather
  than that something raised.

### Added

- `refusal` and `human-only` verbs (and `/refusal`, `/human-only`) manage the two policy
  term lists, keeping the v1.0 README's promise. Neither ever prints a term: the listing
  gives a count and a per-term digest keyed to a workspace-local secret, because a plain
  hash of a short codename falls to a wordlist and a transcript full of those would be
  nearly a transcript full of terms. `--add` is idempotent, which is how you check for a
  term without recording it; `--remove` says plainly what it loosens and what it does not
  restore.
- `promote --dry-run` now names every item it would promote, with an excerpt, instead of
  reporting counts only. It prints the same per-item list the real run does, so reviewing
  the preview is reviewing what lands. Nothing is capped.

### Changed

- The identity verb is now `name` (`parsica-weave name --name Aria`, `name --reset`),
  matching the `/name` slash command it has always paired with. `agent` was too broad a
  word for a verb that only ever touched a display label. The old spelling still
  dispatches and always will.
- README: a scannable top - one-paragraph pitch, an at-a-glance feature list, a
  three-command quick start, and a table of contents. Two stale "CLI coming soon" notes
  now point at the verbs this release ships.

## [1.0.0] - 2026-07-21

Initial public release.

Parsica Weave is workspace continuity for Claude Code - hooks plus scripts (standard-library
Python, no pip dependencies) that make one continuous agent across every project and session,
with memory that survives compaction, new sessions, and restarts.

- One workspace store at `~/.claude/parsica-weave/`: a workspace-level journal and lessons file
  (every appended entry stamped with time, session, and project), a working set per project
  under `working/`, a scrubbed and enriched chat mirror per project under `mirror/`, and machine
  state under `projects/`. Projects key on the git root, so every subdirectory of a repo
  resolves to the same project.
- Retrieval-first recall: `query.py` filters by grep/file/command/tool/time; `--workspace`
  searches every project's mirror and `--archive` folds in rotated shards. SessionStart injects
  the working set, lessons, journal, recent mirror index, and - when `boot_recent_elsewhere=true`
  (off by default) - a "recent elsewhere" view of other sessions across the workspace.
- Retention rotates, never deletes: mirror shards and journal pages move to `archive/` after
  their active window; no code path unlinks user data.
- Native memory (with Parsica Spirit installed): session start mirrors the agent's own memory
  files (`CLAUDE.md` / `CLAUDE.local.md` / user `CLAUDE.md` / auto-memory `MEMORY.md` + topics)
  into the durable pool - read-only on the sources, idempotent, isolation-skipped, joins an
  existing pool only. Imported sections rank as the native permanence class
  (boosted-but-beatable fixed weight, pinned by test), identify themselves at retrieval
  (`claude-native/<scope>` + heading trail), and duplicates collapse at display time - exact
  copies always, near-duplicates only when they do not differ by a negation or a number - so
  the same content does not print twice while a contradiction is never hidden.
  `native_memory_import: false` turns it off.
- Agent identity: the `agent` verb names the workspace's agent and manages its byline
  (`agent --name <name>`, `--byline on|name-only|off`, `--reset`; show with bare `agent`).
  One name per workspace; a display label only, never a store key, so renaming never touches
  data. When a name is set the agent opens every reply with `[Name · <session-tag>]`.
- Secret and PII scrub before disk; a `[[human-only]]` do-not-index fence plus configured
  `human_only_terms`; workspace-level named channels with cross-project `sync`.
- Session controls: `PARSICA_WEAVE=off` disables capture and rehydration for a session without
  uninstalling; `PARSICA_ISOLATED=1` runs a zero-footprint session (full recall, no durable
  writes). Turn cadence and checkpoints run per session.
- `pcx.py migrate` re-homes pre-release per-project silos into the workspace, preserving
  everything and printing a manifest; the legacy directory is left in place for the user to
  remove.

### Notes

- Weave stores locally and calls no external APIs, but it is a Claude Code extension:
  hook-injected recall and the optional enrichment workflow send text to the model according to
  your Claude Code and Anthropic settings. Use `[[human-only]]`, refusal terms, and project policy
  for anything that must never be re-injected or enriched.
- The richer term-based human-only classifier is an optional external plugin
  (`PARSICA_HUMAN_ONLY_CLASSIFIER`); the substring floor and the sentinel ship in-box.
