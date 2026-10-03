#!/usr/bin/env python3
"""The directive, the injection experiment and the retrieval-claim gate.

Three mechanisms, one hook, one temp base — nothing here reads or writes a real one.

  * THE DIRECTIVE IS A FILE. It enters the session ONCE at SessionStart; every prompt
    afterwards carries one pointer line. As a constant appended to every prompt it cost
    1813 characters 46 times in one measured day.
  * THE INJECTION SWITCH. tools/khms_experiment.json turns card injection off per event
    so its value can be MEASURED rather than assumed; a missing or broken file means
    "as before", which is what makes deleting it a complete rollback.
  * THE RETRIEVAL-CLAIM GATE. The mandatory `base:` line is a check, and a check whose
    passing condition holds whether or not a retrieval happened is not a check. A report
    citing card ids with no matching recall in this turn is DENIED.

RED-first record: against the pre-change hook every test in the last two classes failed
(no experiment switch, no gate), and the directive tests failed because the full text was
appended to every prompt instead of the pointer line.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

TOOLS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools")
HOOK = os.path.join(TOOLS, "khms_hook.py")
INIT = os.path.join(TOOLS, "khms_init.sh")
REPLY_TOOL = "mcp__chat__reply"        # matched by KHMS_REPORT_TOOL below


class HookCase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="hookdir-")
        self.addCleanup(shutil.rmtree, self.root, True)
        subprocess.run([INIT, self.root], capture_output=True, text=True,
                       check=True)

    def fire(self, payload, **env_extra):
        env = dict(os.environ, KHMS_ROOT=self.root,
                   KHMS_REPORT_TOOL=r"(?i)chat.*reply")
        env.update(env_extra)
        r = subprocess.run([sys.executable, HOOK], input=json.dumps(payload),
                           capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        out = r.stdout.strip()
        return json.loads(out) if out else None

    def context(self, payload, **kw):
        got = self.fire(payload, **kw)
        return (got or {}).get("hookSpecificOutput", {}).get("additionalContext", "")

    def prompt(self, text, session="s1"):
        return {"hook_event_name": "UserPromptSubmit", "prompt": text,
                "session_id": session}

    def report(self, text, session="s1"):
        return {"hook_event_name": "PreToolUse", "tool_name": REPLY_TOOL,
                "tool_input": {"text": text}, "session_id": session}

    def experiment(self, cfg):
        with open(os.path.join(self.root, "config", "khms_experiment.json"),
                  "w", encoding="utf-8") as f:
            json.dump(cfg, f)

    def log(self):
        p = os.path.join(self.root, "state", "inject.log")
        return open(p, encoding="utf-8").read() if os.path.exists(p) else ""


class Directive(HookCase):
    def test_session_start_carries_the_whole_directive(self):
        ctx = self.context({"hook_event_name": "SessionStart", "session_id": "s1"})
        self.assertIn("base:", ctx)
        self.assertIn("verified:", ctx)
        self.assertIn("if I were wrong:", ctx)
        self.assertGreater(len(ctx), 800, "the full text should be here, once")

    def test_a_prompt_carries_only_the_pointer_line(self):
        ctx = self.context(self.prompt("why do the checksums keep failing on the link"))
        self.assertIn("base:", ctx)
        self.assertIn("report_directive.md", ctx)
        self.assertLess(len(ctx), 500,
                        "a prompt must not carry the whole directive again")

    def test_a_missing_directive_file_degrades_to_the_one_liner(self):
        # The directive ships with the code; a deployment points KHMS_DIRECTIVE elsewhere. A path that
        # does not exist must degrade, not crash.
        ctx = self.context({"hook_event_name": "SessionStart", "session_id": "s2"},
                           KHMS_DIRECTIVE=os.path.join(self.root, "no-such-directive.md"))
        self.assertIn("base:", ctx)
        self.assertIn("report_directive.md", ctx)


class ExperimentSwitch(HookCase):
    def test_no_file_means_as_before(self):
        self.context(self.prompt("why do the checksums keep failing on the link"))
        self.assertIn("UserPromptSubmit", self.log())
        self.assertNotIn("exp:", self.log())

    def test_switching_an_event_off_skips_before_the_base_is_loaded(self):
        self.experiment({"name": "no-ups", "inject": {"UserPromptSubmit": False}})
        ctx = self.context(self.prompt("why do the checksums keep failing on the link"))
        self.assertIn("skip (exp:no-inject)", self.log())
        self.assertIn("exp:no-ups", self.log(), "the regime must be stamped into the log")
        self.assertIn("base:", ctx, "the directive is not part of the experiment")

    def test_a_broken_config_fails_open(self):
        with open(os.path.join(self.root, "config", "khms_experiment.json"),
                  "w", encoding="utf-8") as f:
            f.write("{ this is not json")
        self.context(self.prompt("why do the checksums keep failing on the link"))
        self.assertNotIn("skip (exp:no-inject)", self.log())

    def test_the_name_is_derived_when_the_config_does_not_give_one(self):
        self.experiment({"inject": {"UserPromptSubmit": False}})
        self.context(self.prompt("why do the checksums keep failing on the link"))
        self.assertIn("exp:no-ups", self.log())


class ClaimGate(HookCase):
    def open_turn(self, session="s1"):
        self.context(self.prompt("what is going on with the humidity gaps", session))

    def test_a_substantial_report_without_the_line_is_denied(self):
        self.open_turn()
        got = self.fire(self.report("x" * 300))
        self.assertEqual(got["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("base:", got["hookSpecificOutput"]["permissionDecisionReason"])

    def test_a_short_report_is_exempt(self):
        self.open_turn()
        self.assertIsNone(self.fire(self.report("done")))

    def test_citing_cards_with_no_recall_in_this_turn_is_denied(self):
        self.open_turn()
        got = self.fire(self.report("base: humidity gaps -> K-90001\n" + "y" * 300))
        self.assertEqual(got["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_an_honest_line_is_always_allowed(self):
        self.open_turn()
        self.assertIsNone(
            self.fire(self.report("base: did not search - orchestration only\n"
                                  + "y" * 300)))

    def test_a_line_citing_no_card_is_allowed(self):
        self.open_turn()
        self.assertIsNone(
            self.fire(self.report("base: humidity gaps -> nothing useful\n" + "y" * 300)))

    def test_a_matching_recall_in_this_turn_allows_the_citation(self):
        self.open_turn()
        subprocess.run([os.path.join(TOOLS, "recall.sh"),
                        "humidity", "gaps"], capture_output=True, text=True,
                       env=dict(os.environ, KHMS_ROOT=self.root))
        self.assertIsNone(
            self.fire(self.report("base: humidity gaps -> K-90001\n" + "y" * 300)))

    def test_the_gate_can_be_switched_off_in_the_experiment_config(self):
        self.open_turn()
        self.experiment({"claim_gate": False})
        self.assertIsNone(self.fire(self.report("x" * 300)))

    def test_every_verdict_is_logged(self):
        self.open_turn()
        self.fire(self.report("x" * 300))
        log = open(os.path.join(self.root, "state", "claim_gate.log"),
                   encoding="utf-8").read()
        self.assertIn("DENY", log)

    def test_a_report_tool_that_does_not_match_is_not_gated(self):
        self.open_turn()
        self.assertIsNone(self.fire({"hook_event_name": "PreToolUse",
                                     "tool_name": "Bash",
                                     "tool_input": {"command": "ls"},
                                     "session_id": "s1"}))


if __name__ == "__main__":
    unittest.main(verbosity=2)
