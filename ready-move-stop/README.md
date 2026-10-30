# ready-move-stop

A `Stop` hook that won't let a session end its turn while one of its own next steps needs nothing from you.

It's built around a reply convention: each reply ends with a numbered **Your moves** list (what the person has to do) and a **My moves** list (what Claude does next). The working rule behind it is: before ending a turn, do every My move that needs nothing from the user, and end only when what's left waits on them or on a job that will wake the session. If your sessions don't write a My moves list, the hook does nothing.

## Why it exists

On 18 October 2026 four of my sessions ended a turn with a My move they could have done on their own, then sat idle for 9 to nearly 11 hours until I came back. The rule was then written down in plain words, and the session that wrote it broke it in its very next turn. A sentence in an instructions file wasn't enough, so this hook makes the rule a mechanism.

## How it works

When the session tries to end a turn, the hook reads the last reply from the transcript and finds the My moves list. An item counts as **waiting** when:

- it names what it waits on ("when you", "on your yes", "waits on", "after the review lands", "on or after 2 November", and similar phrases);
- it opens with When, Once, After, If, "On your", or a quoted trigger such as `On "merge":`;
- it follows a waiting item and doesn't open with Meanwhile, Now or In parallel, because the list is in order;
- a background job, agent or Monitor started in the session hasn't reported back yet. While one is running, only an item that opens with Meanwhile, Now or In parallel can block.

An item that opens with "Nothing" or "None" is no move at all. Any other item is **ready**, and the hook blocks the turn with a message quoting it: do it now, or say what it waits on.

It blocks at most once per turn (Claude Code's `stop_hook_active` flag), so a wrong block costs one extra turn and can never trap a session. Any error lets the turn end, with a line in `~/.claude/logs/ready-move-hook.log`. It never runs anything it reads from the transcript.

Set `READY_MOVE_OWNER` to your first name in the hook's command (see `settings-snippet.json`), so that "Sam decides", "ask Sam" and "on Sam's yes" count as waiting. Without it, only phrasings with "you" and "the user" count.

## Tested

The version I run names me directly. This copy reads the name from `READY_MOVE_OWNER` instead; the logic is otherwise the same. The real-transcript replays below were run on my version. This copy has been run only against the fixtures in `tests/`.

- **Replays of real replies.** The first version blocked 52 of 109 turn-ending replies from four real sessions, and most of those blocks were noise: the session really was waiting on a review or job it had started, but the item didn't say so. After the background-job rule was added, it blocked 4 of 109, and all 4 looked like real misses, including the two that had idled for over 9 hours.
- **A live run.** A throwaway `claude -p` session on Sonnet (about $0.20) was given a prompt that ended with an undone move. It was blocked once, did the move, and ended.
- **An independent review** by a separate model found nothing serious. It did find two medium issues, a slow regular expression and eight missed ways of saying "waiting", and both were fixed.
- **Ten days of real use (19 to 28 October 2026):** 34 real blocks. 17 were noise, mostly moves gated on my word in phrasings the hook didn't know ("On a yes:", `On "deploy it":`) and empty moves ("Nothing."). The phrase lists were widened, and a replay of all 34 confirmed those 17 now pass while the other 17 still block. Those 17 haven't all been judged yet. Several look like real misses, and several look like waits the item didn't name.

## Install

Copy `ready-move-stop.py` to `~/.claude/hooks/` and merge `settings-snippet.json` into `~/.claude/settings.json`. Replace `YourFirstName` in the snippet with your own first name (it's how the hook recognises a move that waits on you), or delete `READY_MOVE_OWNER=YourFirstName ` and it will look for "the user" instead. Run `/hooks` to confirm it's loaded.

## Limits

- It can't tell whether a stated wait is real. "When the review reports" passes even when no review is running.
- While a background job is running, it lets a forgotten ready move through.
- Subagents end with `SubagentStop`, not `Stop`, so they aren't covered.
- The phrase lists are English and were tuned on one person's sessions. Expect some noise at first, and read the log.
- An item that opens "Nothing like..." or "None of..." but is really a move will pass.
- It reads numbered items (`1.` or `1)`) and top-level bullets. A list written any other way isn't checked, and a plain bulleted note under My moves that isn't a move can be read as one and block the turn once.

## When to retire it

This one exists because of how models behaved in October 2026: ending a turn with work they could have done on their own. Newer models may stop doing that. Every block is logged to `~/.claude/logs/ready-move-hook.log` with the move it caught, so after a model change, read the log for two weeks. If the blocks are mostly noise (moves that really did wait on you) and the real catches have dropped to almost none, turn it off. Retire it too if Claude Code adds its own check for this.
