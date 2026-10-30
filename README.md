# Claude Code hooks

Three [Claude Code hooks](https://docs.claude.com/en/docs/claude-code/hooks) I run in every session. Each one came out of a specific thing that went wrong, and each folder's README says what happened, how the hook was tested and where it falls short.

| Hook | Event | What it does |
|---|---|---|
| [pkill-guard](pkill-guard/) | `PreToolUse` (Bash) | Refuses `pkill`, `pgrep` and `killall` commands that macOS would read far more broadly than they look |
| [ready-move-stop](ready-move-stop/) | `Stop` | Won't let a session end its turn while one of its own next steps needs nothing from you |
| [compaction-ledger](compaction-ledger/) | `SessionStart` (`compact`) | After a compaction, reads the transcript back and gives the session the record its summary may have lost |

Each is a single Python file with no dependencies beyond Python 3.8 or later. To install one, copy its `.py` file to `~/.claude/hooks/` and merge its `settings-snippet.json` into `~/.claude/settings.json`. Run `/hooks` in Claude Code to check it's listed. Settings files get rewritten, and a hook can vanish in the process, so it's worth checking again now and then.

All three fail open. An error inside a hook lets Claude Code carry on as if the hook weren't there, so a broken hook can't stop your work. It also means a broken hook stops protecting you without saying so.

## Tests

```
./tests/run.sh
```

This runs each hook the way Claude Code does (JSON on stdin) against the fixtures in `tests/`, with `HOME` pointed at a temporary folder so nothing touches your real `~/.claude`.

## License

MIT. See [LICENSE](LICENSE).
