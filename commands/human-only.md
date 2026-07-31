---
description: Manage your human-only term list (content that matches is never stored at all)
argument-hint: [--add TERM | --remove TERM | --project]
---

Manage the workspace human-only term list. Run:

```
python "${CLAUDE_PLUGIN_ROOT}/scripts/pcx.py" human-only $ARGUMENTS
```

Relay the output as-is.

This is the stronger of the two lists. A refusal term is redacted; a human-only term means the
matching content is never written to the store at all. Same handling rules as `/refusal`: **the
verb never prints a term and neither should you.** Refer to entries by digest or position, and
answer "is X listed?" by running `human-only --add X` rather than from anything you can see.

Removal here loosens a never-store fence into ordinary capture from the next hook onward. Make
sure the user has seen the readback before moving on.
