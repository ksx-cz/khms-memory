#!/usr/bin/env python3
"""The directive, the injection experiment, and a report that is never gated (#247).

Three mechanisms, one hook, one temp base — nothing here reads or writes a real one.

  * THE DIRECTIVE IS A FILE. It enters the session ONCE at SessionStart; every prompt
    afterwards carries one pointer line. As a constant appended to every prompt it cost
    1813 characters 46 times in one measured day.
  * THE INJECTION SWITCH. tools/khms_experiment.json turns card injection off per event
    so its value can be MEASURED rather than assumed; a missing or broken file means
    "as before", which is what makes deleting it a complete rollback.
  * NO GATE ON A REPORT (#247). The retrieval-claim gate (a mandatory `base:` line, card ids
    backed by a recall of the same turn) was removed with the `base:` line itself: cards are
    input to the agent, not output to the principal, and every refusal cost a full resend.
    A report without the line, or citing cards without a recall, passes untouched.

RED-first record: against the pre-change hook every test in ExperimentSwitch failed (no experiment
switch), and the directive tests failed because the full text was appended to every prompt instead of
the pointer line. Against the #247 parent commit the ReportNotGated tests and the `base:`-free directive
assertions failed (the claim gate denied, the directive demanded `base:`).
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
        self.assertNotIn("mandatory reply lines", ctx)
        self.assertNotIn("-->", ctx)
        self.assertNotIn("base:", ctx)
        self.assertIn("verified:", ctx)
        self.assertIn("if I were wrong:", ctx)
        self.assertGreater(len(ctx), 800, "the full text should be here, once")

    def test_multiline_and_inline_html_comments_are_not_injected(self):
        path = os.path.join(self.root, "directive.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write("<!-- hidden header\nhidden continuation -->\nbase: visible\n"
                    "verified: visible <!-- hidden inline -->\nif I were wrong: visible")
        ctx = self.context({"hook_event_name": "SessionStart", "session_id": "comments"},
                           KHMS_DIRECTIVE=path)
        self.assertNotIn("hidden", ctx)
        self.assertNotIn("<!--", ctx)
        self.assertIn("base: visible", ctx)
        self.assertIn("verified: visible", ctx)

    def test_a_prompt_carries_only_the_pointer_line(self):
        ctx = self.context(self.prompt("why do the checksums keep failing on the link"))
        self.assertNotIn("base:", ctx)
        self.assertIn("verified:", ctx)
        self.assertIn("report_directive.md", ctx)
        self.assertLess(len(ctx), 500,
                        "a prompt must not carry the whole directive again")

    def test_a_missing_directive_file_degrades_to_the_one_liner(self):
        # The directive ships with the code; a deployment points KHMS_DIRECTIVE elsewhere. A path that
        # does not exist must degrade, not crash.
        ctx = self.context({"hook_event_name": "SessionStart", "session_id": "s2"},
                           KHMS_DIRECTIVE=os.path.join(self.root, "no-such-directive.md"))
        self.assertIn("verified:", ctx)
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
        self.assertIn("verified:", ctx, "the directive is not part of the experiment")

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


class ReportNotGated(HookCase):
    """#247: no card gate on the report -- no `base:` line is demanded and cited ids are not checked."""
    def open_turn(self, session="s1"):
        self.context(self.prompt("what is going on with the humidity gaps", session))

    def test_a_substantial_report_without_the_line_passes(self):
        self.open_turn()
        self.assertIsNone(self.fire(self.report("x" * 300)))

    def test_citing_cards_with_no_recall_in_this_turn_passes(self):
        self.open_turn()
        self.assertIsNone(self.fire(self.report("base: humidity gaps -> K-90001\n" + "y" * 300)))

    def test_no_claim_log_is_written(self):
        self.open_turn()
        self.fire(self.report("x" * 300))
        self.assertFalse(os.path.exists(os.path.join(self.root, "state", "claim_gate.log")))

    def test_a_report_still_stamps_the_reply_for_the_stop_gate(self):
        self.open_turn()
        self.fire(self.report("x" * 300))
        state_dir = os.path.join(self.root, "state", "hook-state")
        stamped = []
        for name in os.listdir(state_dir):
            with open(os.path.join(state_dir, name), encoding="utf-8") as f:
                stamped.append("discord_reply_t" in f.read())
        self.assertTrue(any(stamped))


if __name__ == "__main__":
    unittest.main(verbosity=2)
