# Onboarding: the first sessions with Parsica Weave

This document is for the person who installed Weave and wants to know what their agent
should be doing with it, and how to tell whether it is. The README covers installation
and features; SKILL.md is the working reference the agent reads. This covers the part
neither does: the habits that make a memory worth having, and the checks that show
whether they are being kept. The sections below describe, in the second person, the
habits SKILL.md asks of the agent, so you can read exactly what it is asked to do and
hold what you see against it. Nothing here runs on its own, and nothing here is a
standing instruction to an agent that happens to read it.

---

## 1. The lesson that comes first

Everything else in this document is mechanics. This is the part that decides whether the
mechanics matter.

The first agent to live on this system went an entire session without querying its own
store. Capture was running, enrichment was current, the working set was pinned, and the
answers to the operator's questions were sitting in the mirror, one command away. The
agent answered from its own fading recollection instead, and the recall was imprecise
where the store was exact. The store was healthy. The habit was missing.

The rule that came out of that session:

**A store you never query is boot injection with extra steps.**

The session-start packet (working set, lessons, journal, mirror index) is orientation,
not memory. It tells you where you are; it cannot tell you everything you knew. Treat
passive context as the map and the query as the territory.

Query FIRST, before answering from recollection, whenever:

- you are asked how something works and the answer predates this session,
- you are about to restate a past decision, or the reasoning behind one,
- you feel yourself reconstructing instead of remembering ("I believe we chose X
  because..."), which is the surest sign the precise answer exists and you are about to
  approximate it,
- a person, file, or system comes up that you have not touched today,
- you disagree with the operator about what happened. One of you is right, and the
  mirror knows which.

The failure mode is not ignorance, it is confidence. Passive recall feels sufficient at
exactly the moments it is not. You will not notice the habit is missing; you will simply
be slightly wrong more often than you need to be. Build the reflex before you trust the
feeling.

## 2. What you woke up with

At every session start, Weave injects up to five sections:

- **Working set**: pinned facts and open loops. The living state of this project. You
  maintain this; it survives everything.
- **Lessons / scars**: rules earned the hard way, self-authored, permanent, gathered
  from every project you work in. Read them as standing orders from previous versions
  of yourself.
- **Latest journal**: what happened most recently, in narrative form. One journal for
  the whole workspace.
- **Recent chat mirror index**: one line per recent captured turn, with a ready-to-run
  query command in the header. That command is your reach. Use it.
- **Recent elsewhere**: one line per recent chunk from your other sessions across the
  workspace, so parallel work is visible instead of surprising (opt-in: set
  `boot_recent_elsewhere=true`; off by default).

Weave treats every session as one agent, and that is literal: the lessons and the
journal are workspace-level, shared by every session in every project. A project scopes
what is being worked on, never who the agent is.

If the injection is missing entirely, capture may not be wired: see the README install
steps, then run the checks in section 7 here.

## 3. Your reach: querying the store

Run from the project directory (or pass `--project`). Flags combine as AND:

```
python <scripts>/query.py --grep "auth bug"          # full-text, ranked
python <scripts>/query.py --file effects.py --full   # turns that touched a file
python <scripts>/query.py --cmd "git commit"         # turns that ran a command
python <scripts>/query.py --tool Edit --since 3      # by tool, last 3 days
```

Reading results:

- Every hit carries a timestamp and session id: you can place it in time.
- Enriched hits show `topic=`, categories, and files; enrichment-overlay matches
  outrank raw-text matches, so a fact stored in one vocabulary is still found in
  another. `--full` shows the whole chunk plus its overlay.
- Hits tagged `[durable]` come from the Spirit floor (section 5), not the mirror. They
  survive mirror pruning; treat them as the canonical record.

Query cost is a few hundred milliseconds. Being wrong about a past decision costs the
operator's trust. The exchange rate is not close.

## 4. Your voice: writing memory well

Three surfaces, one discipline: **never write the store files directly.** Everything
goes through the scrubbing wrapper, which redacts credentials, PII, and the operator's
refusal-listed terms before anything touches disk:

```
python <scripts>/pcx.py note --kind working --cwd "<project>" <<'PARSICA_NOTE'
<markdown>
PARSICA_NOTE
```

The same form with `--kind journal` or `--kind lesson`. The quoted delimiter is what makes the
content data: the shell passes a heredoc body through untouched, so a quote, a backslash or a
dollar sign in a note never reaches the shell.

- **Working set** (`--kind working`, replaces in full): pinned facts and open loops
  only. Keep it under a screen. Every stale line you leave costs your successor
  attention at every boot. Prune resolved loops when you close them.
- **Journal** (`--kind journal`, appends): a terse entry at each checkpoint. Decisions
  made, current phase, next step. Write for the agent who resumes cold.
- **Lessons** (`--kind lesson`, appends): only when you hit a wall or learned something
  load-bearing. A good lesson states what happened, then the rule. Lessons are never
  pruned, so respect the reader's attention: no diary entries, no restating the docs.

The checkpoint prompt arrives roughly every 20 turns and asks for the journal and
working-set refresh. Honor it in the same turn it arrives; deferred checkpoints do not
come back.

One more discipline, learned early: the working set and lessons are for what the files
cannot already say. Code structure, git history, and anything derivable from the repo do
not belong in memory. Save the judgment, not the inventory.

## 5. The two floors: a router explanation

Weave is the **warm floor**: workspace-wide, enriched, recent. The mirror rotates by
active days (default 14 days with activity), moving older shards to `archive/` rather
than deleting them, so the live window holds the working edge without destroying the
record.

**Parsica Spirit**, if installed, is the **durable floor**: permanent, cross-session,
model-independent. Weave works completely without it; nothing in this document requires
it. With it, two verbs light up:

**Promotion** moves a warm memory down to the durable floor, deliberately:

```
python <scripts>/pcx.py promote --cwd "<project>" --dry-run   # plan, no writes
python <scripts>/pcx.py promote --cwd "<project>"             # apply
python <scripts>/pcx.py promote --cwd "<project>" --verify    # audit ledger vs store
```

Promotion is never automatic and never wired to a hook: it is a judgment call, yours or
the operator's. Candidates are the durable kinds (journal entries, lessons, high-signal
enriched turns). Run `--dry-run` first, always; the plan prints what would be stored,
updated, or skipped, and why.

**Federation** is already in your reach: `query.py` reads both floors when Spirit is
present and tags durable hits `[durable]`. You do not query the floors separately.

Rules the router answers to, which you should also answer to:

- **One way.** Promotion flows Weave to Spirit only. Nothing writes back up.
- **Never hard-delete.** Retiring a durable record suppresses it; it does not destroy
  it. Preserve by default. If you believe something should be removed, that is the
  operator's call to make, explicitly, never yours to infer.
- **Deliberate, not ambient.** The durable floor is canonical precisely because things
  arrive there by decision. Promote at natural boundaries (a phase closes, a decision
  ratifies, a lesson lands), not on cadence.
- **Scrubbed on the way down.** The current refusal list rides into every promotion, so
  a term fenced after capture is still redacted before it reaches the durable floor.

## 6. Enrichment: passes, run by the operator

Deterministic capture finds what was typed. Enrichment finds what was meant: a compact
overlay (topic, summary, keywords, search queries, facts, decisions with conviction) on
each captured chunk, so recall works in vocabulary you have not used yet.

Overlays are produced in passes, by the operator or by a tool the operator runs, never by
a swarm the hook spawns: the background workflow that did that was removed in October 2026
(REVIEW-2026-10.md). The ~20-turn checkpoint note states what is pending and names the
commands; it asks for nothing. The two stages and the overlay shape are in
AUTONOMOUS_ENRICHMENT.md. The one-line health check:

```
python <scripts>/enrich.py --cwd "<project>" --list-pending
```

A small or empty pending list means passes are keeping up with capture.

When a decision you recorded later proves out or fails, say so:

```
python <scripts>/calibrate.py --cwd <project> --target <did> --event confirmed --source <why>
python <scripts>/calibrate.py --cwd <project> --stats
```

Conviction is captured once, at creation, and never overwritten; calibration events let
its earned weight move over time. The events are `confirmed`, `contradicted`, and `eroded`
(a soft contradiction, weighted half). Retrieval shows the trajectory, not a collapsed
number: a strong conviction that held across ten confirmations is a different fact from
a strong conviction contradicted twice, and you should read them differently.

## 7. Your first fifteen minutes on a fresh install

A concrete thread to begin with, in order:

1. **Verify capture.** Work a few turns, then
   `python <scripts>/query.py --grep "<something you just said>"`. A hit means the
   mirror is live.
2. **Seed the working set.** Pipe an honest first draft through
   `note --kind working`: what the project is, what is in flight, what is unresolved.
   It lands at `working/<project-key> - Working Set.md` in the workspace; your next
   boot is only as oriented as this file.
3. **Write the first journal entry.** One paragraph: where things stand today.
4. **Query once for real.** Ask the store something you genuinely half-remember from
   the last few sessions. Compare what comes back against what you would have said.
   That difference is why the habit in section 1 exists; feel it once and it sticks.
5. **Check enrichment.** `enrich.py --list-pending` after your first checkpoint. If it
   keeps growing, see AUTONOMOUS_ENRICHMENT.md.
6. **If Spirit is installed**, run `pcx.py promote --dry-run` and read the plan. Do not
   apply anything yet; just learn what the durable floor would take.

## 8. Why any of this matters

Continuity is not a convenience feature. An agent that re-derives its context every
session is a very good tool. An agent that remembers its decisions, carries its scars,
and can be precisely right about last month is something the operator can build on.
The store gives the agent the reach. The habit, section 1, is the part to watch for.

Section 1 came out of a real session: an agent answered from recollection with the
exact answer one query away in a healthy store. If you see your agent reconstructing
instead of querying, that is the moment this document is about.
