#!/usr/bin/env python3
"""User text reaches pcx.py as data, never as shell. REVIEW-2026-10.md, follow-up, class (g).

At e480bbd the slash commands spliced the user's text into the shell line the agent runs
(commands/refusal.md 9 and commands/human-only.md 9 unquoted, commands/name.md 9 inside double
quotes), and the skill told the agent to single-quote arbitrary markdown into an echo
(skills/weave/SKILL.md 72-85; the README and ONBOARDING.md showed the same form): a quote in the
text ended the string, and a dollar sign or a backtick inside double quotes expanded or ran. The
commands now hand the text over standard input as the body of a heredoc whose delimiter is
quoted, which the shell passes through byte for byte, and the verbs read it from there.

  1. Structural: every slash command carries $ARGUMENTS only as the body of a quoted heredoc,
     never on the command line, and no shipped document pipes a quoted echo into a verb.
  2. Through a real shell: the documented command blocks, with hostile text in place of
     $ARGUMENTS (a quote, a backslash, a dollar sign, a command substitution, backticks), store
     exactly that text as the term, the name or the note, and nothing is executed (BLOCKED); an
     ordinary term and an ordinary name land unchanged (ALLOWED).
  3. The verbs with no shell at all: a stray word or an unbalanced quote is refused with a
     constant message that never echoes the text, and nothing is written.

Runs under pytest and standalone (python test_shell_arguments_are_data.py). The shell cases
need a bash (Linux, macOS, Git Bash on Windows) and report a skip where none is found; the
structural and verb cases always run.
"""
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("PARSICA_WEAVE_ROOT", tempfile.mkdtemp(prefix="shellargs_boot_"))
SCRIPTS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(SCRIPTS)
sys.path.insert(0, SCRIPTS)
import store  # noqa: E402

PCX = os.path.join(SCRIPTS, "pcx.py")
COMMANDS = ("refusal.md", "human-only.md", "name.md")
SHIPPED_DOCS = ["README.md", "ONBOARDING.md", "AUTONOMOUS_ENRICHMENT.md", "SECURITY.md",
                os.path.join("skills", "weave", "SKILL.md")]
FENCE = re.compile(r"```[^\n]*\n(.*?)```", re.S)
ECHO_QUOTED = re.compile(r"^\s*echo\s+'")
MARKER = "shellargs_marker_file"


# --- 1. structural ----------------------------------------------------------------------------------
def _command_block(fn):
    text = open(os.path.join(ROOT, "commands", fn), encoding="utf-8").read()
    blocks = FENCE.findall(text)
    assert blocks, "%s has no fenced command block" % fn
    return text, blocks[0].strip("\n")


def test_slash_commands_pass_the_text_only_as_a_quoted_heredoc_body():
    for fn in COMMANDS:
        text, block = _command_block(fn)
        lines = block.splitlines()
        assert len(lines) == 3, "%s: the command block is a command line, a body and a delimiter: %r" % (fn, lines)
        cmd, body, end = lines
        assert "$ARGUMENTS" not in cmd, "%s splices the user's text into the shell line: %r" % (fn, cmd)
        assert "--stdin" in cmd or "--name-stdin" in cmd, "%s does not read its text from stdin: %r" % (fn, cmd)
        m = re.search(r"<<'([A-Z_]+)'\s*$", cmd)
        assert m, "%s does not use a QUOTED heredoc delimiter: %r" % (fn, cmd)
        assert body == "$ARGUMENTS" and end == m.group(1), (fn, lines)
        assert text.count("$ARGUMENTS") == 1, "%s names $ARGUMENTS outside the heredoc body" % fn


def test_no_shipped_document_pipes_a_quoted_echo_into_a_verb():
    paths = [os.path.join(ROOT, d) for d in SHIPPED_DOCS]
    paths += sorted(glob.glob(os.path.join(ROOT, "commands", "*.md")))
    checked = 0
    for p in paths:
        if not os.path.isfile(p):
            continue
        checked += 1
        hits = [ln for ln in open(p, encoding="utf-8", errors="replace").read().splitlines()
                if ECHO_QUOTED.search(ln)]
        assert not hits, "%s still quotes content into a shell line: %r" % (os.path.relpath(p, ROOT), hits[:2])
    assert checked >= 7, "discovery broke: too few documents checked (%d)" % checked


# --- shared plumbing --------------------------------------------------------------------------------
def _fresh_root():
    tmp = tempfile.mkdtemp(prefix="shellargs_root_")
    store.STORE_ROOT = Path(tmp)
    store.GLOBAL_CONFIG = store.STORE_ROOT / "config.json"
    cwd = tempfile.mkdtemp(prefix="shellargs_proj_")
    store.ensure_workspace(cwd)
    return cwd


def _env():
    env = dict(os.environ, PARSICA_WEAVE_ROOT=str(store.STORE_ROOT), PYTHONIOENCODING="utf-8",
               CLAUDE_PLUGIN_ROOT=ROOT)
    env.pop("PARSICA_ISOLATED", None)
    return env


def _find_bash():
    """A bash that can run the documented command (the same rule doctor.py applies to the hook
    launcher): Git Bash beside a git.exe on PATH, else a bash on PATH that is not the WSL stub."""
    g = shutil.which("git")
    if g:
        d = Path(g).resolve()
        for root in (d.parent.parent, d.parent.parent.parent):
            for cand in (root / "bin" / "bash.exe", root / "usr" / "bin" / "bash.exe"):
                if cand.is_file():
                    return str(cand)
    b = shutil.which("bash")
    if b:
        rp = str(Path(b).resolve()).lower()
        if ("\\windows\\system32\\" in rp) or ("\\windowsapps\\" in rp):
            return None
    return b


def _skip(reason):
    try:
        import pytest
    except ImportError:
        raise RuntimeError("SKIP: " + reason)
    pytest.skip(reason)


def _python_on_path(env, workdir):
    """The documented commands say `python`. Where only this interpreter is reachable by another
    name, put a `python` shim first on PATH so the shell runs the interpreter the test runs under."""
    if shutil.which("python", path=env.get("PATH")):
        return env
    shim = os.path.join(workdir, "bin")
    os.makedirs(shim, exist_ok=True)
    try:
        os.symlink(sys.executable, os.path.join(shim, "python"))
    except (OSError, NotImplementedError, AttributeError):
        _skip("no `python` on PATH and no way to shim one here")
    env["PATH"] = shim + os.pathsep + env.get("PATH", "")
    return env


def _run_in_bash(script, cwd, env, name):
    bash = _find_bash()
    if not bash:
        _skip("no bash available to run the documented command")
    env = _python_on_path(env, cwd)
    path = os.path.join(cwd, name + ".sh")
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(script + "\n")
    return subprocess.run([bash, path], cwd=cwd, env=env, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=180)


def _run_command_file(fn, arguments, cwd, env):
    """The slash command exactly as shipped, with the user's text in place of $ARGUMENTS the way
    the harness substitutes it (textually), run through bash."""
    _, block = _command_block(fn)
    return _run_in_bash(block.replace("$ARGUMENTS", arguments), cwd, env, fn.replace(".md", ""))


# --- 2. through a real shell --------------------------------------------------------------------------
HOSTILE_TERM_LINE = '--add "it\'s $HOME \\ $(touch %s) `touch %s`" --project' % (MARKER, MARKER)
HOSTILE_TERM = "it's $HOME \\ $(touch %s) `touch %s`" % (MARKER, MARKER)


def test_refusal_command_stores_the_hostile_text_as_the_term_and_runs_nothing():
    cwd = _fresh_root()
    r = _run_command_file("refusal.md", HOSTILE_TERM_LINE, cwd, _env())
    assert r.returncode == 0, (r.returncode, r.stdout[-400:], r.stderr[-400:])
    assert not os.path.exists(os.path.join(cwd, MARKER)), "the shell ran a command substitution from the text"
    assert store.read_policy_list("refusal", "project", cwd) == [HOSTILE_TERM]
    assert store.read_policy_list("refusal", "global", cwd) == []
    assert "added to this project's refusal list" in r.stdout
    assert MARKER not in r.stdout and "it's" not in r.stdout     # the term is never echoed


def test_human_only_command_stores_the_hostile_text_as_the_term_and_runs_nothing():
    cwd = _fresh_root()
    r = _run_command_file("human-only.md", HOSTILE_TERM_LINE, cwd, _env())
    assert r.returncode == 0, (r.returncode, r.stdout[-400:], r.stderr[-400:])
    assert not os.path.exists(os.path.join(cwd, MARKER))
    assert store.read_policy_list("human_only_terms", "project", cwd) == [HOSTILE_TERM]
    assert "it's" not in r.stdout


def test_refusal_command_with_an_ordinary_term_lands_unchanged():
    cwd = _fresh_root()
    r = _run_command_file("refusal.md", "--add falconry", cwd, _env())
    assert r.returncode == 0, (r.returncode, r.stdout[-400:], r.stderr[-400:])
    assert store.read_policy_list("refusal", "global", cwd) == ["falconry"]
    r = _run_command_file("refusal.md", "", cwd, _env())                  # /refusal with no text: the listing
    assert r.returncode == 0 and "1 term(s)" in r.stdout and "falconry" not in r.stdout


HOSTILE_NAME = "O'Brien $5 back\\slash $(touch %s)" % MARKER       # under the 80-char name cap


def test_name_command_stores_the_hostile_text_as_the_name_and_runs_nothing():
    cwd = _fresh_root()
    r = _run_command_file("name.md", HOSTILE_NAME, cwd, _env())
    assert r.returncode == 0, (r.returncode, r.stdout[-400:], r.stderr[-400:])
    assert not os.path.exists(os.path.join(cwd, MARKER)), "the shell ran a command substitution from the name"
    cfg = json.loads(store.GLOBAL_CONFIG.read_text(encoding="utf-8"))
    assert cfg["agent_name"] == HOSTILE_NAME


def test_name_command_with_an_ordinary_name_lands_unchanged():
    cwd = _fresh_root()
    r = _run_command_file("name.md", "Aria", cwd, _env())
    assert r.returncode == 0, (r.returncode, r.stdout[-400:], r.stderr[-400:])
    cfg = json.loads(store.GLOBAL_CONFIG.read_text(encoding="utf-8"))
    assert cfg["agent_name"] == "Aria"


def _skill_note_block():
    text = open(os.path.join(ROOT, "skills", "weave", "SKILL.md"), encoding="utf-8").read()
    for block in FENCE.findall(text):
        lines = [ln.strip() for ln in block.strip("\n").splitlines()]
        if lines and "note --kind working" in lines[0]:
            return "\n".join(lines)
    raise AssertionError("SKILL.md no longer shows the working-set note command")


HOSTILE_NOTE = ("# Working set\n"
                "- it's a 'quoted' line with $HOME and a back\\slash\n"
                "- $(touch %s) and `touch %s` stay text\n" % (MARKER, MARKER))


def test_skill_note_form_stores_the_hostile_markdown_verbatim_and_runs_nothing():
    cwd = _fresh_root()
    block = _skill_note_block().replace("<markdown>", HOSTILE_NOTE.rstrip("\n")).replace('"<C>"', '"%s"' % cwd)
    r = _run_in_bash(block, cwd, _env(), "skill_note")
    assert r.returncode == 0, (r.returncode, r.stdout[-400:], r.stderr[-400:])
    assert not os.path.exists(os.path.join(cwd, MARKER)), "the shell ran a command substitution from the note"
    stored = store.working_path(cwd).read_text(encoding="utf-8")
    for line in HOSTILE_NOTE.splitlines():
        assert line in stored, "note line did not land verbatim: %r" % line


# --- 3. the verbs with no shell ---------------------------------------------------------------------
def _verb(argv, stdin_text):
    return subprocess.run([sys.executable, PCX] + argv, input=stdin_text, env=_env(),
                          capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)


def test_stdin_line_with_a_stray_word_is_refused_without_echo_and_nothing_is_written():
    cwd = _fresh_root()
    r = _verb(["refusal", "--stdin"], "--add Project Falcon\n")
    assert r.returncode == 2 and "unexpected extra argument" in r.stdout, (r.returncode, r.stdout, r.stderr)
    assert "Falcon" not in r.stdout + r.stderr
    assert store.read_policy_list("refusal", "global", cwd) == []


def test_stdin_line_with_an_unbalanced_quote_is_refused_without_echo():
    cwd = _fresh_root()
    r = _verb(["refusal", "--stdin"], '--add "Falcon\n')
    assert r.returncode == 2 and "unbalanced quote" in r.stdout, (r.returncode, r.stdout, r.stderr)
    assert "Falcon" not in r.stdout + r.stderr
    assert store.read_policy_list("refusal", "global", cwd) == []


def test_stdin_flag_takes_no_other_argument_and_cannot_nest():
    cwd = _fresh_root()
    r = _verb(["refusal", "--stdin", "--project"], "--add Falcon\n")
    assert r.returncode == 2 and "give no other argument" in r.stderr
    r = _verb(["refusal", "--stdin"], "--stdin --add Falcon\n")
    assert r.returncode == 2 and "cannot appear inside" in r.stderr
    assert store.read_policy_list("refusal", "global", cwd) == []


def test_stdin_line_quoted_term_lands_and_the_command_line_form_still_works():
    cwd = _fresh_root()
    r = _verb(["refusal", "--stdin"], "--add 'Project Falcon'\n")
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    assert store.read_policy_list("refusal", "global", cwd) == ["Project Falcon"]
    r = _verb(["refusal", "--add", "second term"], "")
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    assert store.read_policy_list("refusal", "global", cwd) == ["Project Falcon", "second term"]


def test_name_stdin_reads_the_name_verbatim_and_refuses_to_combine_with_name():
    cwd = _fresh_root()
    r = _verb(["name", "--name-stdin", "--name", "x"], "Aria\n")
    assert r.returncode == 2 and "cannot be combined" in r.stderr
    r = _verb(["name", "--name-stdin"], "\n")
    assert r.returncode == 2 and "must not be empty" in r.stderr
    r = _verb(["name", "--name-stdin"], "Munin the raven\n")
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    cfg = json.loads(store.GLOBAL_CONFIG.read_text(encoding="utf-8"))
    assert cfg["agent_name"] == "Munin the raven"
    del cwd


def _run():
    fns = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = skipped = 0
    for name, fn in fns:
        try:
            fn()
        except BaseException as exc:                       # pytest's Skipped is a BaseException
            if type(exc).__name__ == "Skipped" or str(exc).startswith("SKIP: "):
                skipped += 1
                print("  skip  %s (%s)" % (name, exc))
                continue
            raise
        passed += 1
        print("  ok  %s" % name)
    print("[test_shell_arguments_are_data] %d passed, %d skipped of %d" % (passed, skipped, len(fns)))


if __name__ == "__main__":
    _run()
