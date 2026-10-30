#!/usr/bin/env python3
"""PreToolUse guard for Bash: refuse pkill/pgrep/killall calls that macOS would
read more broadly than they look.

Why (2026-09-26): a session ran `pkill -f "gh run watch <run-id>" -n`. macOS
(BSD) pkill stops option parsing at the first pattern, so the trailing `-n`
becomes a SECOND pattern; with -f it matches every process whose command line
contains "-n" (measured with pgrep). 1.2 s later four Claude Code sessions,
Docker Desktop and the Claude desktop window all received SIGTERM. That this
command caused it is inferred: the dead processes' command lines could not be
inspected afterwards.

Refused:
  1. any flag after the first pattern (pkill, pgrep, killall);
  2. pkill/pgrep -f with a pattern shorter than 4 characters (matches too much).
Everything else passes through untouched. pgrep only lists, but it is checked
too, because a pgrep line is usually the dry run for a pkill line.
"""
import json
import os
import shlex
import sys

TOOLS = {"pkill", "pgrep", "killall"}
# BSD pkill/pgrep options that consume the next argument
TAKES_ARG = set("FGgJPsTtUu")
SEPARATORS = {";", "&&", "||", "|", "&", "(", ")"}
# commands that run another command after their own options
WRAPPERS = {"sudo", "env", "nohup", "command", "exec", "time", "timeout", "xargs", "nice"}
SHELLS = {"sh", "bash", "zsh", "dash"}


def split_commands(cmd):
    # shlex treats a newline as plain whitespace, so mark line breaks as separators
    cmd = cmd.replace("\n", " ; ")
    lex = shlex.shlex(cmd, posix=True, punctuation_chars=";&|()")
    lex.whitespace_split = True
    lex.commenters = ""
    current = []
    for tok in lex:
        if tok in SEPARATORS or set(tok) <= set(";&|()"):
            if current:
                yield current
            current = []
        else:
            current.append(tok)
    if current:
        yield current


def strip_wrappers(argv):
    """Drop env assignments and wrappers (sudo -u x, timeout 5, env A=1 ...)."""
    while argv:
        head = os.path.basename(argv[0]).lower()
        if "=" in argv[0] and not argv[0].startswith("-"):
            argv = argv[1:]
        elif head in WRAPPERS:
            argv = argv[1:]
            # the wrapper's own options and their values, then a duration for timeout
            while argv and (argv[0].startswith("-") or "=" in argv[0]
                            or (head == "timeout" and argv[0][:1].isdigit())):
                if argv[0] in ("-u", "-g", "-n", "-s", "-k", "-C", "-I") and head != "env" and len(argv) > 1 \
                        and not argv[1].startswith("-") and os.path.basename(argv[1]).lower() not in TOOLS:
                    argv = argv[1:]
                argv = argv[1:]
        else:
            return argv
    return argv


def problem(argv):
    argv = strip_wrappers(argv)
    if not argv:
        return None
    head = os.path.basename(argv[0]).lower()
    if head in SHELLS and "-c" in argv[1:]:
        # check the command string a shell is asked to run
        i = argv.index("-c")
        if i + 1 < len(argv):
            try:
                for inner in split_commands(argv[i + 1]):
                    msg = problem(inner)
                    if msg:
                        return msg
            except ValueError:
                return None
        return None
    if head not in TOOLS:
        return None
    tool = head
    args = argv[1:]
    full_match = False
    patterns = []
    i = 0
    while i < len(args):
        a = args[i]
        if patterns:
            if a.startswith("-") and len(a) > 1:
                return (f"`{tool}` has the flag `{a}` AFTER its pattern. On macOS option parsing "
                        f"stops at the first pattern, so `{a}` becomes a second pattern and can match "
                        f"(and signal) almost every process. Put every flag before the pattern.")
            patterns.append(a)
        elif a == "--":
            patterns.extend(args[i + 1:])
            break
        elif a.startswith("-") and len(a) > 1:
            letters = a[1:]
            if tool != "pgrep" and (letters.isdigit() or (len(letters) > 1 and letters.isupper())):
                i += 1  # a signal (-9, -HUP, -INT): takes no value
                continue
            if "f" in letters:
                full_match = True
            if letters[-1] in TAKES_ARG and tool != "killall":
                i += 1  # its value
        else:
            patterns.append(a)
        i += 1
    if tool in ("pkill", "pgrep") and full_match:
        for p in patterns:
            if len(p) < 4:
                return (f"`{tool} -f` with the pattern `{p}` is shorter than 4 characters and "
                        f"will match far more processes than intended. Use a longer, specific "
                        f"pattern, or kill by exact PID.")
    return None


def main():
    try:
        data = json.load(sys.stdin)
        cmd = data.get("tool_input", {}).get("command", "")
    except Exception:
        return 0
    if not any(t in cmd.lower() for t in TOOLS):
        return 0
    try:
        commands = list(split_commands(cmd))
    except ValueError:
        return 0  # unparseable quoting: leave it to the shell
    for argv in commands:
        msg = problem(argv)
        if msg:
            print(json.dumps({
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": "pkill-guard: " + msg,
                }
            }))
            return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
