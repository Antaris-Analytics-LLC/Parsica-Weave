# Enrichment in passes (Parsica Weave for Claude Code)

Enrichment is one of Parsica's largest recall levers: a doc2query overlay (topic, summary,
keywords, search queries, facts) on every captured memory chunk, so a fact stored one way stays
retrievable the way you later search for it.

## What changed in October 2026

Until October 2026 this file described a background enrichment swarm: a workflow script that the
~20-turn hook text told the agent to spawn, which fanned out one model worker per pending chunk
with no cap, hard-coded the model, dropped a failed worker silently, and reported an applied count
relayed by a model rather than read from the process. That workflow was removed in the integrity
pass (REVIEW-2026-10.md, pile one, class (f)). Nothing in this plugin starts workers now, and no
hook text asks the agent to. What remains are two operator-run stages whose counts come from the
process that did the work.

## The two stages

- `scripts/enrich_prep.py --cwd <project> [--inbox <dir>] [--limit N]`: the scan stage. Lists
  pending chunks (captured turns with no overlay yet), drops pure-noise turns, and stages each as a
  text file in a private per-project inbox under the system temp dir. It stages at most 50 chunks
  per run (the pending listing it reads returns at most 50); `--limit N` stages fewer, and the
  next run takes what is still pending. Prints `{ids, count, inbox, inbox_withheld,
  skipped_fenced}` on stdout.
- `scripts/enrich_apply.py --cwd <project> [--inbox <dir>]`: the apply stage. Reads
  `<inbox>/<id>.overlay.json` files, validates each, applies them through the store's write
  gateway (which scrubs every field and honors the human-only fence), sweeps the staged copies
  whose overlays landed, and prints `{applied, malformed, swept}` on stdout. Those numbers are
  counted by the process that did the work, never relayed.

Between the two stages something has to write the overlay files, and that is the operator's
choice: by hand from the staged text, or with any tool you run yourself. An overlay is one JSON
object with the keys `topic`, `summary`, `keywords`, `search_queries`, `facts` and `decisions`;
the manual path in `skills/weave/SKILL.md` and the `enrich.py --apply-stdin` command in the
README carry the same shape without the inbox.

The stdout shapes of both stages are pinned in `scripts/enrich_handshake.schema.json` (strict:
no other keys, and the safety fields are required) and checked by the test suite.

## Verification

1. `python "${CLAUDE_PLUGIN_ROOT}/scripts/enrich.py" --cwd "<project>" --list-pending` shows the
   backlog; it shrinks when overlays are applied and only then.
2. `python "${CLAUDE_PLUGIN_ROOT}/scripts/query.py" --grep "<recent topic>" --full` shows
   `topic=`, categories and search queries on enriched chunks.
3. The apply stage's printed `applied`, `malformed` and `swept` are the record of a pass; a
   malformed overlay is dropped by the gateway, never stored raw.

## Notes

- Overlays are stored locally as plain text, in the workspace store under
  `projects/<project-key>/enrich/`. No embeddings or model artifacts are written to the durable
  record; the overlay is the readable semantic layer, not a vector. Producing overlays is not
  air-gapped if you use a model to write them: the staged text goes wherever you send it. Weave's
  own code opens no outbound network connections and sends no telemetry.
