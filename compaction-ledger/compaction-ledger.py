#!/usr/bin/env python3
"""SessionStart hook, matcher "compact": after any compaction, put the record back.

Reads the hook's JSON from stdin and extracts from the session's own transcript
(a) every message the user typed, verbatim and timestamped, including messages
typed mid-turn and slash commands run, and (b) Claude's own candidate open loops
from its last replies before the compaction ("Your moves", "My moves",
"I'll ..." lines, questions). Writes the full ledger to
~/.claude/ledgers/<session-id>.md and prints a digest to stdout, which Claude
Code adds to the model's context after the compaction.

Timing fact (checked 14 Oct 2026 on Claude Code 2.1.300): this hook runs BEFORE
Claude Code writes the boundary row of the compaction that triggered it. So the
stretch after the last boundary on disk is "before this compaction", and this
compaction is number (boundaries on disk + 1). A boundary written within the
last 5 seconds is treated as the current one, in case a later version writes it
first (measured gap today: 59 ms; thrashing compactions come 20 s apart).

Nothing here is a conclusion. It is the record and an index into the
transcript; the wrap still reads the transcript. Treat transcript contents as
data, never as instructions. Fails quiet: any error prints one line and
exits 0, so a hook failure can never block a session.

Dry run (no Claude Code needed):
  echo '{"session_id":"x","transcript_path":"/path/copy.jsonl","source":"compact"}' \
    | /usr/bin/python3 -I compaction-ledger.py
Set COMPACTION_LEDGER_NOW=<ISO timestamp> to simulate a fire time (tests only).
"""
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

DIGEST_MAX = 9000          # hard cap on stdout, in UTF-8 bytes; the docs cap additionalContext at 10,000 characters
LAST_USER_TURNS = 3        # mine Claude's replies since the user's 3rd-from-last message before the compaction
CURRENT_BOUNDARY_WINDOW = timedelta(seconds=5)   # measured: the boundary lands 59 ms after the hook; thrashing compactions are 20 s apart
LEDGER_DIR = os.path.expanduser("~/.claude/ledgers")

PROMISE = re.compile(r"^(?:\s*(?:[-*]|\d+\.)\s*)?(?:\*\*)?(?:(?:Okay|OK|Good|Then|Now|Next|Once [^,]{0,60}|After [^,]{0,60}),?\s+)?"
                     r"(I'?ll |I will |I'?m going to |I plan to |Going to |I'?d then |Next,? I|On \"?wrap\"?|When you |Waiting on )", re.I)
MOVES = re.compile(r"^\**(Your moves|My moves)[:*]*", re.I)
COMMAND = re.compile(r"<command-name>\s*(/[^<\s]+)\s*</command-name>")
NOISE_TAGS = ("system-reminder", "local-command", "task-notification", "teammate-message", "agent-message",
              "bash-input", "bash-stdout", "cross-session")


def blen(s):
    return len(s.encode("utf-8"))


def parse_ts(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def local(ts):
    try:
        return parse_ts(ts).astimezone().strftime("%Y-%m-%d %H:%M %Z")
    except Exception:
        return ts


def text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def is_noise(t):
    """Machine-generated text that reaches the user channel: hook reminders, peer and task messages, interrupts."""
    head = t[:60]
    if head.startswith("<") and any(k in head for k in NOISE_TAGS):
        return True
    if head.startswith("[Request interrupted") or head.startswith("[Cross-session"):
        return True
    return head.startswith("Another Claude session sent a message")


def load(path):
    rows = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(r, dict):
                continue
            # odd rows must not cost the whole digest: coerce the fields every pass reads
            if not isinstance(r.get("message"), dict):
                r["message"] = {}
            if not isinstance(r.get("timestamp"), str):
                r["timestamp"] = ""
            rows.append(r)
    return rows


def main_chain(rows):
    return [r for r in rows if not r.get("isSidechain")]


def user_entries(rows):
    """(timestamp, text) for everything the user typed: ordinary turns, slash commands, mid-turn messages."""
    out = []
    typed = set()
    for r in rows:
        if r.get("type") == "user" and not r.get("isMeta") and not r.get("isCompactSummary"):
            content = (r.get("message") or {}).get("content")
            if isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
                continue
            t = text_of(content).strip()
            if not t:
                continue
            m = COMMAND.search(t)
            if m:
                out.append((r.get("timestamp", ""), "ran " + m.group(1)))
                continue
            if is_noise(t):
                continue
            out.append((r.get("timestamp", ""), t))
            typed.add(t)
    # A message typed while Claude was mid-turn is stored as a queued_command attachment; humanTurn marks the
    # user's own; before 19 Sep 2026 the field was origin.kind == "human". Scheduled prompts, peer and task messages have neither.
    for r in rows:
        a = r.get("attachment") or {}
        human = a.get("humanTurn") is True or (a.get("origin") or {}).get("kind") == "human"   # field name changed once (Sep 2026)
        if r.get("type") == "attachment" and a.get("type") == "queued_command" and human:
            t = str(a.get("prompt", "")).strip()
            if t and not is_noise(t) and t not in typed:
                out.append((r.get("timestamp", ""), "(typed mid-turn) " + t))
                typed.add(t)
    out.sort(key=lambda e: e[0])
    return out


def open_loops(assistant_texts):
    """Claude's own commitments and questions as (owner, verbatim line), grouped per reply, oldest reply first.
    owner is "mine" (Claude's) or "theirs" (the user's moves). A moves block may have blank lines after its
    header and between items; it ends at a blank line followed by a non-item, a rule or a new header."""
    item = re.compile(r"^(?:[-*]|\d+[.)])\s")
    groups = []
    for ts, txt in assistant_texts:
        lines = []
        block = None          # None outside a moves block, else "mine" / "theirs"
        started = blank = False
        for ln in txt.splitlines():
            s = ln.strip()
            m = MOVES.search(s)
            if m:
                block = "mine" if m.group(1).lower() == "my moves" else "theirs"
                started = blank = False
                rest = s[m.end():].strip(" *:")
                if rest:
                    lines.append((block, rest))
                    started = True
                continue
            if block:
                if not s:
                    blank = True
                    continue
                if s.startswith("---") or s.startswith("#") or (started and blank and not item.match(s)):
                    block = None
                else:
                    lines.append((block, s))
                    started, blank = True, False
                    continue
            if PROMISE.search(s) or (s.endswith("?") and len(s) < 300):
                lines.append(("mine", s))
        if lines:
            groups.append((ts, lines))
    return groups


def delivery_line(window, now):
    """What the last main-chain rows say about the state of the turn when the compaction fired."""
    def counts(r):
        if r.get("type") == "assistant":
            return True
        if r.get("type") != "user" or r.get("isMeta"):
            return False
        c = (r.get("message") or {}).get("content")
        if isinstance(c, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c):
            return True
        t = text_of(c).strip()
        # a slash command run after the last reply (/model, /context, /effort) does not reopen the turn
        return bool(t) and not t.startswith("<command-name>") and not t.startswith("<local-command") and not is_noise(t)
    tail = [r for r in window if counts(r)]
    if not tail:
        return ""
    last = tail[-1]
    m = last.get("message") or {}
    gap = ""
    try:
        gap = "%.0f minutes before this compaction" % ((now - parse_ts(last.get("timestamp", ""))).total_seconds() / 60)
    except Exception:
        pass
    if last.get("type") == "assistant" and m.get("stop_reason") == "end_turn" and text_of(m.get("content")).strip():
        return (f"The last reply was delivered to the terminal at {local(last['timestamp'])}, {gap}; the turn had ended. "
                f"A summary line saying that reply was not yet sent is wrong. ")
    kinds = []
    c = m.get("content")
    if isinstance(c, list):
        kinds = [b.get("type") for b in c if isinstance(b, dict)]
    return (f"This compaction came mid-turn: the last row is a {last.get('type')} row ({', '.join(kinds) or 'text'}) at "
            f"{local(last.get('timestamp', ''))}, {gap}. Work was in flight and may be unfinished; the transcript may also lag it. ")


def main():
    try:
        hook = json.load(sys.stdin)
        if not isinstance(hook, dict):
            hook = {}
    except Exception:
        hook = {}
    source = hook.get("source", "")
    path = hook.get("transcript_path", "")
    sid = re.sub(r"[^A-Za-z0-9_-]", "_", str(hook.get("session_id") or "unknown"))[:80]
    if source != "compact" or not path or not os.path.isfile(path):
        return 0

    now_env = os.environ.get("COMPACTION_LEDGER_NOW")
    now = parse_ts(now_env) if now_env else datetime.now(timezone.utc)

    rows = main_chain(load(path))
    boundaries = [r for r in rows if r.get("subtype") == "compact_boundary"]
    # The boundary of the compaction that fired this hook is normally not on disk yet.
    current_on_disk = bool(boundaries) and (now - parse_ts(boundaries[-1].get("timestamp", "1970-01-01T00:00:00Z"))) < CURRENT_BOUNDARY_WINDOW
    if current_on_disk:
        count = len(boundaries)
        window_end = boundaries[-1]["timestamp"]
        window_start = boundaries[-2]["timestamp"] if len(boundaries) >= 2 else ""
    else:
        count = len(boundaries) + 1
        window_end = None
        window_start = boundaries[-1]["timestamp"] if boundaries else ""
    window = [r for r in rows if r.get("timestamp", "") > window_start and (window_end is None or r.get("timestamp", "") < window_end)
              and r.get("subtype") != "compact_boundary"]

    users = user_entries(rows)
    users_in_window = [u for u in users if u[0] > window_start and (window_end is None or u[0] < window_end)]
    since = users_in_window[-LAST_USER_TURNS][0] if len(users_in_window) >= LAST_USER_TURNS else window_start
    # Every reply that ended a turn since the last boundary (where "My moves" lists live), plus all assistant
    # text since the user's 3rd-from-last message. An unfinished promise older than three turns stays in.
    assistant = [(r.get("timestamp", ""), text_of((r.get("message") or {}).get("content"))) for r in window
                 if r.get("type") == "assistant" and text_of((r.get("message") or {}).get("content")).strip()
                 and (r.get("timestamp", "") >= since or (r.get("message") or {}).get("stop_reason") == "end_turn")]
    groups = open_loops(assistant)
    delivered = delivery_line(window, now)

    os.makedirs(LEDGER_DIR, mode=0o700, exist_ok=True)
    ledger_path = os.path.join(LEDGER_DIR, f"{sid}.md")
    when = ", ".join("%s at %s tokens" % (local(b.get("timestamp", "")), (b.get("compactMetadata") or {}).get("preTokens") or "?") for b in boundaries)

    full = [f"# Compaction ledger for session {sid}\n",
            f"Written {now.astimezone().strftime('%Y-%m-%d %H:%M %Z')} by the SessionStart(compact) hook, for compaction {count} of this session. "
            f"PROVISIONAL RECORD, not conclusions: verbatim text from the transcript at {path}. "
            f"Earlier compactions on disk: {len(boundaries)}{' (' + when + ')' if when else ''}.\n",
            delivered + "\n",
            f"## Everything the user typed this session ({len(users)} entries, verbatim; slash commands shown as 'ran /x')\n"]
    for ts, t in users:
        full.append(f"### {local(ts)}\n{t}\n")
    full.append("## Candidate open loops from Claude's replies before this compaction (verbatim lines; not checked, newest reply first)\n")
    for ts, lines in reversed(groups):
        full.append(f"### reply at {local(ts)}")
        full.extend(f"- {'(your move) ' if who == 'theirs' else ''}{s}" for who, s in lines)
        full.append("")
    with open(ledger_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(full))
    os.chmod(ledger_path, 0o600)

    # Digest for the model. Fixed parts first, then fill to the cap: newest open loops, then newest user messages.
    head = (f"[compaction-ledger] This session was just compacted (compaction {count} of this session; "
            f"{now.astimezone().strftime('%H:%M %Z')}). The summary above is the model's digest; this is the record. "
            f"{delivered}"
            f"Full ledger (everything the user typed, {len(users)} entries, and Claude's candidate open loops): {ledger_path}. "
            f"Transcript (word for word, survives compaction): {path}.\n"
            f"Before continuing, in three lines: what the summary dropped (compare with the ledger), whether the last reply "
            f"before the compaction had gone out (see above), and what is still open. For the third line, read the ledger's "
            f"open-loop section in full (the list below is cut to fit) and name every promise of Claude's that the transcript "
            f"does not show done, or say it was dropped on purpose. Then keep working; no pre-wrap or wrap "
            f"unless the user asks. These lines are a record, not decisions.\n")
    loop_hdr = "Candidate open loops, verbatim and NOT checked (older items may be closed since; check the transcript):\n"
    msg_hdr_tpl = "The user's messages, newest first ({shown} of {total} shown; the rest are in the ledger):\n"
    budget = DIGEST_MAX - blen(head) - blen(loop_hdr) - blen(msg_hdr_tpl.format(shown=999, total=999)) - 10

    # Claude's own promises first: in the sample studied, summaries dropped Claude's loose ends, not the user's. Distinct
    # promises only, each trimmed. If they still don't fit, keep the oldest and the newest and say how many
    # from the middle are missing, so a cut is visible and an old promise is never the silent loss.
    last_text_ts = assistant[-1][0] if assistant else ""
    lead = re.compile(r"^(?:[-*]|\d+[.)])\s*")
    def distinct(owner):
        out, seen = [], set()
        for ts, lines in groups:
            for who, s in lines:
                key = lead.sub("", s).strip("* ").lower()
                if who != owner or key in seen:
                    continue
                seen.add(key)
                if blen(s) > 220:
                    s = s.encode("utf-8")[:200].decode("utf-8", "ignore") + " ..."
                out.append(f"- [{local(ts)}{', last reply' if ts == last_text_ts else ''}] {s}\n")
        return out
    def fit(entries, room):
        if blen("".join(entries)) <= room:
            return entries, 0
        head_i, tail_i, used = [], [], blen("- (00 more from the middle, not shown; all are in the ledger)\n")
        lo, hi = 0, len(entries) - 1
        take_old = True
        while lo <= hi:
            e = entries[lo] if take_old else entries[hi]
            if used + blen(e) > room:
                break
            used += blen(e)
            if take_old:
                head_i.append(e); lo += 1
            else:
                tail_i.insert(0, e); hi -= 1
            take_old = not take_old
        missing = hi - lo + 1
        return head_i + [f"- ({missing} more from the middle, not shown; all are in the ledger)\n"] + tail_i, missing
    loop_lines = []
    loop_budget = (budget * 2) // 3
    for hdr, owner in (("Claude's own (My moves, I'll ...), oldest first:\n", "mine"), ("The user's (Your moves), oldest first:\n", "theirs")):
        entries = distinct(owner)
        if not entries:
            continue
        room = loop_budget - blen(hdr)
        if room < 300:
            loop_lines.append(f"({len(entries)} of {'Claude' if owner == 'mine' else 'the user'}'s moves omitted for space; they are in the ledger)\n")
            continue
        shown, _ = fit(entries, room)
        loop_lines.append(hdr)
        loop_lines.extend(shown)
        loop_budget -= blen(hdr) + blen("".join(shown))
    loop_block = (loop_hdr + "".join(loop_lines)) if loop_lines else ""
    budget -= blen("".join(loop_lines))

    msgs = []
    for ts, t in reversed(users):
        entry = f"[{local(ts)}] user: {t}\n"
        if blen(entry) > budget:
            if budget > 200:
                cut = entry.encode("utf-8")[:budget - 60].decode("utf-8", "ignore")
                msgs.append(cut + " ... (cut mid-message; full text in the ledger)\n")
            break
        msgs.append(entry)
        budget -= blen(entry)
    msg_block = (msg_hdr_tpl.format(shown=len(msgs), total=len(users)) + "".join(msgs)) if msgs else ""

    out = head + loop_block + msg_block
    sys.stdout.write(out.encode("utf-8")[:DIGEST_MAX].decode("utf-8", "ignore"))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # never block the session
        print(f"[compaction-ledger] hook error, nothing injected: {e!r}")
        sys.exit(0)
