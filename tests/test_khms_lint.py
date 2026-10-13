#!/usr/bin/env python3
"""Regression guard for the correction-edge mechanism.

Run:  python3 tests/test_khms_lint.py   (or: python3 -m pytest tests/test_khms_lint.py)

Two halves of one rule — a correction must be reachable from the card it
corrects:

  (a) khms_lint refuses correction language with no backward edge, and
      approve_inbox.py runs it BEFORE allocating a single id, so a refused run
      leaves know/ and the counter exactly as they were;
  (b) build_injection serves every card together with whatever corrects it,
      whatever the corrected card's status — the gap that let a superseded claim
      be served as current for four days in the source deployment, because the
      old card was still `active` and the bundling was gated on status.

Nothing here touches a real base: everything runs in a temporary KHMS_ROOT.
"""
import os
import subprocess
import sys
import tempfile
import unittest

TOOLS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools")
sys.path.insert(0, TOOLS)
import khms_lint as L  # noqa: E402

CORRECTION_BODY = (
    "Real culprit corrected — the sensor branch is healthy\n\n"
    "CORRECTED (operator, in person): the branch is healthy; the new device is the culprit.\n"
)


def meta(**links):
    m = {"id": "C1", "type": "fact", "level": "observation", "status": "active",
         "tags": ["sensor"], "scope": "universal", "date": "2026-03-04"}
    base = {"derived_from": [], "supports": [], "contradicts": [],
            "refuted_by": [], "supersedes": None}
    base.update(links)
    m["links"] = base
    return m


class LintRule(unittest.TestCase):
    def test_correction_without_edge_is_refused(self):
        msg = L.check_correction_edges("C1", meta(), CORRECTION_BODY, {"K-90042"})
        self.assertIsNotNone(msg)
        self.assertIn("links.contradicts", msg)      # the refusal NAMES the edge
        self.assertIn("links.supersedes", msg)
        self.assertIn("refuted_by", msg)

    def test_correction_with_an_edge_passes(self):
        for links in ({"contradicts": ["K-90042"]}, {"supersedes": "K-90042"},
                      {"refuted_by": ["K-90042"]}):
            self.assertIsNone(L.check_correction_edges(
                "C1", meta(**links), CORRECTION_BODY, {"K-90042"}), links)

    def test_edge_pointing_at_nothing_is_refused(self):
        msg = L.check_correction_edges("C1", meta(contradicts=["K-99999"]),
                                       CORRECTION_BODY, {"K-90042"})
        self.assertIn("points at nothing", msg)

    def test_stray_top_level_edge_counts(self):
        m = meta()
        m["contradicts"] = ["K-90042"]
        self.assertIsNone(L.check_correction_edges("C1", m, CORRECTION_BODY,
                                                   {"K-90042"}))

    def test_escape_hatch(self):
        body = CORRECTION_BODY + "\nNO-CORRECTION-TARGET: never carded.\n"
        self.assertIsNone(L.check_correction_edges("C1", meta(), body, None))

    def test_a_repair_is_not_a_correction(self):
        """The lint must stay quiet on ordinary maintenance prose, or it gets
        switched off: a flat word list fired on 22 % of the source base."""
        for body in ("FIX: the cable was replaced and the errors stopped.\n",
                     "THEN: the harness was corrected on the bench.\n"):
            self.assertEqual(L.correction_terms(body), [], body)

    def test_a_correction_of_the_record_fires(self):
        body = "The claim in card K-90042 is no longer true: the part was fitted.\n"
        self.assertTrue(L.correction_terms(body))
        self.assertIsNotNone(L.check_correction_edges("C1", meta(), body, None))


class NarrowedToTheCardBeingCorrected(unittest.TestCase):
    """#237: the refusals a human answered with NO-CORRECTION-TARGET (124 of 136 on the
    deployment's base) came from lines that MENTION correcting or cards without stating
    that a named card is wrong.  Each case here was refused before the narrowing; each
    sibling is a real correction with no edge and must still be refused."""

    def refused(self, body):
        return L.check_correction_edges("C1", meta(), body, None)

    def test_an_ordinary_repair_beside_a_card_id_is_not_a_correction(self):
        for body in ("FIX: fixed the script; K-90042 records the original bug.\n",
                     "We corrected the cron line in the deploy script; the knowledge base "
                     "card K-90042 describes the job.\n"):
            self.assertIsNone(self.refused(body), body)

    def test_the_escape_hatch_named_in_prose_is_not_a_correction(self):
        body = ("Both refusals of K-90042's batch were answered with a NO-CORRECTION-TARGET "
                "line, as the gate asks.\n")
        self.assertIsNone(self.refused(body))

    def test_quotes_code_and_annotations_are_not_this_cards_claim(self):
        body = ("The edge is written as `supersedes: K-90042` in the frontmatter.\n"
                "<!-- dropped: malformed relation, expected 'supersedes K-xxxxx because' -->\n"
                "**QUOTES:**\n- src=journal :: CORRECTED the timer in K-90042's unit file\n")
        self.assertIsNone(self.refused(body))

    def test_a_real_correction_of_a_named_card_is_still_refused(self):
        for body in ("K-90042 was wrong: the topic is /plant/rh, not /rh.\n",
                     "The claim in K-90042 no longer holds since the probe moved.\n",
                     "This card corrected the wiring note of K-90042.\n",
                     "CORRECTED (operator): the probe is on bus 2.\n"):
            self.assertIsNotNone(self.refused(body), body)


def _terms_with_patterns(tmp, patterns, body):
    """correction_terms() in a fresh interpreter that reads `patterns` as the private file."""
    pat = os.path.join(tmp, "patterns.env")
    with open(pat, "w", encoding="utf-8") as f:
        f.write(patterns)
    code = ("import sys; sys.path.insert(0, %r); import khms_lint as L; "
            "print(L.correction_terms(sys.stdin.read()))" % TOOLS)
    env = {k: v for k, v in os.environ.items() if not k.startswith("KHMS_")}
    env["KHMS_PATTERNS"] = pat
    r = subprocess.run([sys.executable, "-c", code], input=body, env=env,
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip(), r.stderr


class DeploymentVocabulary(unittest.TestCase):
    """A deployment's repair stem (private patterns file) is a WEAK word: beside a bare card
    id it names an activity; beside a definite reference ("this card") it corrects."""

    def test_weak_stem_needs_a_definite_reference(self):
        with tempfile.TemporaryDirectory() as tmp:
            pats = "LINT_WEAK_1=repairo::\\brepairo\\b\n"
            bare, _ = _terms_with_patterns(tmp, pats, "Did the repairo of the timer, see K-90042.\n")
            self.assertEqual(bare, "[]")
            definite, _ = _terms_with_patterns(tmp, pats, "This card is the repairo of K-90042.\n")
            self.assertIn("repairo", definite)

    def test_the_retired_generic_record_key_is_reported_not_silently_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            out, err = _terms_with_patterns(
                tmp, "LINT_RECORD_CTX=\\bdossier\n",
                "We corrected the timer; the dossier lists it.\n")
            self.assertEqual(out, "[]")
            self.assertIn("LINT_RECORD_CTX", err)


CARD = """---
id: T1
type: fact
level: observation
status: active
tags: [sensor]
scope: universal
evidence: reported
source: 'journal 2026-03-04'
date: 2026-03-04
links:
  derived_from: []
  supports: []
  contradicts: [%s]
  refuted_by: []
  supersedes: null
---
Real culprit corrected — the sensor branch is healthy

CORRECTED (operator): the branch is healthy and the earlier decision no longer applies.
"""


class ApproveRefuses(unittest.TestCase):
    """approve_inbox.py end to end, against a throwaway KHMS_ROOT."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.know = os.path.join(self.root, "memory", "know")
        os.makedirs(self.know)
        os.makedirs(os.path.join(self.root, "state", "shared"), exist_ok=True)
        self.counter = os.path.join(self.root, "state", "shared", "next_id")
        with open(self.counter, "w") as f:
            f.write("90500\n")
        with open(os.path.join(self.know, "K-90042.md"), "w") as f:
            f.write("---\nid: K-90042\ntype: fact\nlevel: observation\nstatus: active\ntags: [testing]\n"
                    "scope: universal\nevidence: measured\nsource: fixture\ndate: 2026-01-01\n---\nthe old card\n")

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, text):
        src = os.path.join(self.root, "inbox.md")
        with open(src, "w", encoding="utf-8") as f:
            f.write(text)
        env = dict(os.environ, KHMS_ROOT=self.root)
        return subprocess.run(
            [sys.executable, os.path.join(TOOLS, "approve_inbox.py"), src],
            capture_output=True, text=True, env=env)

    def test_refuses_and_writes_nothing(self):
        r = self._run(CARD % "")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("REFUSED", r.stdout)
        self.assertIn("links.contradicts", r.stdout)
        self.assertFalse(os.path.exists(os.path.join(self.know, "K-90500.md")))
        self.assertEqual(open(self.counter).read().strip(), "90500")   # no id burnt

    def test_accepts_the_same_card_with_the_edge(self):
        r = self._run(CARD % "K-90042")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        written = os.path.join(self.know, "K-90500.md")
        self.assertTrue(os.path.exists(written))
        self.assertIn("contradicts: [K-90042]", open(written).read())


def card(cid, first, status="active", links=None, tags=("sensor",)):
    links = dict({"derived_from": [], "supports": [], "contradicts": [],
                  "refuted_by": [], "supersedes": None}, **(links or {}))
    body = first + "\nbody text\n"
    return {"id": cid, "status": status, "level": "observation", "tags": list(tags),
            "ntags": ",".join(tags), "links": links, "body": body,
            "nbody": body.lower(), "first": first, "fog": False, "path": cid}


OLD = card("K-90042", "The sensor branch has no pull-ups — keep the device disconnected")
NEW = card("K-90300", "Real culprit corrected — the branch is healthy, the new device is at fault",
           links={"contradicts": ["K-90042"]})


class ReverseInjection(unittest.TestCase):
    """The injection itself lives in khms_select (the hook only calls it); tested there, with the V2 params
    the hook serves and the shipped English wording (the conftest strips every KHMS_* variable, so no
    deployment's messages file can be in force)."""

    def setUp(self):
        import khms_select
        self.sel = khms_select

    def build(self, cards, served, params=None):
        text, used = self.sel.build_injection([(40.0, [], served)], cards, {}, 1000.0,
                                              params or self.sel.V2, [])
        return text or "", used

    def test_active_card_carries_its_corrector(self):
        text, used = self.build([OLD, NEW], OLD)
        self.assertIn("! CORRECTED BY K-90300", text)
        self.assertEqual(used, ["K-90042"])   # a pointer eats no slot, is not "injected"

    def test_card_with_no_corrector_is_unchanged(self):
        lone = card("K-90777", "Something else entirely")
        text, _ = self.build([lone, NEW], lone)
        self.assertNotIn("CORRECTED BY", text)

    def test_forward_refuted_by_also_counts(self):
        old = card("K-90043", "An old claim", links={"refuted_by": ["K-90300"]})
        text, _ = self.build([old, NEW], old)
        self.assertIn("! CORRECTED BY K-90300", text)

    def test_refuted_card_keeps_the_full_corrector_line(self):
        old = card("K-90042", "Old claim", status="refuted",
                   links={"refuted_by": ["K-90300"]})
        text, used = self.build([old, NEW], old)
        self.assertIn("-> corrected by: K-90300", text)
        self.assertIn("K-90300", used)
        self.assertEqual(text.count("K-90300"), 1)   # not repeated as a pointer

    def test_pointer_survives_a_full_budget(self):
        import dataclasses
        big = card("K-90042", "x" * 800)
        text, _ = self.build([big, NEW], big, dataclasses.replace(self.sel.V2, char_cap=200))
        self.assertIn("! CORRECTED BY K-90300", text)
        self.assertLess(len(text), 200 + 2 * self.sel.WARN_CHARS + 80)

    def test_at_most_two_pointers(self):
        cors = [card(f"K-0030{i}", f"corrector {i}", links={"contradicts": ["K-90042"]})
                for i in range(4)]
        text, _ = self.build([OLD] + cors, OLD)
        self.assertEqual(text.count("! CORRECTED BY"), self.sel.MAX_WARN)

    def test_malformed_links_do_not_raise(self):
        broken = card("K-90042", "Old claim")
        broken["links"] = {"supports": []}
        text, _ = self.build([broken, NEW], broken)
        self.assertIn("K-90042", text)


class HookProcess(unittest.TestCase):
    """The real binary: it must emit, and it must stay silent when switched off."""

    def _run(self, prompt, root, **env_extra):
        env = dict(os.environ, KHMS_ROOT=root, **env_extra)
        return subprocess.run([sys.executable, os.path.join(TOOLS, "khms_hook.py")],
                              input='{"hook_event_name":"UserPromptSubmit",'
                                    '"session_id":"t","prompt":"%s"}' % prompt,
                              capture_output=True, text=True, env=env, timeout=60)

    def test_kill_switch(self):
        with tempfile.TemporaryDirectory() as d:
            r = self._run("anything at all here", d, KHMS_HOOKS_OFF="1")
            self.assertEqual(r.returncode, 0)
            self.assertEqual(r.stdout.strip(), "")

    def test_empty_base_is_silent_and_clean(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "memory", "know"))
            os.makedirs(os.path.join(d, "state", "shared"), exist_ok=True)
            r = self._run("a question about a sensor branch", d)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertNotIn("Traceback", r.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
