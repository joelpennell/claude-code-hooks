#!/usr/bin/env python3
"""Stop hook: refuse to end a turn whose "My moves" list holds a move that needs nothing from the user.

It enforces a working rule: "Before ending a turn, do every My move that needs nothing from the user; end
only when what remains waits on them or on a job that will wake this session." It assumes replies end with
a numbered "My moves" list (and usually a "Your moves" list for the user). Without that convention it does
nothing.

Reads the last reply (after the last user or notification row) from the transcript, finds its My moves
list, and counts an item as waiting if it names what it waits on (WAIT), opens with When/Once/After/If/On
your, or follows a waiting item without opening with Meanwhile/Now/In parallel. While a background job,
agent or Monitor launched in the session has no completion notice (or a named teammate exists), an
unmarked item counts as waiting on it; only Meanwhile/Now/In parallel items can then block. Any other item is ready:
the hook exits 2 with the reason on stderr, which Claude Code feeds back to the session. It blocks at most
once per turn (stop_hook_active), so a wrong block costs one extra turn and never traps the session.
It cannot tell whether a stated wait is real (an unarmed "when X reports" passes). Never runs anything
from the transcript. Fails open on any error, logging to ~/.claude/logs/ready-move-hook.log.

Set READY_MOVE_OWNER to the user's first name (e.g. READY_MOVE_OWNER=Sam) so that "Sam decides",
"ask Sam" and "on Sam's yes" count as waiting. Without it only "the user" and "you" phrasings count.
"""
import json, os, re, sys, time

LOG = os.path.expanduser("~/.claude/logs/ready-move-hook.log")
MAX_SCAN = 1_000_000  # skip giant transcript lines in the completion scan (keeps the regex cost bounded)
OWNER = os.environ.get("READY_MOVE_OWNER", "").strip()
WHO = "|".join(["the user"] + ([re.escape(OWNER.lower())] if OWNER else []))
POSS = "|".join(["his", "her", "their", "the user's"] + ([re.escape(OWNER.lower()) + "'s"] if OWNER else []))
SHOWN = OWNER or "the user"

WAIT = re.compile(
    r"\b(when you|on your|after you|once you|if you|until you|waits? on|waiting (on|for)|blocked (on|until)|"
    r"wakes? (me|this session)|next session|nothing (else )?(is )?ready|nothing needed|"
    r"your (ok|go|yes|word|answer|answers|decision|reply|verdict|pick|read|choice|call)|"
    r"next time|on or after|in a week|after a week|(on|by|from) \d{1,2} (jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*|"
    r"wait (for|on|until)|hold (until|for)|pending|ask (" + WHO + r")|needs? (" + WHO + r"|his|her|their|your)|"
    r"(" + WHO + r") (to|decides|approves|confirms|says|answers|replies)|(after|once|when) (he|she|they|" + WHO + r") \w+|"
    r"(report|review|result|notice|notification)s? (comes?|come in|arrives?|lands?)|"
    r"when .{0,80}?\b(reports?|finish(es)?|arrives?|returns?|lands?|completes?|comes back|is done|is green|exists?))\b",
    re.I)
OPENS_WAIT = re.compile(
    r"^(\*\*)?(?:(?:when|once|after|if|on your)\b|on [\"“'‘]|"  # On "merge": ...
    r"on (?:a |(?:" + POSS + r") )?(?:yes|go|ok|nod|word|reply|answer)\b|with (?:" + POSS + r") .{0,40}?\bin hand\b)",
    re.I)
OPENS_NOW = re.compile(r"^(\*\*)?(meanwhile|now|in parallel)\b", re.I)
EMPTY = re.compile(r"^(\*\*)?(nothing|none)\b", re.I)  # "Nothing." / "None." is no move at all
HEAD = re.compile(r"^\W*my moves\b", re.I | re.M)
YOURS = re.compile(r"^\W*your moves\b", re.I)
# "1." or "1)" (indented up to 3 spaces), or a top-level "-", "*" or "•" bullet (indented at most 1)
ITEM = re.compile(r"^(?:\s{0,3}\d+[.)]|\s?[-*\u2022])\s+(?P<text>.*)")
DONE = re.compile(r"<tool-use-id>(toolu_\w+)</tool-use-id>\s*(?:<output-file>[^<]*</output-file>\s*)?<status>(completed|failed|killed|stopped|timed_out|error)", re.S)


def log(msg):
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a") as fh:
            fh.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
    except Exception:
        pass


def background_live(path):
    """Background Bash jobs, agents and Monitors launched in this transcript with no completion notice yet.
    A named agent (a teammate) counts as live for the whole session: it notifies each time it idles."""
    launched, done, teammate = set(), set(), False
    with open(path) as fh:
        for line in fh:
            if len(line) > MAX_SCAN:
                continue
            for m in DONE.finditer(line.replace("\\n", "\n")):
                done.add(m.group(1))
            if '"tool_use"' not in line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            for c in (r.get("message") or {}).get("content") or []:
                if not (isinstance(c, dict) and c.get("type") == "tool_use"):
                    continue
                inp = c.get("input") or {}
                if c.get("name") in ("Agent", "Task") and inp.get("name"):
                    teammate = True
                elif inp.get("run_in_background") or c.get("name") in ("Agent", "Task", "Monitor"):
                    launched.add(c.get("id"))
    return teammate or bool(launched - done)


def last_reply(path):
    """Assistant text after the last row that started a turn (a user prompt or a notification)."""
    texts = []
    with open(path) as fh:
        for line in fh:
            try:
                r = json.loads(line)
            except Exception:
                continue
            msg = r.get("message") or {}
            content = msg.get("content")
            if r.get("type") == "user" and not r.get("isMeta"):
                if isinstance(content, str) or (isinstance(content, list) and any(
                        isinstance(c, dict) and c.get("type") == "text" for c in content)):
                    texts = []
            elif r.get("type") == "assistant" and isinstance(content, list):
                texts += [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
    return "\n".join(texts)


def my_moves(text):
    """Numbered or bulleted items under the last 'My moves' heading, each with its continuation lines."""
    heads = list(HEAD.finditer(text))
    if not heads:
        return []
    items, cur = [], None
    for line in text[heads[-1].start():].splitlines()[1:]:
        if YOURS.match(line):
            break
        m = ITEM.match(line)
        if m:
            cur = [m.group("text")]
            items.append(cur)
        elif cur is not None and line.strip():
            cur.append(line.strip())
        elif cur is None and line.strip() and not line.strip().startswith("*"):
            break
    return [" ".join(x) for x in items]


def ready_items(items, live=False):
    """live: background work is still running, so an unmarked move may be waiting on it (not on the user)."""
    out, prev_waiting = [], False
    for it in items:
        if EMPTY.match(it):
            waiting = True
        elif OPENS_NOW.match(it):
            waiting = bool(WAIT.search(it))
        else:
            waiting = bool(WAIT.search(it) or OPENS_WAIT.match(it) or prev_waiting or live)
        if not waiting:
            out.append(it)
        prev_waiting = waiting
    return out


def main():
    try:
        hook = json.load(sys.stdin)
    except Exception as e:
        log(f"bad input: {e}")
        return 0
    if hook.get("stop_hook_active"):
        return 0
    path = hook.get("transcript_path") or ""
    try:
        text = last_reply(path) if os.path.isfile(path) else ""
    except Exception as e:
        log(f"unreadable transcript {path}: {e}")
        text = ""
    if not text:
        text = hook.get("last_assistant_message") or ""
        if not text:
            log(f"no reply found ({path or 'no transcript_path'})")
            return 0
    try:
        live = os.path.isfile(path) and background_live(path)
    except Exception as e:
        log(f"background scan failed: {e}")
        live = False
    ready = ready_items(my_moves(text), live)
    if not ready:
        return 0
    log(f"block {hook.get('session_id', '?')}: {ready[0][:120]}")
    sys.stderr.write(
        f"This My move needs nothing from {SHOWN}, so it must not wait for them:\n  " +
        "\n  ".join(r[:300] for r in ready) +
        f"\nDo it now, or rewrite it to say what it waits on ({SHOWN}, or a job that will wake this session).\n")
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        log(f"crash: {e}")
        sys.exit(0)
