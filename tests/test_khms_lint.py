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
import importlib.util
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
            f.write("---\nid: K-90042\n---\nthe old card\n")

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


def load_hook():
    spec = importlib.util.spec_from_file_location(
        "khms_hook_under_test", os.path.join(TOOLS, "khms_hook.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


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
    def setUp(self):
        self.hook = load_hook()

    def build(self, cards, served):
        text, used = self.hook.build_injection([(40.0, [], served)], cards, {}, 1000.0, [])
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
        big = card("K-90042", "x" * 800)
        old_cap, self.hook.CHAR_CAP = self.hook.CHAR_CAP, 200
        try:
            text, _ = self.build([big, NEW], big)
        finally:
            self.hook.CHAR_CAP = old_cap
        self.assertIn("! CORRECTED BY K-90300", text)
        self.assertLess(len(text), 200 + 2 * self.hook.WARN_CHARS + 80)

    def test_at_most_two_pointers(self):
        cors = [card(f"K-0030{i}", f"corrector {i}", links={"contradicts": ["K-90042"]})
                for i in range(4)]
        text, _ = self.build([OLD] + cors, OLD)
        self.assertEqual(text.count("! CORRECTED BY"), self.hook.MAX_WARN)

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
