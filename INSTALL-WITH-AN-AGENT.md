# Installing these with your agent

You can install these by hand (each folder's README has the steps), but it's easier to let your coding agent do it. That's how I set them up. Paste the prompt below into Claude Code, or another agent that can read files and run commands, from a folder where it can clone this repo.

```
Install hooks from https://github.com/joelpennell/claude-code-hooks for me.
Change nothing in ~/.claude until I say yes in step 6.

1. Clone the repo and check out the latest release tag (`git tag`), or tell
   me the commit you're using if there are no tags.
2. Read README.md, each hook folder's README, and each hook's .py file.
   For each hook tell me, in a sentence or two: what it does, the problem it
   came from, what files it reads and writes on my machine, and its "When
   to retire it" section. Point out that compaction-ledger keeps a copy of
   what I type in ~/.claude/ledgers and never deletes it, and that
   ready-move-stop only does anything if my agent ends its replies with a
   numbered "My moves" list.
3. Ask me which hooks I want. Don't install anything I haven't picked.
4. Check my setup: my operating system, `python3 --version` (3.8 or later;
   on Windows it may be `python`), whether ~/.claude/settings.json already
   has hooks for the same events, and whether ~/.claude/hooks/ already has
   files with the same names. Tell me about any conflict.
5. On macOS or Linux, run ./tests/run.sh in the clone and show me the
   result. On Windows, skip this and say so.
6. Show me exactly what you'll change: the files you'll copy into
   ~/.claude/hooks/, and the diff you'll make to ~/.claude/settings.json.
   Keep every hook I already have. If a file with the same name exists,
   show me both versions and ask before replacing it. Wait for my yes.
7. Back up ~/.claude/settings.json with a dated name, then make the
   changes. For ready-move-stop, ask me what first name to use for
   READY_MOVE_OWNER.
8. Tell me to run /hooks to confirm they're loaded, and where the backup is.
```

The prompt has the agent read the code and explain it before anything runs, show you every change before it's made, and back up the file it edits. If you'd rather approve each step, add "ask me before each step" at the end.

To remove a hook, ask your agent to delete its entry from `~/.claude/settings.json` and its file from `~/.claude/hooks/`, or restore the backup from step 7.
