# Autonomous Enrichment (Parsica Weave for Claude Code)

Enrichment is one of Parsica's largest recall levers: a doc2query overlay (topic, summary,
keywords, search queries, facts) on every captured memory chunk, so a fact stored one way stays
retrievable the way you later search for it. The catch has always been operational. Producing
those overlays by hand at every checkpoint is heavy, so in practice the lever goes unpulled.

On Claude Code we close that gap with the substrate's own capability: a background agent swarm.

## What happens

At each memory checkpoint, instead of asking the agent to hand-author overlays, Weave spawns a
background **enrichment swarm** and the agent goes back to work. The swarm:

1. **Scans** the pending chunks (everything captured since the last enrichment pass).
2. **Fans out** one cheap worker per chunk, in parallel, each writing a terse overlay.
3. **Applies** the overlays through the store's write gateway, which scrubs every field and
   honors the human-only fence on the way in.

It runs entirely off the main loop and reports when finished. The agent never hand-cranks an
overlay again; the lever pulls itself, every cadence.

## Why cheap and terse

Workers run on a small, fast model (Haiku, or any comparably cheap, fast model; the difference
at the top is marginal). This is deliberate, and it is about quality, not
cost: larger models overcomplicate automatic enrichment and add one or two extra words per
field, and that word-bloat pollutes recall as the store grows. Terse wins. The worker prompt
enforces shortest-phrasing, and a decision is recorded only when a real decision was made.
Reserve larger models for enriching dense knowledge packs, where the extra surface is genuinely
useful.

## Components

- `scripts/enrich_prep.py` - scan stage. Lists pending chunks, drops pure-noise turns, stages
  each as a text file to a per-project inbox. Prints `{ids, count, inbox}`.
- `scripts/enrich_apply.py` - apply stage. Reads the workers' overlay files, validates each,
  applies them through the gateway. Prints `{applied, malformed}`.
- `scripts/enrich-swarm.workflow.js` - the workflow that runs scan, the Haiku fan-out, and apply
  as one self-contained invocation.
- The checkpoint hook (`pcx.py`, `handle_prompt`) spawns the workflow when a checkpoint is due.

## Setup

If Weave's hooks are installed per the README, there is nothing extra to wire. The
checkpoint directive that arrives roughly every 20 turns tells the agent to spawn the
workflow and hands it the correct paths:

```
Workflow({scriptPath: "<scripts>/enrich-swarm.workflow.js",
          args: {scripts: "<scripts>", cwd: "<project>"}})
```

The agent runs that call verbatim and returns to work; the swarm reports when done.

Requirements:

- A Claude Code with the Workflow tool available (background agent orchestration).
- The Weave hooks installed, so checkpoints fire and chunks are being captured.

The `args` the directive passes always reflect the live install location and project,
so a moved or renamed plugin folder keeps working. A manual run has no editable fallback
constants: you must pass `args: {scripts: "<abs path to parsica-weave/scripts>", cwd: "<abs
project dir>"}` on the Workflow invocation, or the workflow throws with a clear message.

## Verification

Three checks, cheapest first:

1. **Pending stays small.** `python "${CLAUDE_PLUGIN_ROOT}/scripts/enrich.py" --cwd "<project>" --list-pending`
   right after a checkpoint should show the backlog draining to zero within a few
   minutes. A list that only grows means the swarm is not firing.
2. **Overlays are landing.** `python "${CLAUDE_PLUGIN_ROOT}/scripts/query.py" --grep "<recent topic>" --full`
   should show `topic=`, categories, and search queries on recent chunks.
3. **The swarm ran.** The workflow appears in `/workflows` (Scan, Enrich, Apply
   phases). The apply stage prints `{applied, malformed}`; malformed overlays are
   dropped by the gateway, never stored raw.

If a fence or scrub guard drops a chunk mid-swarm, that is the gateway working as
designed: the swarm's output passes the same scrub-before-store wall as every other
writer.

## Notes

- Cost is not the reason for the small model; recall quality is. Providers are interchangeable.
- Overlays are stored locally as plain text, in the workspace store under
  `projects/<project-key>/enrich/`. No embeddings or model artifacts are written to the durable
  record - the store stays model-free; the overlay is the readable semantic layer, not a vector.
  Generating those overlays is not air-gapped, though: the enrichment workers process staged
  chunks through the configured Claude deployment, so that text may be included in model requests
  according to your Claude Code and Anthropic settings. Weave's own code opens no outbound network
  connections and sends no telemetry.
