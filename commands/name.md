---
description: Name this workspace's agent (the naming ceremony)
argument-hint: <name>
---

Name this workspace's agent. Run exactly this, with the user's chosen name as the body of the
heredoc. The quoted delimiter means the shell passes the name through untouched, so a quote, a
backslash or a dollar sign in it is data to the verb and never shell:

```
python "${CLAUDE_PLUGIN_ROOT}/scripts/pcx.py" name --name-stdin <<'PARSICA_NAME'
$ARGUMENTS
PARSICA_NAME
```

Relay the warm readback to the user, including the byline preview - it shows exactly
what every reply will open with. If the name was rejected (empty, or fully redacted by
the workspace's own scrub/refusal policy), say so plainly and ask for another.

From your next reply on, honor the byline the readback previews: open every response
with that single line. The name is a display label, never a store key - nothing about
the workspace's memory changed.
