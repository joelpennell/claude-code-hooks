# pkill-guard

A `PreToolUse` hook for the Bash tool. It refuses two forms of `pkill`, `pgrep` and `killall` that macOS reads far more broadly than they look:

- a flag written after the pattern, as in `pkill -f "some pattern" -n`;
- `pkill -f` or `pgrep -f` with a pattern shorter than 4 characters.

Everything else passes through untouched. When it refuses, Claude Code shows the session the reason, and the session can rewrite the command.

## Why it exists

On 26 September 2026 a Claude Code session ran a command of the form `pkill -f "gh run watch <run-id>" -n`, meaning to kill the newest matching process. On macOS, `pkill` stops reading options at the first pattern, so the trailing `-n` became a second pattern. With `-f`, that pattern matches every process whose command line contains `-n`, which `pgrep` confirmed is almost every process on the machine.

1.2 seconds later, four Claude Code sessions, Docker Desktop and the Claude desktop app all received SIGTERM. That this command caused it is inferred, not proven: by the time anyone looked, the dead processes' command lines could no longer be inspected. The shape of the mistake is real either way, and it's easy for a model to make, because on Linux the same command does what it looks like.

Two days later the hook disappeared from `~/.claude/settings.json` without any warning, because a settings save was written from an older copy of the file. If you rely on it, check now and then that it's still there:

```
jq .hooks.PreToolUse ~/.claude/settings.json
```

## Install

Copy `pkill-guard.py` to `~/.claude/hooks/`, then merge `settings-snippet.json` into `~/.claude/settings.json`. Run `/hooks` in Claude Code to confirm it's listed.

## Tested

`tests/run.sh` at the top of this repo feeds it the incident's command, a safe reordering of it, a short `-f` pattern, a trailing flag inside a `&&` chain, an option that takes a value, an unrelated command and input that isn't JSON. A copy with the check disabled fails those tests. Whether it has blocked a real command since it was installed hasn't been recorded.

## Limits

- It reads the command text only. A kill inside a script, `sh -c "..."`, `eval` or `xargs` isn't seen.
- If the shell quoting can't be parsed, it lets the command through rather than guess.
- It applies macOS (BSD) rules everywhere. On Linux, where `pkill` accepts options after the pattern, it refuses commands that would have been fine. That's the cost of a refusal, not a danger.
- The 4-character minimum is a judgment call, not a measured threshold.
- `kill <exact PID>`, after `pgrep` shows what a pattern matches, avoids the problem entirely.
