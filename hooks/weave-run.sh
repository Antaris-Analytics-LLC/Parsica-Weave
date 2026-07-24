#!/usr/bin/env bash
# Parsica Weave hook launcher. Resolves a working Python 3 and runs pcx.py with
# the hook event name passed by Claude Code. One command, every platform: no
# shell operators in hooks.json (Windows PowerShell 5.1, the stock fallback
# shell, treats "||" as a syntax error), and the interpreter is chosen by
# probing rather than by a name that differs per OS.
#
# The Windows trap this exists to dodge: `python3` there is usually the
# Microsoft Store stub at .../WindowsApps/python3, which exits 49 silently in a
# non-TTY subprocess (a hook). We probe python3 -> python -> py -3 and skip any
# that fails the probe, so the stub falls through to the real python.org install.
set -e

# Force UTF-8 so non-ASCII content and paths do not crash on Windows, where
# Python otherwise defaults to cp1252. No-op on macOS/Linux (already UTF-8).
export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8

HERE="$(cd "$(dirname "$0")" && pwd)"
PCX="$HERE/../scripts/pcx.py"

# Git Bash hands paths in POSIX form (/c/Users/...); a native python.exe would
# read the leading / as a drive root and fail. Convert to native Windows form
# first. cygpath is a Git Bash builtin, absent on macOS/Linux where the guard
# makes this a no-op.
if command -v cygpath >/dev/null 2>&1; then
    PCX="$(cygpath -w "$PCX")"
fi

probe() {
    # Succeeds only for a real Python 3. The Store stub exits nonzero here.
    "$@" -c 'import sys; sys.exit(0 if sys.version_info[0] >= 3 else 1)' >/dev/null 2>&1
}

for cmd in python3 python "py -3"; do
    # cmd is intentionally word-split so "py -3" becomes two tokens.
    if probe $cmd; then
        exec $cmd "$PCX" "$@"
    fi
done

echo "parsica-weave: no working Python 3 interpreter found (tried python3, python, py -3)." >&2
echo "  Windows: install Python from https://python.org (not the Microsoft Store)." >&2
echo "  macOS/Linux: install Python 3 (brew install python, or apt install python3)." >&2
exit 1
