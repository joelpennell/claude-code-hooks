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
    return len(s.encode("utf-8", "replace"))


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
        return "\n".join(str(b.get("text") or "") for b in content if isinstance(b, dict) and b.get("type") == "text")
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


# Outward actions: things done outside the conversation that a summary may later call "pending".
# Shell commands are tokenized like a shell would (shlex, after a pre-pass for comments, $'...' and heredocs),
# then each command is recognized by its words, so quoted text, comments and heredoc bodies are never commands.
import shlex

SCRATCH = re.compile(r"^(?:/tmp/|/private/tmp/|/var/folders/|\$TMPDIR/|\$\{TMPDIR\}/)|/scratchpad/|\.tmp(?:\.\w+)?$")
HEREDOC = re.compile(r"(?<![<\w])<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")
SEPARATORS = {";", "&&", "||", "|", "&", "\n", "|&", "(", ")", ";;", ";&", "{", "}"}
LOCAL_URL = re.compile(r"^(?:https?://)?(?:localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\]|192\.168\.|10\.\d|host\.docker\.internal)")


def _strip_heredocs(cmd):
    lines, keep, i = cmd.split("\n"), [], 0
    while i < len(lines):
        keep.append(lines[i])
        ends = [m.group(2) for m in HEREDOC.finditer(lines[i])]
        j = i + 1
        for end in ends:                         # bodies follow in order; skip each only if its terminator appears
            k = next((k for k in range(j, len(lines)) if lines[k].strip() == end), None)
            if k is None:
                break
            j = k + 1
        i = j
    return "\n".join(keep)


def _prepass(text):
    """Drop comments and turn $'...' into "..." while honoring quotes and backslashes, so shlex can do the rest."""
    out, i, n = [], 0, len(text)
    while i < n:
        c = text[i]
        if c == "\\" and i + 1 < n:
            out.append(text[i:i + 2]); i += 2; continue
        if c == "'":
            j = text.find("'", i + 1)
            j = n - 1 if j < 0 else j
            out.append(text[i:j + 1]); i = j + 1; continue
        if c == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            out.append(text[i:j + 1]); i = j + 1; continue
        if c == "$" and i + 1 < n and text[i + 1] == "'":
            j = i + 2
            while j < n and text[j] != "'":
                j += 2 if text[j] == "\\" else 1
            body = re.sub(r"\\.", "", text[i + 2:j]).replace('"', "")
            out.append('"' + body + '"'); i = j + 1; continue
        if c == "#" and (i == 0 or text[i - 1] in " \t\n;&|("):
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        out.append(c); i += 1
    return "".join(out)


def _tokens(text):
    lx = shlex.shlex(text, posix=True, punctuation_chars=";&|()<>\n")
    lx.whitespace = " \t\r"
    lx.whitespace_split = True
    lx.commenters = ""
    try:
        return list(lx)
    except ValueError:                           # an unclosed quote: fall back to plain words
        return re.findall(r"[;&|()<>\n]+|[^\s;&|()<>]+", text)


def _commands(cmd, depth=0):
    """[(words, next separator, background, exit hidden)] for every simple command in a shell string."""
    toks = _tokens(_prepass(_strip_heredocs(cmd.replace("\\\n", " "))))
    cmds, words, i = [], [], 0
    seps = []
    while i < len(toks):
        t = toks[i]
        if t in SEPARATORS:
            cmds.append(words); seps.append(t); words = []
        elif set(t) <= set("<>&") or (t.isdigit() and i + 1 < len(toks) and set(toks[i + 1]) <= set("<>&") and toks[i + 1]):
            if not t.isdigit():
                i += 1                           # a redirection: skip its target
        else:
            words.append(t)
        i += 1
    cmds.append(words); seps.append("")
    # drop empty commands (from trailing or doubled separators), keeping each command's following separator
    pairs = [(w, s) for w, s in zip(cmds, seps)]
    out = []
    for idx, (w, s) in enumerate(pairs):
        if not w:
            continue
        rest = [ss for ww, ss in pairs[idx + 1:] if ww] if idx + 1 < len(pairs) else []
        later = [s] + [ss for ww, ss in pairs[idx + 1:]][:-1] if pairs[idx + 1:] else [s]
        later = [x for k, x in enumerate([s] + [ss for ww, ss in pairs[idx + 1:]]) if any(pp[0] for pp in pairs[idx + 1 + k:])]
        # the list this command belongs to ends at the next ; or newline or &; background if that is &
        end = next((x for x in [s] + [ss for _, ss in pairs[idx + 1:]] if x in (";", "\n", "&", "")), "")
        bg = end == "&"
        hidden = s in ("|", "|&") or any(x in ("||", ";", "\n", "&") for x in later)
        out.append((w, s, bg, hidden))
        # wrappers: bash -c "...", eval ..., and $(...) inside words are parsed as commands too
        if depth < 3:
            inner = []
            base = os.path.basename(w[0])
            if base in ("bash", "sh", "zsh") and any(x.startswith("-") and "c" in x for x in w[1:2]) and len(w) > 2:
                inner.append(w[2])
            elif base == "eval" and len(w) > 1:
                inner.append(" ".join(w[1:]))
            for x in w[1:]:
                for m in re.finditer(r"\$\(([^()]*(?:\([^()]*\)[^()]*)*)\)|`([^`]*)`", x):
                    inner.append(m.group(1) or m.group(2) or "")
            for sub in inner:
                out.extend((sw, ss, sb or bg, sh or hidden) for sw, ss, sb, sh in _commands(sub, depth + 1))
    return out


PREFIX_WORDS = {"if", "then", "else", "elif", "while", "until", "do", "!", "time", "nohup", "exec", "command", "builtin", "{", "("}
OPTS_WITH_VALUE = {"sudo": {"-u", "-g", "-h", "-p", "-C", "-D", "-T", "-U", "-r", "-t"}, "env": {"-u", "-C", "-S"},
                   "timeout": {"-s", "-k", "--signal", "--kill-after"}, "nice": {"-n"}, "xargs": {"-n", "-I", "-L", "-P", "-s", "-d", "-E"}}


def _core(w):
    """Strip control words, VAR=value, and wrappers (sudo, env, timeout, nice, xargs) with their options."""
    i = 0
    while i < len(w):
        x = w[i]
        if x in PREFIX_WORDS or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", x):
            i += 1; continue
        base = os.path.basename(x)
        if base in OPTS_WITH_VALUE:
            i += 1
            while i < len(w) and w[i].startswith("-"):
                i += 2 if w[i] in OPTS_WITH_VALUE[base] else 1
            if base == "timeout" and i < len(w):
                i += 1                           # the duration
            while base == "env" and i < len(w) and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", w[i]):
                i += 1
            continue
        break
    return w[i:]


def _git_sub(w):
    i = 1
    while i < len(w) and w[i].startswith("-"):
        i += 2 if w[i] in ("-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path") else 1
    return w[i] if i < len(w) else "", w[i + 1:]


def _shell_kind(w, raw):
    """The kind of outward action a command is, or None."""
    if not w:
        return None
    base = os.path.basename(w[0])
    args = w[1:]
    flags = set(a for a in args if a.startswith("-"))
    if "--dry-run" in flags:
        return None
    if base == "git":
        sub, rest = _git_sub(w)
        rflags = set(a for a in rest if a.startswith("-"))
        if sub == "commit":
            return "commit"
        if sub == "push":
            return None if ("-n" in rflags) else "push"
        if (sub == "tag" and rest and not rflags & {"-l", "--list", "-v", "--verify"} and not rest[0].startswith("-n")) \
                or sub in ("merge", "rebase") or (sub == "reset" and "--hard" in rflags) or (sub == "branch" and rflags & {"-D", "-d", "--delete"}) \
                or (sub == "worktree" and rest[:1] == ["remove"]) or (sub == "stash" and rest[:1] in (["drop"], ["clear"])) \
                or (sub == "clean" and any(re.match(r"^-\w*f", a) for a in rest)) or (sub in ("checkout", "restore") and "--" in rest):
            return "git history"
        return None
    if base == "gh":
        if len(w) > 2 and w[1] in ("pr", "issue", "release", "repo", "workflow", "run", "gist", "secret", "variable", "label") \
                and w[2] in ("create", "merge", "close", "comment", "edit", "delete", "review", "run", "rerun", "cancel", "reopen", "set", "upload"):
            return "GitHub"
        if len(w) > 1 and w[1] == "api" and (any(re.match(r"^-X\s*(POST|PUT|PATCH|DELETE)$", a) for a in args)
                                             or any(a in ("-X", "--method") and nx in ("POST", "PUT", "PATCH", "DELETE") for a, nx in zip(args, args[1:]))
                                             or flags & {"-f", "-F", "--field", "--raw-field", "--input"}):
            return "GitHub"
        return None
    joined = " ".join(w)
    if re.search(r"\bdeploy", base) or (base in ("python", "python3", "node", "bash", "sh", "ruby", "deno", "bun") and args and re.search(r"deploy", os.path.basename(next((a for a in args if not a.startswith("-")), ""))) ) \
            or (base in ("npm", "pnpm", "yarn") and args[:1] in (["publish"],) ) or (base in ("npm", "pnpm", "yarn") and len(args) > 1 and args[0] == "run" and "deploy" in args[1]) \
            or (base in ("make", "just") and any("deploy" in a for a in args)) \
            or (base == "wrangler" and "deploy" in args) or (base == "npx" and len(args) > 1 and args[0] == "wrangler" and "deploy" in args) \
            or (base == "cf" and args[:1] == ["deploy"]) or (base == "railway" and args[:1] in (["up"], ["redeploy"])) \
            or (base == "vercel" and "--prod" in args) or (base == "fly" and args[:1] == ["deploy"]) or (base == "twine" and args[:1] == ["upload"]) \
            or (base == "docker" and args[:1] == ["push"]) or (base == "gcloud" and "deploy" in args) or (base == "terraform" and args[:1] in (["apply"], ["destroy"])) \
            or (base == "kubectl" and args[:1] in (["apply"], ["delete"], ["rollout"])):
        return "deploy"
    if base == "curl":
        writes = any(re.match(r"^-[a-zA-Z]*[dFT]", a) and not a.startswith("--") for a in args) \
            or any(a.startswith(("--data", "--json", "--form", "--upload-file")) for a in args) \
            or any(re.match(r"^-X\s*(POST|PUT|PATCH|DELETE)$", a) for a in args) \
            or any(a in ("-X", "--request") and nx in ("POST", "PUT", "PATCH", "DELETE") for a, nx in zip(args, args[1:]))
        urls = [a for a in args if re.match(r"^(https?://|[\w.-]+\.\w{2,}(/|$)|localhost|\d+\.\d+\.\d+\.\d+)", a)]
        if writes and not (urls and all(LOCAL_URL.match(u) for u in urls)):
            return "web write"
        return None
    if base in ("ssh", "scp", "sftp"):
        return "remote command"
    if base == "rsync" and any(re.search(r"^[^/\s]*[^:/]:[^:]", a) or a.startswith("rsync://") for a in args if not a.startswith("-")):
        return "remote command"
    if base in ("rm", "trash", "unlink", "rmdir", "shred"):
        targets = [a for a in args if not a.startswith("-")]
        if targets and all(SCRATCH.search(a) for a in targets):
            return None                          # temp or scratch paths only: housekeeping
        return "delete"
    if base == "find" and ("-delete" in args or any(a == "-exec" and nx in ("rm", "/bin/rm") for a, nx in zip(args, args[1:]))):
        return "delete"
    if base == "tmutil" and args[:1] and args[0].startswith("delete"):
        return "delete"
    if base == "osascript" and re.search(r"\bsend\b", raw):
        return "send"
    return None


TOOL_ACTIONS = ("SendMessage", "Artifact", "ArtifactData", "ArtifactComments", "PushNotification", "RemoteTrigger",
                "CronCreate", "CronDelete", "Workflow", "SendUserFile")
READ_ONLY_ACTIONS = ("read", "list", "open", "quickstart", "get", "query", "search")
# MCP tools: judged by the leading verb of the tool name, so list_deployments is a read and send_message is not.
MCP_WRITE_VERBS = {"send", "reply", "forward", "post", "publish", "create", "delete", "update", "deploy", "merge", "trash",
                   "redeploy", "set", "share", "schedule", "draft", "respond", "upload", "restart", "accept", "cancel",
                   "label", "unlabel", "add", "patch", "run", "request", "start", "remember", "batch", "promote", "rollback",
                   "stop", "pause", "unpause", "invalidate", "purge", "reset", "retry", "buy", "assign", "approve", "complete",
                   "activate", "revoke", "write", "edit", "remove", "move", "copy", "submit", "mark", "apply", "unmark",
                   "untrash", "connect", "generate", "issue", "replace", "restore", "stage", "test", "upsert", "kill", "extend",
                   "verify", "authorize", "record", "import", "rerequest"}
BROWSER_STRONG = re.compile(r"\b(?:button|to (?:post|send|submit|publish|tweet|reply|delete|confirm|save|buy|pay))\b|"
                            r"(?:press|hit)\s+(?:enter|return)\s+to\b", re.I)
BROWSER_VERB = re.compile(r"\b(?:post|send|submit|publish|tweet|reply|delete|remove|confirm|buy|pay|order|save|sign|accept|approve|merge|deploy|upload)\w*\b", re.I)
FOCUS_ONLY = re.compile(r"\b(?:box|field|input|textarea|tab|menu)\b", re.I)


def _short(s, n=160):
    s = " ".join(str(s).split()).encode("utf-8", "replace").decode("utf-8")
    return s if len(s) <= n else s[:n - 3] + "..."


def classify(name, inp):
    """(kind, detail, flags) list for one tool call; empty when it changes nothing outside the session.
    flags: "bg" started in the background, "masked" its exit status is hidden by a pipe or a later command."""
    if not isinstance(inp, dict):
        inp = {}
    if name == "Bash":
        cmd = str(inp.get("command") or "")
        out, assigned = [], {}
        for words, sep, bg, hidden in _commands(cmd):
            if words and all(re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", x) for x in words):
                for x in words:                  # VAR=value on its own: remember it, so "rm -rf $VAR" can be judged
                    k, v = x.split("=", 1)
                    assigned[k] = re.sub(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?", lambda m: assigned.get(m.group(1), m.group(0)), v)
                continue
            w = _core(words)
            if w and os.path.basename(w[0]) in ("rm", "trash", "unlink", "rmdir", "shred"):
                w = [re.sub(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?", lambda m: assigned.get(m.group(1), m.group(0)), x) for x in w]
            kind = _shell_kind(w, cmd)
            if not kind:
                continue
            flags = set()
            if bg or inp.get("run_in_background"):
                flags.add("bg")
            if hidden:
                flags.add("masked")
            out.append((kind, _short(" ".join(w)), flags))
        return out
    if name in TOOL_ACTIONS:
        if str(inp.get("action", "publish" if name == "Artifact" else "")) in READ_ONLY_ACTIONS:
            return []
        detail = inp.get("to") or inp.get("url") or inp.get("file_path") or inp.get("title") or inp.get("action") or ""
        return [(name, _short(f"{inp.get('summary', '') or ''} {detail}".strip()), set())]
    if name.startswith("mcp__") and not name.startswith("mcp__claude-in-chrome__"):
        verb = re.split(r"[_-]", name.split("__")[-1].lower())[0]
        if verb in MCP_WRITE_VERBS:
            return [(name.split("__")[-1], _short(json.dumps(inp, ensure_ascii=False)[:300]), set())]
        return []
    if name.startswith("mcp__claude-in-chrome__"):
        steps = inp.get("actions") if isinstance(inp.get("actions"), list) else [{"name": name.split("__")[-1], "input": inp}]
        out = []
        for st in steps:
            si = st.get("input") if isinstance(st, dict) and isinstance(st.get("input"), dict) else {}
            summ = str(si.get("action_summary") or "")
            act = str(si.get("action") or "")
            submits = "click" in act or act == "key"     # typing text or taking a screenshot changes nothing outside
            if submits and summ and (BROWSER_STRONG.search(summ) or (BROWSER_VERB.search(summ) and not FOCUS_ONLY.search(summ))):
                out.append(("browser " + (act.replace("left_", "") or "step"), _short(summ), {"batch"} if len(steps) > 1 else set()))
            elif re.search(r"\.submit\(\)|requestSubmit\(", str(si.get("text") or "")):
                out.append(("browser script", "a page script submitted a form", set()))
        return out
    return []


ERROR_OUTPUT = re.compile(r"\[rejected\]|\bfatal:|\berror:|failed to push|Permission denied|\bERROR\b|Traceback \(most recent")


def actions_taken(rows):
    """Every outward action in the session, oldest first: (call timestamp, kind, detail, status, important)."""
    try:
        results = {}
        for r in rows:
            c = (r.get("message") or {}).get("content")
            if r.get("type") == "user" and isinstance(c, list):
                for b in c:
                    if isinstance(b, dict) and b.get("type") == "tool_result" and isinstance(b.get("tool_use_id"), str):
                        content = b.get("content")
                        txt = content if isinstance(content, str) else text_of(content)
                        results[b["tool_use_id"]] = (b.get("is_error") is True, txt or "")
        out = []
        for r in rows:
            c = (r.get("message") or {}).get("content")
            if r.get("type") != "assistant" or not isinstance(c, list):
                continue
            for b in c:
                if not (isinstance(b, dict) and b.get("type") == "tool_use"):
                    continue
                found = classify(str(b.get("name") or ""), b.get("input"))
                if not found:
                    continue
                tid = b.get("id") if isinstance(b.get("id"), str) else None
                res = results.get(tid) if tid else None
                for kind, detail, flags in found:
                    if res is None:
                        status = "NO RESULT in the transcript (interrupted, or still running)"
                    else:
                        err, txt = res
                        if err:
                            status = ("FAILED or refused" + (" (the batch stopped at some step; an earlier click may have run)" if "batch" in flags else "")
                                      + ": " + _short(txt, 100))
                        elif "Command running in background with ID" in txt or "bg" in flags:
                            status = "STARTED in the background; whether it finished is a separate event"
                        elif "masked" in flags and ERROR_OUTPUT.search(txt):
                            status = "OUTPUT SHOWS AN ERROR (exit status hidden by a pipe or later command): " + _short(txt, 100)
                        elif "masked" in flags:
                            status = "ran; exit status hidden by a pipe or later command, so check its output"
                        else:
                            status = "ran without error"
                    important = not status.startswith(("ran without error", "ran; exit status hidden"))
                    out.append((r.get("timestamp", ""), kind, detail, status, important))
        return out
    except Exception:
        return []


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
    actions = actions_taken(rows)

    os.makedirs(LEDGER_DIR, mode=0o700, exist_ok=True)
    ledger_path = os.path.join(LEDGER_DIR, f"{sid}.md")
    when = ", ".join("%s at %s tokens" % (local(b.get("timestamp", "")), (b.get("compactMetadata") or {}).get("preTokens") or "?") for b in boundaries)

    full = [f"# Compaction ledger for session {sid}\n",
            f"Written {now.astimezone().strftime('%Y-%m-%d %H:%M %Z')} by the SessionStart(compact) hook, for compaction {count} of this session. "
            f"PROVISIONAL RECORD, not conclusions: verbatim text from the transcript at {path}. "
            f"Earlier compactions on disk: {len(boundaries)}{' (' + when + ')' if when else ''}.\n",
            delivered + "\n",
            f"## Actions already taken outside the conversation ({len(actions)}, oldest first, by the time each was called; "
            f"read from the tool calls and their results by pattern, so some may be missed or over-included, and an action "
            f"not listed here is not evidence that it wasn't done)\n"]
    full.extend(f"- {local(ts)} [{kind}] {detail} -> {status}" for ts, kind, detail, status, _ in actions)
    full += ["",
            f"## Everything the user typed this session ({len(users)} entries, verbatim; slash commands shown as 'ran /x')\n"]
    for ts, t in users:
        full.append(f"### {local(ts)}\n{t}\n")
    full.append("## Candidate open loops from Claude's replies before this compaction (verbatim lines; not checked, newest reply first)\n")
    for ts, lines in reversed(groups):
        full.append(f"### reply at {local(ts)}")
        full.extend(f"- {'(your move) ' if who == 'theirs' else ''}{s}" for who, s in lines)
        full.append("")
    with open(ledger_path, "w", encoding="utf-8", errors="replace") as fh:
        fh.write("\n".join(full))
    os.chmod(ledger_path, 0o600)

    # Digest for the model. Fixed parts first, then fill to the cap: newest open loops, then newest user messages.
    head = (f"[compaction-ledger] This session was just compacted (compaction {count} of this session; "
            f"{now.astimezone().strftime('%H:%M %Z')}). The summary above is the model's digest; this is the record. "
            f"{delivered}"
            f"Full ledger (actions already taken, {len(actions)}; everything the user typed, {len(users)} entries; and Claude's candidate open loops): {ledger_path}. "
            f"Transcript (word for word, survives compaction): {path}.\n"
            f"Before continuing, in three lines: what the summary dropped (compare with the ledger), whether the last reply "
            f"before the compaction had gone out (see above), and what is still open. For the third line, read the ledger's "
            f"open-loop section in full (the list below is cut to fit) and name every promise of Claude's that the transcript "
            f"does not show done, or say it was dropped on purpose. The actions list below is what the session already did outside "
            f"the conversation: before redoing anything on it that the summary calls pending, check whether it took effect, "
            f"because doing it again could do it twice. 'ran without error' means the tool succeeded, not that the effect was checked. "
            f"Then keep working; no pre-wrap or wrap "
            f"unless the user asks. These lines are a record, not decisions.\n")
    loop_hdr = "Candidate open loops, verbatim and NOT checked (older items may be closed since; check the transcript):\n"
    msg_hdr_tpl = "The user's messages, newest first ({shown} of {total} shown; the rest are in the ledger):\n"
    budget = DIGEST_MAX - blen(head) - blen(loop_hdr) - blen(msg_hdr_tpl.format(shown=999, total=999)) - 10
    act_hdr = "Actions already taken (oldest first; status from the tool result):\n"
    budget -= blen(act_hdr)

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
    # Actions first: a summary that calls a done push or send "pending" is the costliest error (it gets done twice).
    # Up to a third of the room; if they don't fit, keep the oldest and newest and say how many are cut.
    # Anything not "ran without error" (failed, refused, unfinished, hidden exit status) is listed first and in full,
    # so a cut can never hide it; repeats of the same action in a row are folded into one line with a count.
    def entries(sel):
        out, prev, n = [], None, 0
        for ts, kind, detail, status, imp in actions:
            if imp != sel:
                continue
            key = (kind, detail, status)
            if key == prev:
                n += 1
                out[-1] = f"- [{local(first_ts)}] {kind}: {detail} -> {status} (x{n})\n"
                continue
            prev, n, first_ts = key, 1, ts
            out.append(f"- [{local(ts)}] {kind}: {detail} -> {status}\n")
        return out
    act_block = ""
    room = budget // 3
    if actions:
        flagged, plain = entries(True), entries(False)
        shown_f, _ = fit(flagged, room // 2) if flagged else ([], 0)
        shown_p, _ = fit(plain, room - blen("".join(shown_f))) if plain else ([], 0)
        body = (("Failed, refused, unfinished, or output showing an error:\n" + "".join(shown_f)) if shown_f else "") + \
               (("Ran (status as shown):\n" + "".join(shown_p)) if shown_p else "")
        act_block = act_hdr + body
        budget -= blen(body)
    else:
        budget += blen(act_hdr)
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

    out = head + act_block + loop_block + msg_block
    sys.stdout.write(out.encode("utf-8", "replace")[:DIGEST_MAX].decode("utf-8", "ignore"))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # never block the session
        print(f"[compaction-ledger] hook error, nothing injected: {e!r}")
        sys.exit(0)
