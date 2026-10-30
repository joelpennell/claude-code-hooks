# Installing these with your agent

You can install these by hand (each folder's README has the steps), but it's easier to let your coding agent do it. That's how I set them up. Paste the prompt below into Claude Code, or another agent that can read files and run commands, from a folder where it can clone this repo.

```
Install hooks from https://github.com/joelpennell/claude-code-hooks for me.

1. Clone the repo and read README.md, then each hook folder's README.
2. In one or two sentences each, tell me what each hook does, the problem
   it came from, and its "When to retire it" section. Ask me which ones I
   want. Don't install anything I haven't picked.
3. Check my setup first: my operating system, `python3 --version` (3.8 or
   later), and whether ~/.claude/settings.json already has hooks for the
   same events. Tell me about any conflict before changing anything.
4. Run ./tests/run.sh in the clone and show me the result.
5. Back up ~/.claude/settings.json with a dated name, then copy each hook I
   picked into ~/.claude/hooks/ and merge its settings-snippet.json into
   ~/.claude/settings.json. Keep every hook I already have.
6. For ready-move-stop, ask me what first name to use for READY_MOVE_OWNER.
7. Show me the diff of settings.json, then tell me to run /hooks to confirm
   they're loaded.
```

The prompt has the agent explain before it installs, test before it touches your settings, and back up the file it changes. If you'd rather review every step, add "ask me before each step" to the end.

To remove a hook, ask your agent to delete its entry from `~/.claude/settings.json` and its file from `~/.claude/hooks/`, or restore the backup from step 5.
