export const meta = {
  name: 'enrich-swarm',
  description: 'Autonomous background enrichment: scan pending memory chunks, fan out cheap Haiku workers to write terse doc2query overlays, apply them through the store gateway',
  phases: [
    { title: 'Scan', detail: 'stage pending chunks to the inbox' },
    { title: 'Enrich', detail: 'one Haiku worker per chunk', model: 'haiku' },
    { title: 'Apply', detail: 'validate + apply overlays through the store gateway' },
  ],
}

// The checkpoint directive passes {scripts, cwd} via args, so the hook-driven path needs no
// editing and survives a moved or renamed install. A MANUAL run must supply both (absolute paths;
// the worker prompts embed them verbatim) - there are NO placeholder defaults, so a raw run fails
// with a clear message instead of chasing a fake path. See AUTONOMOUS_ENRICHMENT.md.
// args may arrive as an object OR a JSON string depending on the invocation path (the checkpoint
// hook and a manual Workflow call can serialize it differently); accept both so autonomous
// enrichment never silently no-ops on a delivery detail.
let _a = args || {}
if (typeof _a === 'string') { try { _a = JSON.parse(_a) } catch (_e) { _a = {} } }
const SCRIPTS = _a && _a.scripts
const CWD = _a && _a.cwd
if (!SCRIPTS || !CWD) {
  throw new Error(
    "enrich-swarm requires args {scripts, cwd}: scripts = absolute path to parsica-weave/scripts, " +
    "cwd = absolute project directory. The checkpoint hook passes these automatically; supply them " +
    "explicitly for a manual run (see AUTONOMOUS_ENRICHMENT.md).")
}

phase('Scan')
const scan = await agent(
  `Run EXACTLY this command and return the JSON object it prints on stdout, nothing else:

python "${SCRIPTS}/enrich_prep.py" --cwd "${CWD}"

It stages the pending memory chunks to an inbox and prints {"ids":[...],"count":N,"inbox":"<path>","inbox_withheld":bool,"skipped_fenced":N}. Return that object verbatim.`,
  { label: 'scan', phase: 'Scan',
    schema: { type: 'object', additionalProperties: false,
      properties: { ids: { type: 'array', items: { type: 'string' } }, count: { type: 'number' },
        inbox: { type: 'string' }, inbox_withheld: { type: 'boolean' },
        skipped_fenced: { type: 'number' },
        // a REFUSAL or an ERROR is a result and rides the same shape (isolated session, and the
        // _die containment/no-safe-default paths the autonomous run can reach): these fields let
        // each validate instead of dying on a schema error mid-stage.
        refused: { type: 'string' }, note: { type: 'string' }, error: { type: 'string' } },
      // inbox_withheld and skipped_fenced are REQUIRED, not optional: both are SAFETY signals, and
      // an absent field reads as falsy - which would silently skip the stop below, or silently
      // zero the skip count, on any relay that dropped it.
      required: ['ids', 'count', 'inbox', 'inbox_withheld', 'skipped_fenced'] } }
)

// EVERY return path reports skipped_fenced. It was dropped on both early returns, and the
// all-chunks-skipped case landed in the first one - so a run where every chunk was withheld for a
// fenced id reported "nothing pending to enrich", pending 0, which is precisely the silent
// enrichment death this counter exists to make visible. (convergence pass 2)
const SKIPPED = (scan && scan.skipped_fenced) || 0
const skipNote = SKIPPED
  ? ` ${SKIPPED} chunk(s) were WITHHELD from enrichment: their chunk ids carry a workspace refusal ` +
    'term, so staging them would have written the term into a worker inbox path. These chunks stay ' +
    'un-enriched until the refusal term or the affected ids change.'
  : ''

if (!scan || !Array.isArray(scan.ids) || scan.ids.length === 0) {
  if (scan && scan.refused) {
    return { pending: 0, applied: 0, skippedFenced: SKIPPED,
             note: `staging refused (${scan.refused}): ${scan.note || 'no reason given'}.` + skipNote }
  }
  return { pending: 0, applied: 0, skippedFenced: SKIPPED,
           note: (SKIPPED ? 'nothing was enrichable.' : 'nothing pending to enrich') + skipNote }
}
// A withheld inbox means a workspace refusal term coincides with a component of the machine temp
// path: the echoed path is REDACTED (display-only), and embedding it in worker prompts would both
// break the workers and defeat the fence. Stop the fan-out LOUDLY - visible degradation, never a
// silent enrichment death and never a raw term in worker context. (round-4 4a)
if (scan.inbox_withheld) {
  return { pending: scan.count, applied: 0, skippedFenced: SKIPPED,
           note: 'inbox path withheld: a workspace refusal term coincides with the machine temp ' +
                 'path, so the worker fan-out was stopped before any prompt embedded it. Stage ' +
                 'with an explicit fence-clean --inbox, or adjust the refusal term.' + skipNote }
}
const INBOX = scan.inbox

phase('Enrich')
await parallel(scan.ids.map(id => () =>
  agent(
    `Read the file ${INBOX}/${id}.txt - one captured conversation turn from a memory store. Produce a doc2query retrieval overlay and Write it as a single valid JSON object to ${INBOX}/${id}.overlay.json.

TERSE IS THE RULE - this is a retrieval index, not a human summary; extra words pollute recall over time. Shortest phrasing that captures the fact; no hedging, no "the user discussed" framing.

Keys, exactly: "topic" (string, <=8 words), "summary" (string, ONE sentence), "keywords" (array of 4-8 distinctive terms), "search_queries" (array of 2-3 natural questions this turn answers), "facts" (array of 1-3 atomic short clauses), "decisions" (array of {"text": string, "conviction": "high"|"medium"|"low"} ONLY for a real decision/stance, else []).

Write ONLY the JSON object to the file - no markdown fences, no prose. Reply: done`,
    { label: `enrich:${id.slice(0, 8)}`, phase: 'Enrich', model: 'haiku', effort: 'low' }
  ).then(() => id).catch(() => null)
))

phase('Apply')
// no --inbox: apply re-derives the default inbox from --cwd (the same deterministic derivation
// prep staged into), so the path never round-trips through another worker context. (round-4 4a)
const applied = await agent(
  `Run EXACTLY this command and return the JSON object it prints on stdout, nothing else:

python "${SCRIPTS}/enrich_apply.py" --cwd "${CWD}"

It validates and applies the overlays through the store gateway, deletes the staged copies whose enrichment landed, and prints {"applied":N,"malformed":M,"swept":S}. Return that object verbatim.`,
  { label: 'apply', phase: 'Apply',
    // `swept` is part of the handshake because the schema is additionalProperties:false - a key
    // the stage prints but the schema omits fails validation and turns a SUCCESSFUL apply into a
    // stage error, which is the exact failure test_enrich_apply_guard.py was written for.
    schema: { type: 'object', additionalProperties: false,
      properties: { applied: { type: 'number' }, malformed: { type: 'number' },
                    swept: { type: 'number' }, refused: { type: 'string' } },
      required: ['applied', 'malformed'] } }
)

return { pending: scan.count, applied: applied && applied.applied || 0,
         malformed: applied && applied.malformed || 0,
         skippedFenced: SKIPPED,
         note: skipNote.trim() || undefined }
