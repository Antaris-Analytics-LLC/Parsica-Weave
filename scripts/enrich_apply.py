#!/usr/bin/env python3
"""Autonomous enrichment swarm - APPLY stage.

Reads the workers' overlay files (<inbox>/<id>.overlay.json), validates each (tolerating a
stray markdown fence or trailing prose), assembles {chunk_id: overlay}, and applies them
through the store's enrichment gateway - which scrubs every field and honors the human_only
fence on the way in. Prints {"applied": N, "malformed": M} as JSON on stdout.
The inbox defaults to the SAME deterministic per-project path enrich_prep stages into
(re-derived from --cwd), so the path never has to transit worker context.

Stage 3 of the `enrich-swarm` workflow.
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path

_JSON_OBJ = re.compile(r"\{.*\}", re.DOTALL)


def _load_overlay(path, root):
    import store
    text = store._read_text_no_follow(path, root).strip()
    m = _JSON_OBJ.search(text)          # tolerate ```json fences / trailing prose from a worker
    if m:
        text = m.group(0)
    return json.loads(text)


def main():
    ap = argparse.ArgumentParser(prog="enrich_apply.py")
    ap.add_argument("--cwd", required=True)
    ap.add_argument("--inbox", default=None,
                    help="override the inbox dir (default: re-derived from --cwd via the same "
                         "deterministic default enrich_prep stages into)")
    args = ap.parse_args()

    here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, here)
    import store
    import enrich
    import enrich_prep

    # --inbox is OPTIONAL (round-4 4a): the default inbox is re-derived from --cwd through the
    # SAME deterministic derivation enrich_prep staged into (default_inbox), so the real path
    # never needs to round-trip through worker context - which is what lets the prep stage fence
    # its echoed path. An explicit --inbox is honoured verbatim (an operator's choice is never
    # silently rewritten); a stale caller that pipes back a REDACTED echo simply globs an empty
    # dir and applies 0 - degraded, visible in the counts, and never a leak.
    inbox = args.inbox
    if not inbox:
        try:
            inbox = enrich_prep.default_inbox(args.cwd)
        except enrich_prep.NoSafeDefaultInbox:
            # default_inbox refuses (exit 2) only on a pathological refusal vocabulary where no
            # machine slug can be fence-clean - prep would have refused identically, so nothing
            # was ever staged. Report THIS stage's own result shape instead of leaking prep's
            # error shape out of the apply stage (the caller parses {"applied","malformed"}).
            print(json.dumps({"applied": 0, "malformed": 0,
                              "refused": "no fence-clean default inbox for this project; "
                                         "pass an explicit --inbox"}))
            sys.exit(3)

    # applying overlays is a durable write: an isolated (zero-footprint) session refuses BEFORE
    # reading any overlay from disk. Machine-parseable refusal on stdout, exit 3 (matches the
    # other standalone writers).
    if store.is_isolated():
        print(json.dumps({"applied": 0, "malformed": 0, "refused": "PARSICA_ISOLATED=1"}))
        sys.exit(3)

    # Apply is a separate process from prep, so re-prove the inbox rather than trusting the
    # prior stage's result. A Windows junction reports os.path.islink() False; use the shared
    # tag-aware predicate and require a real, single-link ownership marker.
    store_root = os.path.realpath(str(store.STORE_ROOT))
    problem = enrich_prep._inbox_problem(
        inbox, store_root, allow_missing=False, require_marker=False)
    if problem:
        print(json.dumps({"applied": 0, "malformed": 0, "refused": problem}))
        sys.exit(3)
    marker = os.path.join(inbox, ".parsica-weave-inbox")
    inbox_path = Path(os.path.abspath(inbox))
    inbox_parent = inbox_path.parent
    entries = list(store._safe_iterdir(inbox_path, inbox_parent))
    problem = enrich_prep._inbox_problem(
        str(inbox_path), store_root, allow_missing=False, require_marker=False)
    if problem:
        print(json.dumps({"applied": 0, "malformed": 0, "refused": problem}))
        sys.exit(3)
    overlay_entries = [f for f in entries if f.name.endswith(".overlay.json")]
    if os.path.lexists(marker):
        problem = enrich_prep._inbox_problem(
            inbox, store_root, allow_missing=False, require_marker=True)
        if problem:
            print(json.dumps({"applied": 0, "malformed": 0, "refused": problem}))
            sys.exit(3)
    elif overlay_entries:
        print(json.dumps({"applied": 0, "malformed": 0,
                          "refused": "inbox with overlays is not marked as Weave-owned"}))
        sys.exit(3)

    overlays, malformed = {}, 0
    for f in overlay_entries:
        if not f.name.endswith(".overlay.json"):
            continue
        cid = f.name[: -len(".overlay.json")]
        try:
            overlays[cid] = _load_overlay(f, inbox_parent)
        except Exception:
            malformed += 1

    applied = enrich.apply_enrichments(args.cwd, overlays) if overlays else 0
    print(json.dumps({"applied": applied, "malformed": malformed}))


if __name__ == "__main__":
    main()
