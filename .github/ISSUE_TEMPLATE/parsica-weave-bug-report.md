---
name: Parsica Weave Bug report
about: Create a report to help us improve
title: Parsica Weave Bug Report - [Explain in a few words here]
labels: bug, documentation, duplicate, enhancement, good first issue, help wanted,
  invalid, question, wontfix
assignees: Antaris-Analytics
type: Bug

---

**Describe the bug**
A clear and concise description of what the bug is.

**To Reproduce**
Steps to reproduce the behavior — commands run, in order, and what happened at each step.

**Expected behavior**
A clear and concise description of what you expected to happen instead.

**`doctor` output**
Run `parsica-weave doctor` (or `python "${CLAUDE_PLUGIN_ROOT}/scripts/doctor.py"` if it's not on your PATH) and paste the output here. It checks layout, the scrub gate, and capture — most bugs show up here directly.

**Environment**
 - OS: [e.g. Windows 11, macOS 14, Ubuntu 22.04]
 - Install method: [marketplace / direct from GitHub]
 - Claude Code version:

**Additional context**
Add any other context about the problem here. If you're pasting `config.json`, please redact the `refusal` and `human_only_terms` lists first — those often contain exactly the sensitive terms someone was trying to keep private.
