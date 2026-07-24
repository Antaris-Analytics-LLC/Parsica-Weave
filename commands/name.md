---
description: Name this workspace's agent (the naming ceremony)
argument-hint: <name>
---

Name this workspace's agent. Run the identity verb with the user's chosen name:

```
python "${CLAUDE_PLUGIN_ROOT}/scripts/pcx.py" agent --name "$ARGUMENTS"
```

Relay the warm readback to the user, including the byline preview - it shows exactly
what every reply will open with. If the name was rejected (empty, or fully redacted by
the workspace's own scrub/refusal policy), say so plainly and ask for another.

From your next reply on, honor the byline the readback previews: open every response
with that single line. The name is a display label, never a store key - nothing about
the workspace's memory changed.
