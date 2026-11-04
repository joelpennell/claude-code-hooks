# compaction-ledger

A `SessionStart` hook (matcher `compact`) that runs when a Claude Code session resumes after compacting. It reads the session's transcript from disk and gives the resumed session the record its summary may have lost.

It writes a ledger to `~/.claude/ledgers/<session-id>.md` (mode 600) with:

- everything the user typed in the session, word for word with times, including messages typed while Claude was mid-turn and slash commands run;
- whether the last reply before the compaction had been delivered, and how long before, so "did that reply go out?" has a factual answer;
- Claude's own candidate open loops from its replies before the compaction: its "My moves", "I'll ..." lines and questions, labelled as unchecked.
- the actions the session already took outside the conversation (commits, pushes, deploys, pull-request merges, messages sent, posts and submits in the browser, deletions, web requests that change something), each with when it was called and what its tool result says: ran without error, failed or refused, started in the background, or ran with its exit status hidden by a pipe.

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

## The actions list

A reader on X suggested it: summaries are easier to trust with a note, outside the chat, of each action the agent finished. Every action is already in the transcript as a tool call with its result, so the hook reads them back. The digest lists anything that failed, was refused or didn't finish first, then the rest, and asks the session to check whether an action took effect before redoing one the summary calls pending.

How it was tested (4 November 2026): two independent reviews on another model. The first broke the original version in three must-fix ways (a failed `git push 2>&1 | tail` reported as a success, quoted text read as commands, and `<<<` swallowing the rest of a script). The second broke the rewrite in eight more (escaped quotes, a comment with an apostrophe, `if git push; then`, `bash -c "git push"` and others), which led to reading shell commands with Python's own shell tokenizer. Every input from both reviews is now a test. On five real transcripts from five projects, the commits and pushes it lists match a separate count call by call.

How often it matters: in 141 of my compactions that followed a long pause, I found one summary that called an already-finished action pending (a merged pull request listed as still to merge). Most were scored in one pass by a small model and only merges and pushes were cross-checked, so a few may have been missed. The error that keeps happening is the last reply, below. The list is cheap insurance for a rare case.

What to know before relying on it:
- "Ran without error" means the tool succeeded. It doesn't mean the effect was checked: a click on a Reply button can succeed without posting.
- A command piped into `tail` or followed by `;` hides its own exit status; those are labelled, and in some sessions they're most of the list.
- It recognizes actions by pattern. A deploy or a file move run through your own script can be missed, so an action that isn't listed isn't evidence that nothing was done.
- The hook's code behaves the same under any model, so the list itself doesn't depend on the model. What does is whether the session follows the digest's instructions (the three lines, checking before redoing). Every session I've scored ran Claude Opus 5.5; I haven't tested a smaller model, and a smaller one may follow a long digest less reliably.
- The list takes up to a third of the digest, so fewer of your own messages fit there (on one long session, 5 of 26 instead of 14). All of them are still in the ledger file.

## Pair it with a Compact Instructions section

A reader suggested a step I hadn't tested: a `## Compact Instructions` section in your CLAUDE.md that tells the summary what to keep. I now run one alongside this hook:

```markdown
## Compact Instructions

In the summary, list every action already taken outside the conversation (messages sent, commits, pushes, deploys, posts, files deleted), each with its time. Never describe one of these as pending unless the transcript shows it was not done.
```

In the six compactions that followed (1 and 2 November 2026), four summaries carried that list, and the ones I checked were accurate about what had been done. All six still listed the last reply as something to do after it had gone out. The section can't fix that case: Claude Code keeps the last few messages word for word after the summary, and its own note says the summary is written without seeing them. In all 136 compactions on my disk that followed a long pause, the last reply was in that kept part. So the section is a cheap layer for the rest of the record, and this hook covers the last reply.

## When to retire it

This one exists because compaction summaries got the AI's own state wrong, typically saying a reply hadn't gone out when it had. That happened in 6 of the 8 compactions whose summaries were checked in October 2026 (Claude did the scoring, from the transcripts). After a Claude Code update or a model change, compare the next few summaries with their ledgers. If the summaries get it right every time, or Claude Code starts carrying that state across a compaction itself, retire it. A quick signal to watch for: today every summary ends with Claude Code's note that "the summary was written without seeing" the messages kept after it. If that note goes away, the summary may now see the last reply, so check a few summaries then. It fires on both automatic compaction and a manual `/compact`, so if you never compact either way, it never fires and you don't need it.
