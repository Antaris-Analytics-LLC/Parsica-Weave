#!/usr/bin/env python3
"""Parsica Weave CLI dispatcher. Resolves the plugin's scripts/ and forwards to the right one, so
users run `parsica-weave doctor` / `parsica-weave query --grep X` instead of a long script path.
The sh and .cmd shims beside this file only pick a python interpreter and hand off here."""
import os
import sys
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRIPTS = os.path.join(ROOT, "scripts")

DIRECT = {"doctor": "doctor.py", "query": "query.py",
          "enrich": "enrich.py", "calibrate": "calibrate.py"}
# `agent` is the v1.0 spelling of `name`, kept dispatchable forever and listed nowhere: an
# installed v1.0 skill, a shell alias or a user's muscle memory must not start erroring on upgrade.
PCX = {"note", "promote", "channel", "sync", "migrate", "name", "agent",
       "refusal", "human-only"}

HELP = """parsica-weave <command> [args]

  doctor                self-check the install and its trust guarantees
  query [--grep TERM]   retrieve from the chat mirror
  name [--name NAME]    show or set the workspace agent name + byline
  refusal | human-only  manage your redaction / never-store term lists
  note --kind KIND      write a journal / working-set / lesson entry
  promote               promote durable memory to Parsica Spirit (if installed)
  channel | sync        manage channels / catch up other lanes
  migrate               migrate a legacy store to the workspace layout
  enrich | calibrate    enrichment and decision calibration

Run any command with --help for its own options.
"""


def main(argv):
    if not argv or argv[0] in ("-h", "--help", "help"):
        sys.stdout.write(HELP)
        return 0
    cmd, rest = argv[0], argv[1:]
    if cmd in DIRECT:
        target = [os.path.join(SCRIPTS, DIRECT[cmd])] + rest
    elif cmd in PCX:
        target = [os.path.join(SCRIPTS, "pcx.py"), cmd] + rest
    else:
        # constant, un-interpolated: this dispatcher runs BEFORE the store is importable, so it
        # cannot reach the workspace refusal fence - it must never echo caller-supplied argv onto a
        # terminal. The user sees their own typed command on their prompt; `help` lists the valid set.
        sys.stderr.write("parsica-weave: unknown command (run 'parsica-weave help' for the list)\n")
        return 2
    return subprocess.call([sys.executable] + target)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
