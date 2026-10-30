# Claude Code hooks

Three [Claude Code hooks](https://docs.claude.com/en/docs/claude-code/hooks) I run in every session. Each one came out of a specific thing that went wrong, and each folder's README says what happened, how the hook was tested and where it falls short.

| Hook | Event | What it does |
|---|---|---|
| [pkill-guard](pkill-guard/) | `PreToolUse` (Bash) | Refuses `pkill`, `pgrep` and `killall` commands that macOS would read far more broadly than they look |
| [ready-move-stop](ready-move-stop/) | `Stop` | Won't let a session end its turn while one of its own next steps needs nothing from you |
| [compaction-ledger](compaction-ledger/) | `SessionStart` (`compact`) | After a compaction, reads the transcript back and gives the session the record its summary may have lost |

## Installing them, and knowing when to stop

Most people will install these by asking their own agent to do it, and that's how I set them up too. [INSTALL-WITH-AN-AGENT.md](INSTALL-WITH-AN-AGENT.md) has a prompt to paste in. It explains each hook, asks which you want, tests them, and backs up your settings before changing anything.

Each hook exists because a model, or my Mac, did something specific that cost me time. Models and Claude Code keep improving, so any of these can stop earning its place, and I expect some of them to. Each README ends with a "When to retire it" section that says what to check.

The rule I use for anything added on top of a stock Claude setup, whether it's a hook, an instructions file or a custom rule: name the failure it fixes, keep a record of that failure happening, and re-check it when the model or the tool changes. When the failure stops showing up without the add-on, take the add-on out. One that guards against a problem that's gone still costs something: false alarms, slower turns, or a rule the model has to work around.

## Where they run

| | macOS | Linux | Windows |
|---|---|---|---|
| pkill-guard | Tested; this is what it's for | Works, but refuses some commands that GNU `pkill` would have handled fine. Harmless, and you may not need it | Untested; no `pkill` there |
| ready-move-stop | Tested | Expected to work; untested | Untested |
| compaction-ledger | Tested | Expected to work; untested | Untested |

All three need Python 3.8 or later (the tests also pass on 3.8 in a Linux container). On Windows the command may be `python` rather than `python3`, and the paths in the snippets assume a Unix-style home folder.

## Installing by hand

Each is a single Python file with no dependencies beyond Python 3.8 or later. To install one, copy its `.py` file to `~/.claude/hooks/` and merge its `settings-snippet.json` into `~/.claude/settings.json`. Run `/hooks` in Claude Code to check it's listed. Settings files get rewritten, and a hook can vanish in the process, so it's worth checking again now and then.

All three fail open. An error inside a hook lets Claude Code carry on as if the hook weren't there, so a broken hook can't stop your work. It also means a broken hook stops protecting you without saying so.

## Tests

```
./tests/run.sh
```

This runs each hook the way Claude Code does (JSON on stdin) against the fixtures in `tests/`, with `HOME` pointed at a temporary folder so nothing touches your real `~/.claude`.

## License

MIT. See [LICENSE](LICENSE).
