"""Runs each hook as Claude Code would: JSON on stdin, then checks stdout, stderr and the exit code.

HOME points at a temporary folder for every run, so nothing is written to your real ~/.claude.
Run with: ./tests/run.sh
"""
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIX = os.path.join(ROOT, "tests", "fixtures")


def run(script, payload, extra_env=None, home=None):
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": home or tempfile.mkdtemp()}
    env.update(extra_env or {})
    data = payload if isinstance(payload, str) else json.dumps(payload)
    p = subprocess.run([sys.executable, "-I", os.path.join(ROOT, script)], input=data,
                       capture_output=True, text=True, env=env, timeout=30)
    return p.returncode, p.stdout, p.stderr


def transcript(rows):
    """Write rows as a JSONL transcript and return its path."""
    fd, path = tempfile.mkstemp(suffix=".jsonl")
    with os.fdopen(fd, "w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    return path


def user(ts, text):
    return {"type": "user", "timestamp": ts, "message": {"role": "user", "content": text}}


def reply(ts, text, stop="end_turn"):
    return {"type": "assistant", "timestamp": ts,
            "message": {"role": "assistant", "stop_reason": stop, "content": [{"type": "text", "text": text}]}}


class PkillGuard(unittest.TestCase):
    S = "pkill-guard/pkill-guard.py"

    def decide(self, cmd):
        code, out, _ = run(self.S, {"tool_name": "Bash", "tool_input": {"command": cmd}})
        self.assertEqual(code, 0)
        return json.loads(out)["hookSpecificOutput"]["permissionDecision"] if out.strip() else "allow"

    def test_trailing_flag_refused(self):
        self.assertEqual(self.decide('pkill -f "gh run watch 123" -n'), "deny")

    def test_flag_before_pattern_allowed(self):
        self.assertEqual(self.decide('pkill -n -f "gh run watch 123"'), "allow")

    def test_short_full_match_pattern_refused(self):
        self.assertEqual(self.decide("pgrep -f ab"), "deny")

    def test_killall_trailing_flag_refused_inside_a_chain(self):
        self.assertEqual(self.decide("echo hi && killall Finder -9"), "deny")

    def test_killall_flag_first_allowed(self):
        self.assertEqual(self.decide("killall -9 Finder"), "allow")

    def test_option_value_is_not_a_pattern(self):
        self.assertEqual(self.decide("pkill -u nobody -f long-pattern"), "allow")

    def test_multiline_commands_are_split(self):
        self.assertEqual(self.decide('echo hi\npkill -f "gh run watch 123" -n'), "deny")
        self.assertEqual(self.decide("pkill -f foo_bar\nls -la"), "allow")

    def test_wrappers_and_shells_are_looked_through(self):
        for c in ('bash -c "pkill -f abc_def -n"', 'sudo -u root pkill -f "abc def" -n',
                  'sudo -n pkill -f "abc def" -n', 'env A=1 pkill -f "abc def" -n',
                  'timeout 5 pkill -f "abc def" -n', 'PKILL -f "abc def" -n'):
            self.assertEqual(self.decide(c), "deny", c)

    def test_signal_names_take_no_value(self):
        self.assertEqual(self.decide("pkill -HUP -f ab"), "deny")
        self.assertEqual(self.decide("pkill -INT -f x"), "deny")
        self.assertEqual(self.decide('pkill -HUP -f "gh run watch 123"'), "allow")
        self.assertEqual(self.decide('pkill -9 -f "gh run watch 123"'), "allow")
        self.assertEqual(self.decide("killall -HUP Dock"), "allow")

    def test_unrelated_and_bad_input_pass(self):
        self.assertEqual(self.decide("ls -la"), "allow")
        code, out, _ = run(self.S, "not json")
        self.assertEqual((code, out), (0, ""))


class ReadyMoveStop(unittest.TestCase):
    S = "ready-move-stop/ready-move-stop.py"

    def stop(self, text, extra_rows=(), env=None, active=False):
        rows = [user("2026-10-30T10:00:00Z", "go")] + list(extra_rows) + [reply("2026-10-30T10:01:00Z", text)]
        return run(self.S, {"transcript_path": transcript(rows), "stop_hook_active": active, "session_id": "t"}, env)

    def test_ready_move_blocks(self):
        code, _, err = self.stop("Done.\n\n**My moves:**\n1. Run the test suite.")
        self.assertEqual(code, 2)
        self.assertIn("Run the test suite", err)

    def test_stated_wait_passes(self):
        code, _, _ = self.stop("**My moves:**\n1. When you reply, deploy it.\n2. Then tag the release.")
        self.assertEqual(code, 0)

    def test_meanwhile_after_a_wait_blocks(self):
        code, _, _ = self.stop("**My moves:**\n1. When you reply, deploy it.\n2. Meanwhile, update the docs.")
        self.assertEqual(code, 2)

    def test_paren_numbers_and_bullets_are_read(self):
        self.assertEqual(self.stop("**My moves:**\n1) Run the test suite.")[0], 2)
        self.assertEqual(self.stop("**My moves:**\n- Run the test suite.")[0], 2)
        self.assertEqual(self.stop("**My moves:**\n- When you reply, deploy it.")[0], 0)

    def test_nested_detail_stays_with_its_item(self):
        code, _, _ = self.stop("**My moves:**\n1. When you reply, deploy it.\n   - check the logs first")
        self.assertEqual(code, 0)

    def test_nothing_is_no_move(self):
        self.assertEqual(self.stop("**My moves:**\n1. Nothing.")[0], 0)

    def test_no_my_moves_passes(self):
        self.assertEqual(self.stop("Here is the answer.")[0], 0)

    def test_loop_guard(self):
        self.assertEqual(self.stop("**My moves:**\n1. Run the test suite.", active=True)[0], 0)

    def test_running_background_agent_makes_unmarked_move_wait(self):
        launch = {"type": "assistant", "timestamp": "2026-10-30T10:00:30Z", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "toolu_abc", "name": "Agent", "input": {"prompt": "review"}}]}}
        self.assertEqual(self.stop("**My moves:**\n1. Fold in the review's findings.", [launch])[0], 0)

    def test_owner_name_from_environment(self):
        text = "**My moves:**\n1. On Sam's yes, merge the branch."
        self.assertEqual(self.stop(text)[0], 2)
        self.assertEqual(self.stop(text, env={"READY_MOVE_OWNER": "Sam"})[0], 0)

    def test_missing_transcript_fails_open_and_logs(self):
        home = tempfile.mkdtemp()
        code, _, _ = run(self.S, {"transcript_path": "/nonexistent.jsonl"}, home=home)
        self.assertEqual(code, 0)
        self.assertTrue(os.path.exists(os.path.join(home, ".claude", "logs", "ready-move-hook.log")))


class CompactionLedger(unittest.TestCase):
    S = "compaction-ledger/compaction-ledger.py"

    def test_digest_and_ledger(self):
        path = os.path.join(FIX, "compaction-transcript.jsonl")
        home = tempfile.mkdtemp()
        code, out, _ = run(self.S, {"session_id": "fixture", "transcript_path": path, "source": "compact"},
                           {"COMPACTION_LEDGER_NOW": "2026-10-30T11:00:00Z"}, home)
        self.assertEqual(code, 0)
        self.assertIn("[compaction-ledger]", out)
        self.assertIn("compaction 2 of this session", out)                  # one boundary on disk, plus this one
        self.assertIn("the turn had ended", out)                            # the last reply had gone out
        self.assertIn("I'll write the migration notes", out)                # Claude's own promise
        self.assertIn("(typed mid-turn) also check the logs", out)          # a message typed mid-turn
        self.assertNotIn("I'll do the old thing", out)                   # a promise from before the last compaction
        ledger = os.path.join(home, ".claude", "ledgers", "fixture.md")
        self.assertTrue(os.path.isfile(ledger))
        self.assertEqual(stat.S_IMODE(os.stat(ledger).st_mode), 0o600)
        with open(ledger) as fh:
            self.assertIn("before the first compaction", fh.read())         # every user message is in the ledger

    def test_actions_taken(self):
        def call(ts, tid, name, inp):
            return {"type": "assistant", "timestamp": ts, "message": {"role": "assistant", "stop_reason": "tool_use",
                    "content": [{"type": "tool_use", "id": tid, "name": name, "input": inp}]}}
        def result(ts, tid, text, err=False):
            return {"type": "user", "timestamp": ts, "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": tid, "content": text, "is_error": err}]}}
        rows = [
            user("2026-11-04T10:00:00Z", "ship it"),
            call("2026-11-04T10:01:00Z", "a1", "Bash", {"command": "cd ~/repo && git commit -qm 'x' && git push -q"}),
            result("2026-11-04T10:01:05Z", "a1", "abc1234 x"),
            call("2026-11-04T10:02:00Z", "a2", "Bash", {"command": "git push origin main"}),
            result("2026-11-04T10:02:05Z", "a2", "Exit code 1\nrejected", err=True),
            call("2026-11-04T10:03:00Z", "a3", "Bash", {"command": "./deploy.sh && wrangler pages deploy public", "run_in_background": True}),
            result("2026-11-04T10:03:01Z", "a3", "Command running in background with ID: b1."),
            call("2026-11-04T10:04:00Z", "a4", "SendMessage", {"to": "peer-session", "summary": "report done", "message": "hi"}),
            result("2026-11-04T10:04:01Z", "a4", "ok"),
            call("2026-11-04T10:05:00Z", "a5", "mcp__claude-in-chrome__browser_batch", {"actions": [
                {"name": "computer", "input": {"action": "screenshot", "tabId": 1}},
                {"name": "computer", "input": {"action": "left_click", "tabId": 1, "action_summary": "Click Reply to post the approved reply"}}]}),
            result("2026-11-04T10:05:03Z", "a5", "Clicked"),
            call("2026-11-04T10:06:00Z", "a6", "Bash", {"command": "git status --short && git log --oneline -3"}),
            result("2026-11-04T10:06:01Z", "a6", "clean"),
            call("2026-11-04T10:07:00Z", "a7", "Bash", {"command": "rm -rf /private/tmp/claude-501/x/scratchpad/r4"}),
            result("2026-11-04T10:07:01Z", "a7", ""),
            call("2026-11-04T10:08:00Z", "a8", "Artifact", {"action": "read", "url": "https://claude.ai/artifact/x"}),
            result("2026-11-04T10:08:01Z", "a8", "page"),
            call("2026-11-04T10:08:30Z", "a9", "Bash", {"command": "S=/private/tmp/x/scratchpad/m; rm -rf $S; mkdir -p $S\npython3 - <<'EOF'\nprint('git push and git commit inside a script')\nEOF\ngit tag v1.2"}),
            result("2026-11-04T10:08:31Z", "a9", ""),
            call("2026-11-04T10:08:40Z", "a10", "mcp__claude-in-chrome__computer", {"action": "left_click", "tabId": 1, "action_summary": "Click the 'Post your reply' box"}),
            result("2026-11-04T10:08:41Z", "a10", "Clicked"),
            reply("2026-11-04T10:09:00Z", "Pushed and posted.\n\n**My moves:**\n1. Nothing."),
        ]
        home = tempfile.mkdtemp()
        code, out, _ = run(self.S, {"session_id": "acts", "transcript_path": transcript(rows), "source": "compact"},
                           {"COMPACTION_LEDGER_NOW": "2026-11-04T11:00:00Z"}, home)
        self.assertEqual(code, 0)
        self.assertIn("Actions already taken", out)
        self.assertIn("do it twice", out)
        self.assertIn("commit: git commit -qm x -> ran without error", out)          # each command its own line
        self.assertIn("push: git push -q -> ran without error", out)
        self.assertRegex(out, r"push: git push origin main -> FAILED")             # a failed push is not "done"
        self.assertRegex(out, r"deploy: wrangler pages deploy public -> STARTED")        # a background job only started
        self.assertRegex(out, r"SendMessage: report done peer-session -> ran without error")
        self.assertRegex(out, r"browser click: Click Reply to post the approved reply -> ran without error")
        self.assertNotIn("git status", out)                                         # reads are not actions
        self.assertNotIn("screenshot", out)
        self.assertNotIn("scratchpad", out)                                         # temp-file cleanup is not an action
        self.assertNotIn("artifact/x", out)                                         # reading an artifact is not an action
        with open(os.path.join(home, ".claude", "ledgers", "acts.md")) as fh:
            ledger = fh.read()
        self.assertIn("## Actions already taken outside the conversation (8,", ledger)   # incl. the tag after a heredoc
        self.assertIn("[git history] git tag v1.2", ledger)
        self.assertNotIn("inside a script", ledger)                                  # heredoc bodies are data
        self.assertNotIn("Post your reply' box", ledger)                             # a focus click is not an action

    def test_actions_checker_cases(self):
        """Inputs from the independent check of 4 Nov 2026 that broke the first version."""
        cases = [  # (tool name, input, result text, is_error, expected substring or None if it must not be listed)
            ("Bash", {"command": "git push 2>&1 | tail -3"}, "! [rejected] main -> main\nerror: failed to push", False, "push: git push -> OUTPUT SHOWS AN ERROR"),
            ("Bash", {"command": "git push origin dev | tail -1"}, "ok", False, "push: git push origin dev -> ran; exit status hidden"),
            ("Bash", {"command": "echo 'a; git push quoted'"}, "", False, None),
            ("Bash", {"command": 'git commit -m "title\n\ngit push origin inmsg\nrm -rf inmsg"'}, "", False, "commit: git commit -m"),
            ("Bash", {"command": "cat <<< 'here'\ngit push origin herestring"}, "", False, "push: git push origin herestring"),
            ("Bash", {"command": "git -c user.name=x commit -m globalopt"}, "", False, "commit: git -c user.name=x commit"),
            ("Bash", {"command": "git --no-pager push origin nopager"}, "", False, "push: git --no-pager push origin nopager"),
            ("Bash", {"command": "git push --dry-run origin dryrun"}, "", False, None),
            ("Bash", {"command": "rm -rf ~/Projects/realdir /tmp/y"}, "", False, "delete: rm -rf ~/Projects/realdir"),
            ("Bash", {"command": "git \\\n  push origin continued"}, "", False, "push: git push origin continued"),
            ("Bash", {"command": "git push origin amp &"}, "", False, "push: git push origin amp -> STARTED"),
            ("Bash", {"command": "git push origin hooks"}, "remote: Running in background hooks", False, "push: git push origin hooks -> ran without error"),
            ("mcp__claude_ai_Gmail__list_drafts", {"q": "readonly"}, "", False, None),
            ("mcp__railway__set-variables", {"service": "api"}, "", False, "set-variables: {"),
            ("mcp__claude-in-chrome__computer", {"action": "left_click", "action_summary": "Click Tweet button"}, "", False, "browser click: Click Tweet button"),
            ("mcp__claude-in-chrome__computer", {"action": "key", "action_summary": "Press Enter to send message"}, "", False, "browser key: Press Enter to send message"),
            ("mcp__claude-in-chrome__computer", {"action": "left_click", "action_summary": "Click the 'Post your reply' box"}, "", False, None),
            ("Bash", {"command": "git push origin strerr"}, "", "false", "push: git push origin strerr -> ran without error"),
        ]
        rows, absent = [user("2026-11-04T10:00:00Z", "go")], []
        for i, (name, inp, res, err, want) in enumerate(cases):
            ts = "2026-11-04T10:%02d:00Z" % (i + 1)
            rows.append({"type": "assistant", "timestamp": ts, "message": {"role": "assistant", "stop_reason": "tool_use",
                         "content": [{"type": "tool_use", "id": "c%d" % i, "name": name, "input": inp}]}})
            rows.append({"type": "user", "timestamp": ts, "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "c%d" % i, "content": res, "is_error": err}]}})
        rows.append({"type": "assistant", "timestamp": "2026-11-04T10:40:00Z", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": ["odd"], "name": "Bash", "input": {"command": "git push origin oddid"}}]}})   # must not crash
        rows.append(reply("2026-11-04T10:41:00Z", "Done."))
        home = tempfile.mkdtemp()
        code, out, _ = run(self.S, {"session_id": "chk", "transcript_path": transcript(rows), "source": "compact"},
                           {"COMPACTION_LEDGER_NOW": "2026-11-04T11:00:00Z"}, home)
        self.assertEqual(code, 0)
        with open(os.path.join(home, ".claude", "ledgers", "chk.md")) as fh:
            ledger = fh.read()
        for name, inp, res, err, want in cases:
            if want:
                self.assertIn("[" + want.replace(": ", "] ", 1), ledger, msg=str(inp))   # the ledger writes "[kind] detail"
        for gone in ("quoted", "[push] git push origin inmsg", "[delete] rm -rf inmsg", "dryrun", "readonly", "Post your reply"):
            self.assertNotIn(gone, ledger.split("## Everything the user typed")[0], msg=gone)
        self.assertIn("oddid", ledger)                                               # odd id: listed, NO RESULT, no crash

    def test_shell_parsing_cases(self):
        """Inputs from the second independent check (4 Nov 2026): quotes, escapes, comments, wrappers, control flow."""
        import importlib.util
        spec = importlib.util.spec_from_file_location("cl", os.path.join(ROOT, self.S))
        cl = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cl)
        C = [  # command, expected list of (kind, has bg, has masked)
         ('echo "a \\" b" ; git push origin main', [("push",0,0)]),
         ('echo "foo \\" ; git push \\" bar"', []),
         ("git commit -m 'it'\\''s; git push'", [("commit",0,0)]),
         ("git commit -m $'it\\'s ok' && git push", [("commit",0,0),("push",0,0)]),
         ("# don't panic\ngit push origin main", [("push",0,0)]),
         ("git status # it's fine\ngit commit -m x", [("commit",0,0)]),
         ("git -C ~/Projects/do push", [("push",0,0)]),
         ("if git push origin main; then echo ok; fi", [("push",0,1)]),
         ("while ! git push; do sleep 1; done", [("push",0,1)]),
         ('bash -c "git push"', [("push",0,0)]),
         ("sh -c 'git commit -am x'", [("commit",0,0)]),
         ('eval "git push"', [("push",0,0)]),
         ("sudo -u bob git push", [("push",0,0)]),
         ("env -i git push", [("push",0,0)]),
         ("git --git-dir /x push", [("push",0,0)]),
         ('echo "$(git push)"', [("push",0,0)]),
         ("osascript -e 'tell application \"Mail\" to send m'", [("send",0,0)]),
         ("rm -rf $HOME/important && echo hi > /tmp/log", [("delete",0,0)]),
         ("rm ~/Documents/tmp.old", [("delete",0,0)]),
         ("rm -f ./s515-q.tmp.ts", []),
         ("curl -X POST https://api.example.com -d '{\"u\":\"http://localhost\"}'", [("web write",0,0)]),
         ("curl -d'{}' https://api.example.com", [("web write",0,0)]),
         ("curl -sd x https://api.example.com", [("web write",0,0)]),
         ("curl --json '{}' https://api.example.com", [("web write",0,0)]),
         ("curl -s -X POST localhost:3000/api -d x", []),
         ("python3 scripts/deploy.py", [("deploy",0,0)]),
         ("npm run deploy", [("deploy",0,0)]),
         ("pnpm publish", [("deploy",0,0)]),
         ("gcloud run deploy x", [("deploy",0,0)]),
         ("find . -name x -exec rm {} \;", [("delete",0,0)]),
         ("ls | xargs -n 1 rm", [("delete",0,0)]),
         ("git clean -fdx", [("git history",0,0)]),
         ("git checkout -- .", [("git history",0,0)]),
         ("rm -f /tmp/x 2>/dev/null", []),
         ("git push && echo ok &", [("push",1,0)]),
         ("git push\n", [("push",0,0)]),
         ("git commit-tree abc", []),
         ("cat <<A <<B\ngit push in-a\nA\ngit push in-b\nB\ngit tag v9", [("git history",0,0)]),
         ("git commit -m x && git push -q && git log --oneline -1", [("commit",0,0),("push",0,0)]),
         ("git commit -m x && git push -q 2>&1 | tail -2; git status", [("commit",0,1),("push",0,1)]),
         ("S=/private/tmp/x/scratchpad/r; rm -rf $S; mkdir -p $S", []),
         ("git push --dry-run", []),
         ("git status --short && git log --oneline -3", []),
        ]
        for cmd, want in C:
            got = [(k, int("bg" in f), int("masked" in f)) for k, d, f in cl.classify("Bash", {"command": cmd})]
            self.assertEqual(got, want, msg=cmd)

    def test_ignores_other_sources(self):
        path = os.path.join(FIX, "compaction-transcript.jsonl")
        self.assertEqual(run(self.S, {"transcript_path": path, "source": "startup"})[1], "")

    def test_bad_input_is_quiet(self):
        self.assertEqual(run(self.S, "not json")[0], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
