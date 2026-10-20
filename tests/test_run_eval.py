#!/usr/bin/env python3
"""The golden set is only worth what its failures cost.

Three properties:
  * a gated row that regresses FAILS the run (exit 1) — otherwise the set is a
    dashboard, not a gate;
  * an ungated row that misses is COUNTED AND NAMED but does not fail — open debt
    kept visible, because a permanently red gate is a gate nobody reads;
  * scored against an empty base the harness REFUSES (exit 2) instead of reporting
    a clean sweep of failures, which is the same number a broken run would print.

The base here is the repository's own fictional example cards, so the harness is
tested against something rather than mocked.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

TOOLS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools")
REPO = os.path.dirname(TOOLS)
EVAL = os.path.join(TOOLS, "eval", "run_eval.py")
EXAMPLES = os.path.join(REPO, "examples")
SHIPPED_GOLD = os.path.join(EXAMPLES, "golden_set.jsonl")


def a_real_gold_id():
    """An id that exists in the example base, taken from the shipped set rather than
    written out here: a literal card id in a tools/ file is a disclosure-gate hit,
    and this test does not need to know which card it is."""
    with open(SHIPPED_GOLD, encoding="utf-8") as f:
        return json.loads(f.readline())["gold"]


def base_with_examples():
    root = tempfile.mkdtemp(prefix="eval-")
    know = os.path.join(root, "memory", "know")
    os.makedirs(know)
    os.makedirs(os.path.join(root, "state", "shared"), exist_ok=True)
    for f in sorted(os.listdir(EXAMPLES)):
        if f.startswith("K-") and f.endswith(".md"):
            shutil.copy(os.path.join(EXAMPLES, f), know)
    return root


def run(root, *args):
    env = dict(os.environ, KHMS_ROOT=root)
    return subprocess.run([sys.executable, EVAL, "--prod", *args],
                          capture_output=True, text=True, env=env)


class Prod(unittest.TestCase):
    def setUp(self):
        self.root = base_with_examples()
        self.addCleanup(shutil.rmtree, self.root, True)

    def test_the_shipped_set_passes(self):
        # G3 (a superseded card and its successor both match) was open debt until #247: recall now returns
        # the successor in the dead card's place, so the row is gated. An ungated miss is still counted and
        # named -- test_the_same_row_ungated_does_not_fail_the_run below holds that property.
        r = run(self.root)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("EVAL recall@3: 3/3 (misses: 0)", r.stdout)
        self.assertIn("gated-fail=0", r.stdout)
        self.assertNotIn("MISS G3", r.stdout)

    def test_a_gated_row_that_misses_fails_the_run(self):
        gold = os.path.join(self.root, "gate.jsonl")
        with open(gold, "w", encoding="utf-8") as f:
            f.write(json.dumps({
                "id": "X1", "split": "eval", "source": "miss", "gate": True,
                "k": 3, "query": "something no card in this base is about at all",
                "gold": a_real_gold_id(), "note": "must fail"}) + "\n")
        r = run(self.root, "--gold", gold)
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("MISS X1 GATE", r.stdout)
        self.assertIn("gated-fail=1", r.stdout)

    def test_the_same_row_ungated_does_not_fail_the_run(self):
        # the control: identical row, one flag flipped
        gold = os.path.join(self.root, "open.jsonl")
        with open(gold, "w", encoding="utf-8") as f:
            f.write(json.dumps({
                "id": "X1", "split": "eval", "source": "miss", "gate": False,
                "k": 3, "query": "something no card in this base is about at all",
                "gold": a_real_gold_id(), "note": "open debt"}) + "\n")
        r = run(self.root, "--gold", gold)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("MISS X1 open", r.stdout)

    def test_an_empty_base_is_refused_not_scored(self):
        empty = tempfile.mkdtemp(prefix="eval-empty-")
        self.addCleanup(shutil.rmtree, empty, True)
        os.makedirs(os.path.join(empty, "memory", "know"))
        r = run(empty)
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn("no cards loaded", r.stdout)

    def test_json_out_records_every_row(self):
        out = os.path.join(self.root, "res.json")
        r = run(self.root, "--json-out", out)
        self.assertEqual(r.returncode, 0, r.stdout)
        data = json.load(open(out, encoding="utf-8"))
        self.assertEqual(data["n"], 3)
        self.assertEqual(len(data["rows"]), 3)
        self.assertTrue(any(row["gate"] for row in data["rows"]))

    def test_the_lexical_channel_scores_the_same_rows(self):
        r = run(self.root, "--channel", "lexical")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("channel=lexical", r.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
