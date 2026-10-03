#!/usr/bin/env python3
"""Acceptance tests for tools/nearest_cards.py.

Run:  python3 -m pytest tests/test_nearest_cards.py

The mechanical half of "merge pressure": before the consolidate stage is asked which
existing card a candidate supersedes or supports, it is TOLD which cards are nearest.
Zero model tokens, so it can run on every candidate of every night.

Four properties are guarded:
  (1) every candidate comes back with exactly one NEAREST line naming existing ids;
  (2) an ARCHIVED or non-active card is never proposed — a supersedes edge onto a
      retired card chains onto a dead end;
  (3) running it twice changes nothing, so a re-run in the pipeline or by hand during
      review cannot pile lines up;
  (4) `--only-missing` preserves a line carried through from the sweep.
The base is a temp directory; nothing here reads or writes a real one.
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

TOOLS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools")
NEAREST = os.path.join(TOOLS, "nearest_cards.py")
CARD_RE = re.compile(r"^#{2,3} +([A-Z]\d+[a-z]?):", re.M)
NEAREST_RE = re.compile(r"^NEAREST: (.+)$", re.M)

CARD = """\
---
id: %s
type: %s
level: observation
status: %s
tags: [testing]
scope: universal
evidence: measured
source: 'journal/2026-03-04.md line 1'
date: 2026-03-04
links:
  derived_from: []
  supports: []
  contradicts: []
  supersedes: null
  refuted_by: []
---
%s
"""

BODIES = {
    "K-90001": "SYMPTOM: humidity frames vanish at the hour boundary during log rotation.",
    "K-90002": "SYMPTOM: the barometer checksum fails whenever the serial link renegotiates.",
    "K-90003": "SYMPTOM: battery percentage reads zero after a cold restart of the gateway.",
    "K-90004": "SYMPTOM: anemometer counts double under strong gusts on the north mast.",
}
ARCHIVED = {"K-90009": "SYMPTOM: humidity frames vanish at the hour boundary during log rotation."}
INACTIVE = {"K-90008": "SYMPTOM: the barometer checksum fails whenever the serial link renegotiates."}

SWEEP = """\
# nightly sweep — candidates

## Cards

### C1: rotation eats humidity frames again
SYMPTOM: humidity frames vanish at the hour boundary during log rotation.

### C2: checksum failures on the serial link
SYMPTOM: the barometer checksum fails whenever the serial link renegotiates.

## Flagged

- nothing to see here, and this section must not become a candidate
"""


def base(tmp):
    know = os.path.join(tmp, "memory", "know")
    arch = os.path.join(tmp, "memory", "archive", "know")
    os.makedirs(know)
    os.makedirs(arch)
    os.makedirs(os.path.join(tmp, "state", "shared"), exist_ok=True)
    for cid, body in BODIES.items():
        open(os.path.join(know, cid + ".md"), "w").write(
            CARD % (cid, "problem→solution", "active", body))
    for cid, body in INACTIVE.items():
        open(os.path.join(know, cid + ".md"), "w").write(
            CARD % (cid, "problem→solution", "superseded", body))
    for cid, body in ARCHIVED.items():
        open(os.path.join(arch, cid + ".md"), "w").write(
            CARD % (cid, "problem→solution", "active", body))
    return know, arch


def run(root, *args):
    env = dict(os.environ, KHMS_ROOT=root)
    return subprocess.run([sys.executable, NEAREST, *args],
                          capture_output=True, text=True, env=env)


class Annotate(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="nearest-")
        base(cls.tmp)
        cls.path = os.path.join(cls.tmp, "sweep.md")
        open(cls.path, "w", encoding="utf-8").write(SWEEP)
        cls.r1 = run(cls.tmp, cls.path)
        cls.after1 = open(cls.path, encoding="utf-8").read()
        cls.r2 = run(cls.tmp, cls.path)
        cls.after2 = open(cls.path, encoding="utf-8").read()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_exit_zero(self):
        self.assertEqual(self.r1.returncode, 0, self.r1.stdout + self.r1.stderr)

    def test_one_nearest_line_per_candidate(self):
        n = len(CARD_RE.findall(self.after1))
        self.assertEqual(n, 2)
        self.assertEqual(len(NEAREST_RE.findall(self.after1)), n)

    def test_nearest_names_existing_active_cards_with_scores(self):
        know = os.path.join(self.tmp, "memory", "know")
        for line in NEAREST_RE.findall(self.after1):
            ids = re.findall(r"K-\d{5}", line)
            self.assertTrue(ids, line)
            self.assertLessEqual(len(ids), 3, line)
            self.assertRegex(line, r"K-\d{5} \([\d.]+\)")
            for cid in ids:
                self.assertTrue(os.path.exists(os.path.join(know, cid + ".md")),
                                f"{cid} named by NEAREST is not a live card")

    def test_the_top_hit_is_the_card_about_the_same_symptom(self):
        lines = NEAREST_RE.findall(self.after1)
        self.assertIn("K-90001", lines[0])
        self.assertIn("K-90002", lines[1])

    def test_archived_and_retired_cards_are_never_proposed(self):
        blob = " ".join(NEAREST_RE.findall(self.after1))
        self.assertNotIn("K-90009", blob, "an archived card was proposed")
        self.assertNotIn("K-90008", blob, "a superseded card was proposed")

    def test_rerun_is_byte_identical(self):
        self.assertEqual(self.after1, self.after2)

    def test_a_trailing_section_is_not_absorbed_into_the_last_candidate(self):
        # the `## Flagged` section must still be there, and must not have been
        # annotated as if it were a candidate
        self.assertIn("## Flagged", self.after1)
        tail = self.after1[self.after1.index("## Flagged"):]
        self.assertNotIn("NEAREST:", tail)


class OnlyMissing(unittest.TestCase):
    """--only-missing preserves a NEAREST line the consolidate stage carried through
    from the sweep, and computes one only where none exists."""

    def test_only_missing(self):
        tmp = tempfile.mkdtemp(prefix="nearest-om-")
        self.addCleanup(shutil.rmtree, tmp, True)
        base(tmp)
        path = os.path.join(tmp, "inbox.md")
        open(path, "w", encoding="utf-8").write(
            "## Cards\n\n"
            "### C1: carried through\n"
            "SYMPTOM: humidity frames vanish at the hour boundary during log rotation.\n"
            "NEAREST: K-90003 (1.0), K-90004 (2.0)\n\n"
            "### C2: nothing yet\n"
            "SYMPTOM: the barometer checksum fails whenever the serial link renegotiates.\n")
        r = run(tmp, path, "--only-missing")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        out = open(path, encoding="utf-8").read()
        self.assertIn("NEAREST: K-90003 (1.0), K-90004 (2.0)", out,
                      "a carried-through line was recomputed")
        self.assertEqual(len(NEAREST_RE.findall(out)), 2)

    def test_a_file_with_no_candidates_is_an_error_not_a_silent_success(self):
        tmp = tempfile.mkdtemp(prefix="nearest-empty-")
        self.addCleanup(shutil.rmtree, tmp, True)
        base(tmp)
        path = os.path.join(tmp, "empty.md")
        open(path, "w", encoding="utf-8").write("# nothing here\n\nprose only\n")
        r = run(tmp, path)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("no '### C<n>' candidates", r.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
