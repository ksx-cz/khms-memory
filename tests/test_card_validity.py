#!/usr/bin/env python3
"""Acceptance tests for tools/card_validity.py -- does an approved card still hold against today's trees?

Run:  python3 -m pytest tests/test_card_validity.py

Guarded properties:
  (1) MECHANICAL: the anchors a card names (paths, path:line, unit names, git shas, snake_case keys) are
      extracted deterministically and each is classified against the trees: exists / moved / gone /
      line-gone / absent / external -- no model involved.
  (2) SCHEMA: the model's answer is one fixed JSON object; every deviation is named in an error line.
  (3) EVIDENCE RE-RUN: every evidence command is re-run by the checker in the same read-only folder,
      through an allowlist with no shell; a quote that is not in the real output is an error.
  (4) FEEDBACK LOOP: a rejected answer goes back to the model with the exact errors, at most twice;
      a model that keeps quoting wrong ends as `unverified`, never as its own verdict.
  (5) SAMPLE / PROPOSALS / INTERVAL: the calibration sample is deterministic and stratified by month;
      only a no-longer-holds verdict both legs agree on (or the paid leg alone, flagged) is proposed.
Every tree, card and repository is built in a temporary directory; nothing reads a real base.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

TOOLS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools")
sys.path.insert(0, TOOLS)

import card_validity as CV  # noqa: E402

CARD_BODY = """\
stack control goes through one script

`dumpee_testing/scripts/stack.sh` is the only entry point; the old `deploy/restart_all.sh` is gone.
The verdict line is printed at `dumpee_testing/scripts/stack.sh:3` and the unit `dumpee-stack.service`
starts it. The tunable `min_turning_radius: 1.47` lives in `nav/config/params.yaml`.
Fixed in commit `{sha}`; the helper `old_helper.py` was moved. See /etc/systemd/system/foo.service and
https://example.com/a/b.py for background.
"""


def card_text(cid="K-00001", typ="fact", status="active", date="2026-08-01", body=CARD_BODY):
    return (f"---\nid: {cid}\ntype: {typ}\nlevel: observation\nstatus: {status}\ntags: [testing]\n"
            f"scope: universal\nevidence: measured\nsource: test\ndate: {date}\nlinks:\n  derived_from: []\n"
            f"  supports: []\n  contradicts: []\n  refuted_by: []\n  supersedes: null\n---\n{body}")


def write(root, rel, text):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(text)
    return p


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="khms-validity-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = os.path.join(self.tmp, "sandbox")
        # tree "proj": stack.sh exists (2 lines only), old_helper.py moved to lib/, params.yaml holds the key
        write(self.root, "proj/dumpee_testing/scripts/stack.sh", "#!/bin/bash\necho STACK-VERDICT\n")
        write(self.root, "proj/lib/old_helper.py", "X = 1\n")
        write(self.root, "proj/nav/config/params.yaml", "min_turning_radius: 1.74\n")
        write(self.root, "proj/deploy/units/dumpee-stack.service", "[Unit]\n")
        write(self.root, "proj/deploy/README.md", "nothing\n")
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x"}
        g = lambda *a: subprocess.run(["git", "-C", self.repo, *a], check=True, capture_output=True,
                                      text=True, env=env)
        g("init", "-q", "-b", "master")
        write(self.repo, "a.txt", "a\n")
        g("add", "a.txt")
        g("commit", "-qm", "base")
        self.sha = g("rev-parse", "HEAD").stdout.strip()[:10]


class Mechanical(Fixture):
    def test_extracts_each_anchor_kind_and_skips_urls(self):
        anchors = CV.extract_anchors(CARD_BODY.format(sha=self.sha))
        got = {(a["kind"], a["value"]) for a in anchors}
        self.assertIn(("path", "dumpee_testing/scripts/stack.sh"), got)
        self.assertIn(("path", "deploy/restart_all.sh"), got)
        self.assertIn(("path:line", "dumpee_testing/scripts/stack.sh:3"), got)
        self.assertIn(("unit", "dumpee-stack.service"), got)
        self.assertIn(("key", "min_turning_radius"), got)
        self.assertIn(("sha", self.sha), got)
        self.assertIn(("name", "old_helper.py"), got)
        self.assertFalse(any("example.com" in v for _k, v in got), got)
        # deterministic: same text, same list in the same order
        self.assertEqual(anchors, CV.extract_anchors(CARD_BODY.format(sha=self.sha)))

    def test_classifies_against_the_trees(self):
        idx = CV.TreeIndex({"proj": os.path.join(self.root, "proj")}, git_repos=[self.repo])
        res = {a["value"]: idx.check(a)["status"]
               for a in CV.extract_anchors(CARD_BODY.format(sha=self.sha))}
        self.assertEqual(res["dumpee_testing/scripts/stack.sh"], "exists")
        self.assertEqual(res["deploy/restart_all.sh"], "gone")
        self.assertEqual(res["dumpee_testing/scripts/stack.sh:3"], "line-gone")   # the file has 2 lines
        self.assertEqual(res["dumpee-stack.service"], "exists")
        self.assertEqual(res["min_turning_radius"], "exists")
        self.assertEqual(res[self.sha], "exists")
        self.assertEqual(res["old_helper.py"], "exists")                          # bare name, found anywhere
        self.assertEqual(res["/etc/systemd/system/foo.service"], "external")
        moved = idx.check({"kind": "path", "value": "tools/old_helper.py", "path": "tools/old_helper.py",
                           "line": None})
        self.assertEqual(moved["status"], "moved")
        self.assertIn("proj/lib/old_helper.py", moved["where"])
        self.assertEqual(idx.check({"kind": "sha", "value": "abc1234def", "path": None, "line": None})["status"],
                         "absent")
        self.assertEqual(idx.check({"kind": "key", "value": "no_such_key_here", "path": None,
                                    "line": None})["status"], "absent")

    def test_mechanical_pass_over_a_base_counts_statuses(self):
        know = os.path.join(self.tmp, "know")
        write(know, "K-00001.md", card_text(body=CARD_BODY.format(sha=self.sha)))
        write(know, "K-00002.md", card_text("K-00002", status="superseded"))
        idx = CV.TreeIndex({"proj": os.path.join(self.root, "proj")}, git_repos=[self.repo])
        rows, summary = CV.mechanical_pass(CV.load_cards(know), idx)
        self.assertEqual([r["id"] for r in rows], ["K-00001"])        # active only
        self.assertEqual(summary["cards"], 1)
        self.assertGreaterEqual(summary["by_status"]["gone"], 1)
        self.assertGreaterEqual(summary["by_status"]["moved"] + summary["by_status"]["exists"], 1)
        self.assertEqual(summary["cards_with_gone_or_moved"], 1)


GOOD = {
    "card_id": "K-00001",
    "subclaims": [{
        "claim": "stack.sh exists",
        "verdict": "holds",
        "evidence": [{"command": "ls proj/dumpee_testing/scripts/stack.sh", "exit_code": 0,
                      "output_quote": "proj/dumpee_testing/scripts/stack.sh"}],
        "reason": "ls lists it",
    }],
    "verdict": "holds",
    "false_sentence": "",
    "suggested_path": "none",
}


class Schema(Fixture):
    def test_good_answer_passes_schema_and_evidence(self):
        card = CV.parse_card(card_text(body=CARD_BODY.format(sha=self.sha)), "K-00001.md")
        self.assertEqual(CV.check_answer(json.loads(json.dumps(GOOD)), card, self.root), [])

    def test_schema_errors_are_named(self):
        card = CV.parse_card(card_text(body=CARD_BODY.format(sha=self.sha)), "K-00001.md")
        bad = json.loads(json.dumps(GOOD))
        del bad["suggested_path"]
        bad["verdict"] = "probably"
        errs = CV.check_answer(bad, card, self.root)
        self.assertTrue(any("suggested_path" in e for e in errs), errs)
        self.assertTrue(any("verdict" in e and "probably" in e for e in errs), errs)

    def test_verdict_without_evidence_is_an_error(self):
        card = CV.parse_card(card_text(body=CARD_BODY.format(sha=self.sha)), "K-00001.md")
        bad = json.loads(json.dumps(GOOD))
        bad["subclaims"][0]["evidence"] = []
        errs = CV.check_answer(bad, card, self.root)
        self.assertTrue(any("evidence" in e for e in errs), errs)

    def test_card_verdict_must_follow_from_subclaims(self):
        card = CV.parse_card(card_text(body=CARD_BODY.format(sha=self.sha)), "K-00001.md")
        bad = json.loads(json.dumps(GOOD))
        bad["verdict"] = "no-longer-holds"
        errs = CV.check_answer(bad, card, self.root)
        self.assertTrue(any("follow" in e for e in errs), errs)

    def test_false_sentence_must_be_in_the_card(self):
        card = CV.parse_card(card_text(body=CARD_BODY.format(sha=self.sha)), "K-00001.md")
        ans = json.loads(json.dumps(GOOD))
        ans["subclaims"][0].update(verdict="no-longer-holds", evidence=[
            {"command": "ls proj/deploy/restart_all.sh", "exit_code": 2, "output_quote": "No such file"}])
        ans.update(verdict="no-longer-holds", false_sentence="the moon is made of cheese",
                   suggested_path="dated-pointer")
        errs = CV.check_answer(ans, card, self.root)
        self.assertTrue(any("false_sentence" in e for e in errs), errs)
        ans["false_sentence"] = "the old `deploy/restart_all.sh` is gone."
        self.assertEqual(CV.check_answer(ans, card, self.root), [])
        ans["false_sentence"] = "the old deploy/restart_all.sh is gone"     # backticks are typography
        self.assertEqual(CV.check_answer(ans, card, self.root), [])

    def test_parse_answer_takes_fenced_or_bare_json(self):
        self.assertEqual(CV.parse_answer("blah\n```json\n{\"a\": 1}\n```\n")["a"], 1)
        self.assertEqual(CV.parse_answer("{\"a\": 2}")["a"], 2)
        with self.assertRaises(ValueError):
            CV.parse_answer("no json here")


class Evidence(Fixture):
    def test_wrong_quote_and_wrong_exit_code_are_detected(self):
        ok = CV.rerun_evidence({"command": "grep -n STACK proj/dumpee_testing/scripts/stack.sh",
                                "exit_code": 0, "output_quote": "2:echo STACK-VERDICT"}, self.root)
        self.assertIsNone(ok)
        err = CV.rerun_evidence({"command": "grep -n STACK proj/dumpee_testing/scripts/stack.sh",
                                 "exit_code": 0, "output_quote": "STACK-OK"}, self.root)
        self.assertIn("not found in the real output", err)
        err = CV.rerun_evidence({"command": "grep -n NOPE proj/dumpee_testing/scripts/stack.sh",
                                 "exit_code": 0, "output_quote": ""}, self.root)
        self.assertIn("exit code", err)
        self.assertIsNone(CV.rerun_evidence({"command": "grep -n NOPE proj/dumpee_testing/scripts/stack.sh",
                                             "exit_code": 1, "output_quote": ""}, self.root))

    def test_a_multi_line_quote_must_match_line_by_line(self):
        write(self.root, "proj/m.txt", "alpha one\nbeta two\ngamma three\n")
        ev = {"command": "cat proj/m.txt", "exit_code": 0, "output_quote": "alpha one\ngamma three"}
        self.assertIsNone(CV.rerun_evidence(ev, self.root))           # a skipped line is fine
        ev["output_quote"] = "alpha one\ngamma three."
        self.assertIn("not found", CV.rerun_evidence(ev, self.root))  # an added period is not

    def test_forbidden_commands_are_refused_not_run(self):
        canary = os.path.join(self.tmp, "canary")
        for cmd in (f"touch {canary}", "cat proj/deploy/README.md | head", "cat proj/a; ls",
                    "ls > out.txt", "cat /etc/passwd", "cat ../repo/a.txt", "python3 -c 'print(1)'",
                    "grep -r x proj --include=*.py && touch x"):
            with self.subTest(cmd=cmd):
                err = CV.rerun_evidence({"command": cmd, "exit_code": 0, "output_quote": ""}, self.root)
                self.assertIsNotNone(err)
                self.assertIn("not allowed", err)
        self.assertFalse(os.path.exists(canary))

    def test_grep_pattern_may_look_like_an_absolute_path(self):
        write(self.root, "proj/x.sh", "source /opt/ros/jazzy/setup.bash\n")
        self.assertIsNone(CV.rerun_evidence({"command": "grep -n /opt/ros proj/x.sh", "exit_code": 0,
                                             "output_quote": "/opt/ros/jazzy"}, self.root))


class FakeModel:
    """Returns the scripted answers in order and records every prompt it was given."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.prompts = []

    def __call__(self, prompt):
        self.prompts.append(prompt)
        a = self.answers.pop(0) if self.answers else self.answers_last
        self.answers_last = a
        return (a if isinstance(a, str) else json.dumps(a)), {"wall_s": 0.01, "cost_usd": 0.0}


class FeedbackLoop(Fixture):
    def card(self):
        return CV.parse_card(card_text(body=CARD_BODY.format(sha=self.sha)), "K-00001.md")

    def test_wrong_quote_is_retried_with_the_exact_error_then_unverified(self):
        wrong = json.loads(json.dumps(GOOD))
        wrong["subclaims"][0]["evidence"][0]["output_quote"] = "stack.sh is present and healthy"
        model = FakeModel([wrong, wrong, wrong, wrong])
        res = CV.judge_card(self.card(), model, self.root, anchors=[], max_retries=2)
        self.assertEqual(res["verdict"], "unverified")
        self.assertEqual(res["attempts"], 3)                       # 1 + 2 retries, never a fourth call
        self.assertEqual(len(model.prompts), 3)
        self.assertNotIn("REJECTED", model.prompts[0])
        for p in model.prompts[1:]:
            self.assertIn("REJECTED", p)
            self.assertIn("stack.sh is present and healthy", p)    # the exact offending quote
            self.assertIn("not found in the real output", p)
        self.assertTrue(res["errors"])

    def test_schema_error_then_a_good_answer_is_accepted_on_the_retry(self):
        model = FakeModel(["I think it holds.", GOOD])
        res = CV.judge_card(self.card(), model, self.root, anchors=[], max_retries=2)
        self.assertEqual(res["verdict"], "holds")
        self.assertEqual(res["attempts"], 2)
        self.assertIn("no JSON object", model.prompts[1])

    def test_model_error_counts_as_an_attempt(self):
        def broken(prompt):
            raise TimeoutError("deadline 5s")
        res = CV.judge_card(self.card(), broken, self.root, anchors=[], max_retries=1)
        self.assertEqual(res["verdict"], "unverified")
        self.assertEqual(res["attempts"], 2)
        self.assertIn("deadline", json.dumps(res["errors"]))

    def test_prompt_carries_card_anchors_steps_and_schema(self):
        model = FakeModel([GOOD])
        anchors = [{"kind": "path", "value": "deploy/restart_all.sh", "status": "gone", "where": []}]
        CV.judge_card(self.card(), model, self.root, anchors=anchors, max_retries=0)
        p = model.prompts[0]
        for needle in ("K-00001", "deploy/restart_all.sh", "gone", "cannot-check-offline",
                       "output_quote", "STEPS", "re-runs every command"):
            self.assertIn(needle, p)


class SampleAndReport(unittest.TestCase):
    def cards(self):
        out = []
        for m, n in (("2026-07", 30), ("2026-08", 60), ("2026-09", 10)):
            for i in range(n):
                cid = f"K-{m[-1]}{i:04d}"
                out.append(CV.parse_card(card_text(cid, date=f"{m}-1{i % 9}"), cid + ".md"))
        out.append(CV.parse_card(card_text("K-99999", typ="principle"), "K-99999.md"))
        return out

    def test_sample_is_deterministic_stratified_and_typed(self):
        s1 = CV.select_sample(self.cards(), 10, seed=219)
        s2 = CV.select_sample(self.cards(), 10, seed=219)
        self.assertEqual([c["id"] for c in s1], [c["id"] for c in s2])
        self.assertEqual(len(s1), 10)
        months = [c["date"][:7] for c in s1]
        self.assertEqual((months.count("2026-07"), months.count("2026-08"), months.count("2026-09")), (3, 6, 1))
        self.assertNotIn("K-99999", [c["id"] for c in s1])
        self.assertNotEqual([c["id"] for c in s1], [c["id"] for c in CV.select_sample(self.cards(), 10, seed=7)])

    def test_proposals_need_agreement_or_are_flagged(self):
        nlh = {"verdict": "no-longer-holds", "answer": {"false_sentence": "S", "suggested_path": "supersede",
                                                        "subclaims": [{"verdict": "no-longer-holds", "evidence": [
                                                            {"command": "ls x", "exit_code": 2,
                                                             "output_quote": "No such"}]}]}}
        holds = {"verdict": "holds", "answer": {}}
        rows = [{"id": "K-1", "qwen": nlh, "sonnet": nlh},
                {"id": "K-2", "qwen": holds, "sonnet": nlh},
                {"id": "K-3", "qwen": nlh, "sonnet": holds}]
        lines = CV.proposal_lines(rows)
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].startswith("- K-1 "))
        self.assertNotIn("SONNET-ONLY", lines[0])
        self.assertIn("K-2", lines[1])
        self.assertIn("SONNET-ONLY", lines[1])
        self.assertIn("supersede", lines[0])
        self.assertIn("ls x", lines[0])

    def test_wilson_interval(self):
        lo, hi = CV.wilson(5, 50)
        self.assertAlmostEqual(lo, 0.0435, places=3)
        self.assertAlmostEqual(hi, 0.2136, places=3)
        self.assertEqual(CV.wilson(0, 0), (0.0, 0.0))


if __name__ == "__main__":
    unittest.main()
