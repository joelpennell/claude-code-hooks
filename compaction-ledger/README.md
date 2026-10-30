# compaction-ledger

A `SessionStart` hook (matcher `compact`) that runs when a Claude Code session resumes after compacting. It reads the session's transcript from disk and gives the resumed session the record its summary may have lost.

It writes a ledger to `~/.claude/ledgers/<session-id>.md` (mode 600) with:

- everything the user typed in the session, word for word with times, including messages typed while Claude was mid-turn and slash commands run;
- whether the last reply before the compaction had been delivered, and how long before, so "did that reply go out?" has a factual answer;
- Claude's own candidate open loops from its replies before the compaction: its "My moves", "I'll ..." lines and questions, labelled as unchecked.

It also prints a digest of that ledger (under 9,000 bytes), which Claude Code adds to the session's context. The digest asks the session to say three things before carrying on: what the summary dropped, whether the last reply went out, and what is still open.

## Why it exists

Since early October 2026, Claude Code compacts a large session (over 200,000 tokens) once it has sat idle for about 55 minutes, just before the prompt cache expires. It saves usage. It also replaces the full history with a summary. `"idleCompaction": false` in `~/.claude/settings.json` turns it off.

Instead of guessing what the summaries lose, I had an agent compare 16 recoveries, covering 19 compactions in 7 projects from 9 to 14 October, against the full transcripts. In that sample none of my decisions or corrections was dropped. The errors that mattered, in 6 of the 16, were about the AI's own loose ends: a summary saying a reply hadn't been sent when it had gone out 55 minutes earlier, or calling a promised task done when it had never been started.

The full record was already on disk. Claude Code writes every turn to a transcript file, and compaction doesn't change that file. The missing step was reading it back at the right moment.

## Install

Copy `compaction-ledger.py` to `~/.claude/hooks/`, then merge `settings-snippet.json` into `~/.claude/settings.json`. To try it without waiting for a compaction, point it at a copy of a transcript:

```
echo '{"session_id":"x","transcript_path":"/path/to/copy.jsonl","source":"compact"}' \
  | python3 -I ~/.claude/hooks/compaction-ledger.py
```

## Tested

- **Design review on another model, before install:** four must-fix problems. The worst was timing. The hook runs before Claude Code writes the boundary row of the compaction that triggered it, so the first version described the previous compaction. The design's own tests had all run on finished transcripts, where the row was already there. Another problem: prompts the AI had scheduled for itself were coming back labelled as the user's words. Both were fixed and re-tested, and it was installed on 14 October 2026.
- **First evening:** it caught a summary that said a reply hadn't been sent when it had gone out 55 minutes earlier. The same evening it failed a check, because one of Claude's promises was in the ledger file but not in the digest, and the session missed it. It was patched the next day.
- **A second review of the patch (18 October)** found two more must-fix problems, and both were fixed. One of them: when the digest has to cut, it now keeps the oldest and newest promises and says how many from the middle are missing, instead of dropping them silently.
- **In daily use:** across the compactions scored from 14 to 20 October, summaries got the AI's own state wrong in 6 of 8. In the 20 October case the summary said a report "was not yet written" and made writing it the next step; the ledger showed it had gone out before the compaction.
- **Not finished:** the acceptance test I set (three passes on real compactions, at least two of them outside the project where it was built) has two passes and one fail, and that fail led to the patch. The patched version fired in real compactions in three projects on 18 and 19 October, and the hook's account was right every time it was scored. Those scores were made by Claude from the transcripts, not by me, so I haven't counted them toward the test.
- `tests/run.sh` checks the digest, the compaction count, the delivered-reply line, a mid-turn message, a promise from an older window being left out of the digest, and the ledger file's permissions.

## Limits

- It reads Claude Code's transcript format, which isn't a documented interface. One field it relies on changed name in September 2026, and the hook reads both names. Expect to adjust it when the format changes.
- The ledger is a record, not a set of conclusions. Its open loops are candidates, found by phrase matching, and some will already be closed.
- The ledger keeps everything you typed in the session, including anything pasted by mistake, such as a key. It's written with owner-only permissions, and nothing deletes or rotates it, so clear out old ledgers yourself.
- The digest is cut to fit under 9,000 bytes. The full ledger file always has everything.
- It treats transcript contents as data and never runs anything from them, but the text it puts back into context is whatever was in the transcript.

## When to retire it

This one exists because compaction summaries got the AI's own state wrong, typically saying a reply hadn't gone out when it had. That happened in 6 of the 8 compactions whose summaries were checked in October 2026 (Claude did the scoring, from the transcripts). After a Claude Code update or a model change, compare the next few summaries with their ledgers. If the summaries get it right every time, or Claude Code starts carrying that state across a compaction itself, retire it. It fires on both automatic compaction and a manual `/compact`, so if you never compact either way, it never fires and you don't need it.
