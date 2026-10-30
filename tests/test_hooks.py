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

    def test_ignores_other_sources(self):
        path = os.path.join(FIX, "compaction-transcript.jsonl")
        self.assertEqual(run(self.S, {"transcript_path": path, "source": "startup"})[1], "")

    def test_bad_input_is_quiet(self):
        self.assertEqual(run(self.S, "not json")[0], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
