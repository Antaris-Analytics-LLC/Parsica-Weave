---
description: Manage your refusal list (terms redacted everywhere content reaches the store)
argument-hint: [--add TERM | --remove TERM | --project]
---

Manage the workspace refusal list. Run:

```
python "${CLAUDE_PLUGIN_ROOT}/scripts/pcx.py" refusal $ARGUMENTS
```

Relay the output as-is.

The list holds the user's own sensitive words, so **the verb never prints a term and neither
should you**. Do not repeat a term back in your reply, do not quote it from their message, and
do not summarize the list by content. Refer to entries by their digest or their position.

If the user asks "is X on my list?", do not answer from anything you can see - run
`refusal --add X`. It is idempotent and reports whether the term was already there, which
answers the question without putting it anywhere new.

If they are removing a term, make sure they have seen the readback: removal loosens the fence
from the next hook onward, and nothing already redacted is restored.
