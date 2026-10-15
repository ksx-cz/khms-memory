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
  (6) CHECKED LEDGER: one append-only line per judged card (id, sha256 of the card file, date, judge,
      prompt version, verdict, know-how, reason, evidence); a draw skips every card whose CURRENT hash
      has a ledger line, so a card changed since (an erratum) is eligible again; the calibration backfill
      is idempotent.
  (7) V2 PROMPT: the same judge call also answers "is this card know-how?" -- `knowhow` yes|no is part of
      the schema; a missing or other value goes back as an error.
  (8) RUN / PROPOSALS: the run stops submitting at the dollar cap; every card that is not "holds" or not
      know-how gets one proposal row with its K-05297 route and the exact command (or a draft).
Every tree, card and repository is built in a temporary directory; nothing reads a real base.
"""
import json
import os
import shutil
import subprocess
import sys
import re
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
    "knowhow": "yes",
    "knowhow_reason": "names the one entry point a future agent must use",
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

class Ledger(unittest.TestCase):
    """The checked ledger: which cards were judged, at which content, so a draw never repeats one."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="khms-validity-ledger-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.know = os.path.join(self.tmp, "memory", "know")
        os.makedirs(self.know)
        self.ledger = os.path.join(self.tmp, "memory", "ledger", "validity.jsonl")
        for i in range(1, 21):
            status = "challenged" if i % 5 == 0 else ("superseded" if i == 7 else "active")
            write(self.know, f"K-{i:05d}.md", card_text(f"K-{i:05d}", typ="principle" if i % 3 else "fact",
                                                        status=status, body=f"claim number {i}\n"))

    def cards(self):
        return CV.load_cards(self.know)

    def result(self, verdict="holds", knowhow="yes", cost=0.04):
        return {"verdict": verdict, "attempts": 1, "cost_usd": cost, "wall_s": 9.0,
                "answer": {"verdict": verdict, "knowhow": knowhow, "knowhow_reason": "why",
                           "suggested_path": "none", "false_sentence": "",
                           "subclaims": [{"claim": "c", "verdict": verdict, "reason": "the file says so",
                                          "evidence": [{"command": "ls a", "exit_code": 0,
                                                        "output_quote": "a"}]}]}}

    def test_load_cards_carries_the_file_hash(self):
        import hashlib
        c = {x["id"]: x for x in self.cards()}["K-00003"]
        with open(os.path.join(self.know, "K-00003.md"), "rb") as fh:
            self.assertEqual(c["sha256"], hashlib.sha256(fh.read()).hexdigest())

    def test_ledger_append_writes_one_line_per_judged_card(self):
        c = {x["id"]: x for x in self.cards()}["K-00003"]
        line = CV.ledger_line(c, self.result("no-longer-holds", "no"), judge="sonnet", prompt_version="v2",
                              date="2026-10-15", value_flag="K-00003: states a value", run="seed=5")
        CV.append_ledger(self.ledger, [line])
        CV.append_ledger(self.ledger, [CV.ledger_line(c, self.result(), judge="sonnet", prompt_version="v2",
                                                      date="2026-10-16")])
        rows = CV.read_ledger(self.ledger)
        self.assertEqual(len(rows), 2)                      # append-only: the first line is still there
        r = rows[0]
        for key, want in (("id", "K-00003"), ("sha256", c["sha256"]), ("date", "2026-10-15"),
                          ("judge", "sonnet"), ("prompt_version", "v2"), ("verdict", "no-longer-holds"),
                          ("knowhow", "no"), ("reason", "the file says so"),
                          ("value_flag", "K-00003: states a value")):
            self.assertEqual(r[key], want, key)
        self.assertEqual(r["evidence"][0]["command"], "ls a")

    def test_sampling_skips_every_card_whose_hash_is_in_the_ledger(self):
        cards = self.cards()
        pool = CV.eligible_cards(cards, [], statuses=("active", "challenged"))
        self.assertEqual(len(pool), 19)                     # K-00007 is superseded, every type counts
        first = CV.select_uniform(pool, 8, seed=11)
        self.assertEqual(len(first), 8)
        self.assertEqual([c["id"] for c in first], [c["id"] for c in CV.select_uniform(pool, 8, seed=11)])
        CV.append_ledger(self.ledger, [CV.ledger_line(c, self.result(), "sonnet", "v2", "2026-10-15")
                                       for c in first])
        pool2 = CV.eligible_cards(self.cards(), CV.read_ledger(self.ledger), statuses=("active", "challenged"))
        self.assertEqual(len(pool2), 11)
        again = CV.select_uniform(pool2, 8, seed=11)
        self.assertFalse({c["id"] for c in again} & {c["id"] for c in first})

    def test_a_changed_card_becomes_eligible_again(self):
        c = {x["id"]: x for x in self.cards()}["K-00004"]
        CV.append_ledger(self.ledger, [CV.ledger_line(c, self.result(), "sonnet", "v2", "2026-10-15")])
        ids = lambda: {x["id"] for x in CV.eligible_cards(self.cards(), CV.read_ledger(self.ledger))}
        self.assertNotIn("K-00004", ids())
        with open(os.path.join(self.know, "K-00004.md"), "a", encoding="utf-8") as fh:
            fh.write("[ERRATUM TS-00001] corrected\n")
        self.assertIn("K-00004", ids())

    def test_backfill_is_idempotent(self):
        calib = os.path.join(self.tmp, "sonnet.jsonl")
        with open(calib, "w", encoding="utf-8") as fh:
            for cid, v in (("K-00001", "holds"), ("K-00002", "no-longer-holds"), ("K-00099", "holds")):
                r = self.result(v)
                del r["answer"]["knowhow"]
                fh.write(json.dumps(dict(r, id=cid)) + "\n")
        n1 = CV.backfill(self.ledger, calib, self.cards(), date="2026-10-12")
        n2 = CV.backfill(self.ledger, calib, self.cards(), date="2026-10-12")
        self.assertEqual((n1, n2), (2, 0))                  # K-00099 is not in the base: no line
        rows = CV.read_ledger(self.ledger)
        self.assertEqual([r["id"] for r in rows], ["K-00001", "K-00002"])
        self.assertTrue(all(r["prompt_version"] == "v1-calib" for r in rows))
        self.assertEqual(rows[1]["verdict"], "no-longer-holds")
        self.assertIsNone(rows[0]["knowhow"])
        self.assertNotIn("K-00001", {x["id"] for x in CV.eligible_cards(self.cards(), rows)})


class KnowHowV2(Fixture):
    def card(self):
        return CV.parse_card(card_text(body=CARD_BODY.format(sha=self.sha)), "K-00001.md")

    def test_v2_knowhow_field_is_required_and_parsed(self):
        ans = json.loads(json.dumps(GOOD))
        del ans["knowhow"]
        self.assertTrue(any("knowhow" in e for e in CV.check_answer(ans, self.card(), self.root)))
        ans["knowhow"] = "maybe"
        self.assertTrue(any("knowhow" in e for e in CV.check_answer(ans, self.card(), self.root)))
        ans["knowhow"] = "no"
        self.assertEqual(CV.check_answer(ans, self.card(), self.root), [])
        res = CV.judge_card(self.card(), FakeModel([ans]), self.root, anchors=[], max_retries=0)
        self.assertEqual(res["verdict"], "holds")
        line = CV.ledger_line(dict(self.card(), sha256="0" * 64), res, "sonnet", CV.PROMPT_VERSION, "2026-10-15")
        self.assertEqual((line["knowhow"], line["prompt_version"]), ("no", "v2"))

    def test_the_prompt_asks_the_knowhow_question_in_the_same_call(self):
        model = FakeModel([GOOD])
        CV.judge_card(self.card(), model, self.root, anchors=[], max_retries=0)
        self.assertEqual(len(model.prompts), 1)
        for needle in ("knowhow", "act differently", "status"):
            self.assertIn(needle, model.prompts[0].lower())


class RunAndProposals(Fixture):
    def test_judging_stops_submitting_at_the_dollar_cap(self):
        cards = [CV.parse_card(card_text(f"K-0000{i}", body=CARD_BODY.format(sha=self.sha)), f"K-0000{i}.md")
                 for i in range(1, 6)]

        def model(prompt):
            cid = re.search(r"THE CARD (K-\d+)", prompt).group(1)
            return json.dumps(dict(GOOD, card_id=cid)), {"cost_usd": 0.6}
        seen = []
        done, stopped = CV.judge_many(cards, model, self.root, {}, workers=1, max_usd=1.0,
                                      on_result=lambda c, r: seen.append(c["id"]))
        self.assertTrue(stopped)
        self.assertEqual(seen, ["K-00001", "K-00002"])       # $1.20 > $1.00 after the second: no third call
        self.assertEqual(len(done), 2)

    def test_proposal_rows_carry_the_route_and_the_exact_command(self):
        def res(verdict, path="none", knowhow="yes", sentence="", cmd="grep -n key proj/nav/config/params.yaml"):
            return {"verdict": verdict, "answer": {
                "verdict": verdict, "knowhow": knowhow, "suggested_path": path, "false_sentence": sentence,
                "subclaims": [{"claim": "c", "verdict": verdict, "reason": "value is 1.74 now",
                               "evidence": [{"command": cmd, "exit_code": 0, "output_quote": "1.74"}]}]}}
        wrong = CV.proposal_row("K-1", res("no-longer-holds", "erratum-wrong-record", sentence="It's 1.47."))
        self.assertIn("khms_erratum.py declare --card K-1", wrong["command"])
        self.assertIn("--kind wrong-record", wrong["command"])
        self.assertIn("--by coordinator", wrong["command"])
        self.assertIn("'It'\"'\"'s 1.47.'", wrong["command"])    # shell-quoted, runnable as written
        moved = CV.proposal_row("K-2", res("no-longer-holds", "dated-pointer", sentence="radius 1.47"),
                                value_flag="K-2: states a value the repo holds: min_turning_radius — card says "
                                           "1.47, repo says 1.74 (nav/config/params.yaml:1) — stale value?")
        self.assertIn("--kind dated-pointer", moved["command"])
        self.assertIn("--source 'min_turning_radius in nav/config/params.yaml'", moved["command"])
        sup = CV.proposal_row("K-3", res("no-longer-holds", "supersede", sentence="old mechanism"))
        self.assertIn("SUPERSEDES K-3 BECAUSE", sup["command"])
        self.assertEqual(sup["route"], "supersede")
        nk = CV.proposal_row("K-4", res("holds", knowhow="no"))
        self.assertEqual(nk["route"], "retire-candidate")
        self.assertIn("retire candidate", nk["command"])
        self.assertIsNone(CV.proposal_row("K-5", res("holds")))
        both = CV.proposal_row("K-6", res("no-longer-holds", "supersede", knowhow="no", sentence="x"))
        self.assertIn("retire candidate", both["command"])  # not know-how outranks rewriting it
        lines = CV.proposal_table([wrong, moved, sup, nk])
        self.assertEqual(sum(1 for l in lines if l.startswith("| K-")), 4)

    def test_summary_line_counts_verdicts_knowhow_and_cost(self):
        rows = [{"verdict": "holds", "cost_usd": 0.05, "answer": {"knowhow": "yes"}},
                {"verdict": "no-longer-holds", "cost_usd": 0.04, "answer": {"knowhow": "no"}},
                {"verdict": "cannot-check-offline", "cost_usd": 0.03, "answer": {"knowhow": "yes"}},
                {"verdict": "unverified", "cost_usd": 0.10, "answer": None}]
        self.assertEqual(CV.verdict_line(rows), "VERDICTS holds=1 no-longer-holds=1 cannot-check=1 unverified=1 "
                                                "knowhow-no=1 cost=$0.2200")


if __name__ == "__main__":
    unittest.main()
