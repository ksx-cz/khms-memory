#!/usr/bin/env python3
"""Grounding: what counts as a quote, and what a card may claim on its strength.

Two properties, both of them discriminating:

  * A SHORTENED quote is legitimate when the omission is MARKED, and is a splice
    when it is not. The difference matters because a spliced quote asserts an
    adjacency nobody wrote — so for every accepted elision here there is a control
    that differs only by removing the mark, and must fail.
  * `src=K-NNNNN` resolves to that card's own file. The weekly stage reasons over
    existing cards and cites them this way; without the resolution its every quote
    comes back "unknown source", which reads as a broken stage rather than a
    missing feature.

RED-first record: against the pre-change verify_quotes.py the elision cases failed
(no marked-elision support at all) and every card-source case failed with
"unknown source".
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

TOOLS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools")
VERIFY = os.path.join(TOOLS, "verify_quotes.py")

SOURCE = (
    "MARK[solved] humidity gaps were log rotation, not the sensor - the writer kept "
    "the old handle\nand the summary job re-reads the rotated file after the flush "
    "interval completes.\n")

CARD = """\
## Cards

### C1: a card
SYMPTOM: gaps at the hour boundary.

**QUOTES:**
- src=%s :: %s
"""


class Quotes(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="vq-")
        self.addCleanup(shutil.rmtree, self.d, True)
        self.know = os.path.join(self.d, "memory", "know")
        os.makedirs(self.know)
        self.src = os.path.join(self.d, "journal.md")
        open(self.src, "w", encoding="utf-8").write(SOURCE)
        self.cards = os.path.join(self.d, "cards.md")

    def run_vq(self, src, quote, *extra):
        open(self.cards, "w", encoding="utf-8").write(CARD % (src, quote))
        env = dict(os.environ, KHMS_ROOT=self.d)
        return subprocess.run(
            [sys.executable, VERIFY, self.cards,
             f"--source=journal={self.src}", *extra],
            capture_output=True, text=True, env=env)

    # ------------------------------------------------------------ classification
    def test_an_exact_quote_passes_silently(self):
        r = self.run_vq("journal", "humidity gaps were log rotation, not the sensor")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("unverified=0", r.stdout)

    def test_a_retypeset_quote_passes_as_wrapped(self):
        r = self.run_vq("journal", "**humidity gaps were log rotation**, not the sensor")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("WRAPPED", r.stdout)

    def test_a_marked_elision_passes_and_says_so(self):
        r = self.run_vq("journal",
                        "humidity gaps were log rotation ... the summary job "
                        "re-reads the rotated file")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("ELIDED", r.stdout)
        self.assertIn("max gap", r.stdout)

    def test_the_same_quote_without_the_mark_is_a_splice(self):
        # the control: identical text, one mark removed
        r = self.run_vq("journal",
                        "humidity gaps were log rotation the summary job re-reads "
                        "the rotated file")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("FAIL quote not found", r.stdout)

    def test_elided_segments_too_short_to_carry_evidence_fail(self):
        r = self.run_vq("journal", "hum ... the ... file")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("too short to carry evidence", r.stdout)

    def test_a_bracketed_insertion_is_allowed_but_a_long_one_is_content(self):
        ok = self.run_vq("journal",
                         "humidity gaps were log rotation, not the [humidity] sensor")
        self.assertEqual(ok.returncode, 0, ok.stdout)
        bad = self.run_vq(
            "journal",
            "humidity gaps were log rotation, not the [a whole clause the source "
            "never contained] sensor")
        self.assertEqual(bad.returncode, 1, bad.stdout)

    def test_a_quote_found_in_another_source_says_where_without_advising_a_relabel(self):
        other = os.path.join(self.d, "digest.txt")
        open(other, "w", encoding="utf-8").write(SOURCE)
        open(self.cards, "w", encoding="utf-8").write(
            CARD % ("digest", "humidity gaps were log rotation, not the sensor"))
        env = dict(os.environ, KHMS_ROOT=self.d)
        r = subprocess.run([sys.executable, VERIFY, self.cards,
                            f"--source=journal={self.src}",
                            f"--source=digest={os.path.join(self.d, 'empty.txt')}"],
                           capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("text IS present in: journal", r.stdout)
        self.assertNotIn("relabel", r.stdout.lower())

    # --------------------------------------------------------------- card sources
    def card(self, cid, body):
        open(os.path.join(self.know, cid + ".md"), "w", encoding="utf-8").write(
            f"---\nid: {cid}\ntype: fact\nlevel: observation\nstatus: active\n"
            f"tags: [testing]\n---\n{body}\n")

    def test_a_card_id_source_resolves_to_that_card(self):
        self.card("K-90001", "the mast is four metres tall and guyed at three points.")
        r = self.run_vq("K-90001", "the mast is four metres tall")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("unknown-source=0", r.stdout)

    def test_a_claim_about_a_card_that_the_card_does_not_make_fails(self):
        self.card("K-90001", "the mast is four metres tall and guyed at three points.")
        r = self.run_vq("K-90001", "the mast is six metres tall")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("FAIL quote not found in K-90001", r.stdout)

    def test_a_card_id_that_does_not_exist_warns(self):
        r = self.run_vq("K-90404", "anything at all here")
        self.assertIn("which is in neither", r.stdout)

    def test_an_archived_card_still_resolves(self):
        arch = os.path.join(self.d, "memory", "archive", "know")
        os.makedirs(arch)
        open(os.path.join(arch, "K-90002.md"), "w", encoding="utf-8").write(
            "---\nid: K-90002\nstatus: superseded\n---\n"
            "the anemometer double-counts under strong gusts.\n")
        r = self.run_vq("K-90002", "the anemometer double-counts under strong gusts")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
