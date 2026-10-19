#!/usr/bin/env python3
"""MEMORY.md carries no card (#247).

The always-loaded index used to carry a card digest (section 1, the body of the newest core+khms policy
card), a list of every active core card (section 2) and the top-belief cards (section 4). The principal
decided that cards are found by search, not preloaded: the index now says where the base is and how to
search it, the current focus and the topic registry -- and no card id at all.

RED-first record: against the #247 parent commit both tests failed (the core card's id and title were in
section 2, the policy card's body in section 1).
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

TOOLS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools")
INIT = os.path.join(TOOLS, "khms_init.sh")

CARD = ("---\nid: K-{n:05d}\ntype: {typ}\nlevel: {level}\nstatus: active\ntags: [core, khms, testing]\n"
        "scope: universal\ndate: 2026-01-01\n{extra}---\n# {title}\n{body}\n")


class MemoryIndex(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="memidx-")
        self.addCleanup(shutil.rmtree, self.root, True)
        subprocess.run([INIT, self.root], capture_output=True, text=True, check=True)
        know = os.path.join(self.root, "memory", "know")
        with open(os.path.join(know, "K-90001.md"), "w", encoding="utf-8") as f:
            f.write(CARD.format(n=90001, typ="fact", level="observation", extra="evidence: measured\nsource: s\n",
                                title="A core fact about the harness",
                                body="The sensor harness picks up noise from the motor leads."))
        with open(os.path.join(know, "K-90002.md"), "w", encoding="utf-8") as f:
            f.write(CARD.format(n=90002, typ="policy", level="derived", extra="links: {derived_from: [K-90001]}\n",
                                title="How this base works",
                                body="RULE: search the base before every hypothesis; see K-90001."))
        env = dict(os.environ, KHMS_ROOT=self.root)
        self.run_ = subprocess.run([sys.executable, os.path.join(TOOLS, "build_views.py")],
                                   capture_output=True, text=True, env=env, timeout=120)
        with open(os.path.join(self.root, "memory", "MEMORY.md"), encoding="utf-8") as f:
            self.text = f.read()

    def test_the_index_names_no_card(self):
        self.assertEqual(self.run_.returncode, 0, self.run_.stdout + self.run_.stderr)
        self.assertEqual(re.findall(r"K-\d{5}", self.text), [])
        self.assertNotIn("A core fact about the harness", self.text)
        self.assertNotIn("search the base before every hypothesis; see", self.text)

    def test_the_index_says_where_the_base_is_and_how_to_search_it(self):
        self.assertIn("## 1 Where the base is and how to search it", self.text)
        self.assertIn("tools/recall.sh", self.text)
        self.assertIn("memory/views/topics/<tag>.md", self.text)
        self.assertIn("2 active cards", self.text)
        self.assertIn("## 2 Current focus", self.text)
        self.assertIn("## 3 Topics", self.text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
