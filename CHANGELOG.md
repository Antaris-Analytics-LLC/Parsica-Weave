# Changelog

All notable changes to Parsica Weave are documented in this file.
This project follows [Semantic Versioning](https://semver.org).

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
