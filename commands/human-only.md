---
description: Manage your human-only term list (content that matches is never stored at all)
argument-hint: [--add TERM | --remove TERM | --project]
---

Manage the workspace human-only term list. Run exactly this, with the user's text as the body
of the heredoc. The quoted delimiter means the shell passes that text through untouched, so a
quote, a backslash or a dollar sign in a term is data to the verb and never shell. A term with
spaces goes in quotes, as on a shell line; the verb splits the line itself and runs nothing in it.

```
python "${CLAUDE_PLUGIN_ROOT}/scripts/pcx.py" human-only --stdin <<'PARSICA_ARGS'
$ARGUMENTS
PARSICA_ARGS
```

Relay the output as-is.

This is the stronger of the two lists. A refusal term is redacted; a human-only term means the
matching content is never written to the store at all. Same handling rules as `/refusal`: **the
verb never prints a term and neither should you.** Refer to entries by digest or position, and
answer "is X listed?" by running the same command with `--add X` as the heredoc body rather than
from anything you can see.

Removal here loosens a never-store fence into ordinary capture from the next hook onward. Make
sure the user has seen the readback before moving on.
