#!/usr/bin/env python3
"""Two hook properties, end to end through the real binary, in English.

  * A SWITCH THAT IS ON IS SAID. The kill switch (file or KHMS_HOOKS_OFF=1) writes one HOOKS-OFF line per
    silenced event to the inject log, and a gate switched off in the experiment file writes one OFF line
    to its own log each time it is consulted: a switched-off gate must not look like an idle one.
  * PROPOSAL PHRASINGS. The shipped English matchers recognise "I'd go with", "I'd recommend", a bare
    "Proposed:" / "Proposed solution:" / "Recommended option:" label, and a label after a connective or a
    "revised"-type word; plain status lines that merely mention a proposal stay ungated.
"""
import json
import os
import subprocess
import sys
import unittest

from test_hook_gates import HONEST, HOOK, PROPOSAL, HookCase


class SwitchesAreSaid(HookCase):
    def experiment(self, **flags):
        with open(os.path.join(self.root, "config", "khms_experiment.json"), "w") as f:
            json.dump(flags, f)

    def test_the_kill_switch_file_writes_one_line_per_silenced_event(self):
        off = os.path.join(self.state, "hooks-off")
        open(off, "w").close()
        self.assertIsNone(self.fire({"hook_event_name": "SessionStart", "session_id": "s1"}))
        self.assertIsNone(self.prompt())
        lines = [ln for ln in self.read_log("inject.log").splitlines() if "HOOKS-OFF" in ln]
        self.assertEqual(len(lines), 2, lines)
        self.assertIn("| SessionStart | q= | nhits=0 | top=- | HOOKS-OFF: every KHMS hook skipped", lines[0])
        self.assertIn("| UserPromptSubmit |", lines[1])
        self.assertIn(off, lines[0])

    def test_the_kill_switch_variable_is_named_in_its_line(self):
        self.assertIsNone(self.fire(self.report(PROPOSAL), KHMS_HOOKS_OFF="1"))
        self.assertIn("| PreToolUse | q= | nhits=0 | top=- | HOOKS-OFF: every KHMS hook skipped (KHMS_HOOKS_OFF=1)",
                      self.read_log("inject.log"))
        self.assertEqual(self.read_log("proposal_gate.log"), "")     # nothing ran behind the switch

    def test_no_switch_no_line(self):
        self.prompt()
        self.assertNotIn("HOOKS-OFF", self.read_log("inject.log"))

    def test_a_proposal_gate_switched_off_logs_each_consultation(self):
        self.experiment(proposal_gate=False)
        self.prompt()
        for _ in range(2):
            self.assertFalse(self.denied(self.fire(self.report(PROPOSAL)))[0])
        off = [ln for ln in self.read_log("proposal_gate.log").splitlines() if "| OFF |" in ln]
        self.assertEqual(len(off), 2, off)
        self.assertIn("switched off by proposal_gate in the experiment file", off[0])

    def test_a_dispatch_with_the_proposal_gate_off_is_logged(self):
        self.experiment(proposal_gate=False)
        self.prompt()
        got = self.fire({"hook_event_name": "PreToolUse", "tool_name": "Agent", "session_id": "s1",
                         "tool_input": {"prompt": "I recommend we replace the sensor harness.",
                                        "description": "harness"}})
        self.assertFalse(self.denied(got)[0])
        self.assertRegex(self.read_log("proposal_gate.log"), r"\| OFF \| .*proposal_gate.* \| dispatch")

    def test_the_claim_and_term_gates_switched_off_are_logged(self):
        self.experiment(baze_gate=False, term_gate=False, proposal_gate=False)
        self.prompt()
        self.fire(self.report("No claim line here, and a long enough reply. " * 8))
        self.assertIn("| OFF | switched off by baze_gate/claim_gate", self.read_log("claim_gate.log"))
        self.assertIn("OFF (switched off by term_gate", self.read_log("inject.log"))


# --- the English proposal matchers ---------------------------------------------------------------------
POSITIVE = [
    "I'd go with option B: the ledger floor stays at 2026-09-21.",
    "I'd recommend raising the chunk size to 120 for the backlog.",
    "Proposed: put the ledger floor file back before tonight's run.",
    "Proposed solution: dismiss the status kinds without a model call.",
    "Recommended option: B, with the floor at 21 September.",
    "So the recommendation: measure it on the robot first, then start the architect.",
    "I therefore refine the recommendation: measure it on the robot in step 3 first.",
    "Revised proposal: keep the floor file in the base, not in the code.",
    "I recommend we replace the sensor harness with shielded wire.",          # control: matched before
]
NEGATIVE = [
    "The night finished at 05:10; the ledger has 331 open records.",
    "The proposed fix was deployed yesterday and the gate is green.",
    "Status of the proposal: merged into master on Friday.",
    "Recommendations from the review are all applied and tested.",
    "We went with option B last week and it holds.",
    "The marks carry proposed_in for 40 entries; nothing else changed.",
    "DONE: the harness was checked; it is fine.",
]

SPANS = (
    "import json, sys\n"
    "sys.path.insert(0, %r)\n"
    "import khms_hook as H\n"
    "print(json.dumps([bool(H.proposal_spans(t)) for t in json.load(sys.stdin)]))\n")


def gated(texts):
    """The shipped matchers (no deployment patterns): which texts carry a proposal span."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("KHMS_")}
    r = subprocess.run([sys.executable, "-c", SPANS % os.path.dirname(HOOK)], input=json.dumps(texts),
                       capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 0, r.stderr
    return dict(zip(texts, json.loads(r.stdout)))


class EnglishProposalPhrasings(unittest.TestCase):
    def test_every_proposal_phrasing_is_gated(self):
        missed = [t for t, g in gated(POSITIVE).items() if not g]
        self.assertEqual(missed, [])

    def test_plain_status_lines_stay_ungated(self):
        flagged = [t for t, g in gated(NEGATIVE).items() if g]
        self.assertEqual(flagged, [])


class NewPhrasingThroughTheGate(HookCase):
    def test_a_bare_proposed_label_is_denied_once_with_cards(self):
        path = os.path.join(self.root, "memory", "know", "K-90002.md")
        with open(path, "w") as f:
            f.write("---\nid: K-90002\ntype: fact\nlevel: observation\nstatus: active\ntags: [testing]\n"
                    "scope: universal\ndate: 2026-01-01\n---\n# Ledger floor file\n"
                    "The ledger floor file names the first tracked journal day.\n")
        self.prompt()
        text = HONEST + "\n\nProposed: write the ledger floor file before the next night.\n"
        deny, why = self.denied(self.fire(self.report(text)))
        self.assertTrue(deny)
        self.assertIn("K-90002", why)
        self.assertFalse(self.denied(self.fire(self.report(text)))[0])          # the same proposal again


if __name__ == "__main__":
    unittest.main()
