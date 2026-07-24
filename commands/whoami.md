---
description: Show this workspace's agent identity (name, byline state, live preview)
---

Show the workspace's agent identity. Run:

```
python "${CLAUDE_PLUGIN_ROOT}/scripts/pcx.py" agent
```

Relay the readback to the user as-is: the name, the byline state, and the live preview.
If no name is set, state that plainly - do not suggest setting one unless the user asks.
