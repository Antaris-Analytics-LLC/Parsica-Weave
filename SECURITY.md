# Security policy

## Reporting a vulnerability

Please report suspected vulnerabilities **privately** - do not open a public issue for an
unfixed security problem. Use this repository's **GitHub Security Advisories** ("Report a
vulnerability" under the Security tab) so the report reaches the maintainers privately. We aim to
acknowledge reports within a few business days.

## What Weave does with your data

- **Everything stays local.** The store is a single workspace under `~/.claude/parsica-weave/`:
  shared journal and lessons at the top level, each project's mirror and machine state under
  `mirror/<project-key>/` and `projects/<project-key>/`. Weave has no network listener, opens no
  outbound network connections, and sends no telemetry. It is a Claude Code extension, not an
  air-gapped vault: text injected by hooks becomes Claude Code context, and optional enrichment
  workers process staged chunks through the configured Claude deployment, so that text may be
  included in model requests according to your Claude Code and Anthropic settings.
- **No live cross-session context, by design.** Weave never shares one session's LIVE context
  (its active window, its in-progress work) with another session or project, and there is no
  setting that enables this. What may cross a project boundary is only DURABLE, already-captured,
  already-scrubbed memory: by default on demand through a query, or (opt-in) a small one-time
  boot digest of recent captured chunks. A snapshot of the past, never a window into the present.
  Durable learning is shared; live context is separated.
- **Capture is scrubbed before it is written.** High-confidence secrets and PII are redacted at
  collection, at the single store-write seam every writer crosses, so a raw secret does not reach
  the durable record.
- **Enrichment is local.** Semantic overlays are generated and stored locally as plain text; no
  embeddings or model artifacts are persisted, and no content is sent to any external service by
  Weave itself.
- **Provenance stays in records, never in output.** Stored records may carry local provenance
  labels (project keys, session ids) that you own; every output surface - query display, plan
  headers, session injects, digests - passes the refusal fence before anything is shown, so a
  fenced term riding a label never reaches a screen or transcript.
- **Retention rotates, never deletes.** Files that age past their active window move to
  `archive/` directories inside the workspace; no code path in Weave unlinks user data. Removal
  is always the user's own explicit action.

## Security surface

Weave is a local Claude Code plugin. Its surface is local, not networked:

- **Filesystem containment** - everything lives under one workspace directory; per-project
  material is keyed by git root and its paths are validated for containment under `mirror/` and
  `projects/`.
- **Session isolation** - `PARSICA_ISOLATED=1` gives a session full read access and no durable
  writes: nothing is captured, no state advances, no files rotate.
- **Staging containment** - the enrichment pass stages content into a temporary inbox that is
  refused if it resolves inside the store, if any component of its path is a link or junction, or
  if an existing unmarked directory is non-empty. The inbox must be a directory private to the
  current OS user; it is cleared only of Weave's own staging files, never a caller's directory.
- **Hook commands** - the five continuity hooks run local Python from the plugin directory.

## Threat model

Weave defends against malformed data and pre-existing redirected, linked, shared, or non-regular
objects beneath its managed paths - a hostile project, a legacy artifact, a pre-planted symlink,
junction, or hardlink, or ordinary corrupt input. Every write and read gateway proves the file or
directory it is about to touch, not just the path that named it.

Weave does not claim isolation from another process running as the same OS user with concurrent
write access to Weave's private storage. A process with that access already has enough authority
to read, replace, or corrupt the store directly; renaming a directory mid-operation is not a
stronger position than doing so. Defending against a concurrently-writing same-user process is
tracked as hardening, not a v1.0 boundary - see Known limitations below.

## Known limitations (tracked for v1.0.1)

- **Directory authority is proved by path, not held.** Gateways re-verify the full lexical path of
  what they are about to touch, but between that proof and the operation completing, nothing holds
  the directory open. A concurrent same-user process that renames a managed directory in that
  narrow window (and replaces the old name with a link back to it) can redirect the effect before
  a later check refuses it. Closing this fully means routing every managed-path operation through
  held directory handles (POSIX `dir_fd`-relative opens; Windows native handles without
  `FILE_SHARE_DELETE`) instead of repeated path checks.
- **The enrichment inbox marker is a sentinel, not authenticated ownership.** `.parsica-weave-inbox`
  is a known, fixed filename; on platforms or configurations where the inbox's parent directory is
  not private to the current OS user, another process could pre-create a directory and marker with
  that name before Weave does. The default inbox path requires a user-private parent for this
  reason. A fresh, unpredictable run directory recorded in protected Weave state (rather than a
  static well-known marker) is the planned v1.0.1 replacement.

## Supported versions

Security fixes are provided for the latest released version.
