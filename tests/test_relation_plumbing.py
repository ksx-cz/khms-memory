#!/usr/bin/env python3
"""The withheld sections are review plumbing, and the approve stage must not read them.

`verify_relations.py` does not delete a candidate it rejects — it MOVES the whole block
into `## DROPPED (no valid RELATION)` or `## DEFERRED` so a human can rescue it. That is
only worth anything if the stage that WRITES cards refuses to read those sections: a
parser that scans the whole file would approve exactly the candidates the gate rejected,
and the gate would then be worse than nothing.

RED-first record: against the pre-change approve_inbox.py both tests below failed —
the dropped candidate was written into memory/know/ with a real id.
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

TOOLS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools")
APPROVE = os.path.join(TOOLS, "approve_inbox.py")
VERIFY = os.path.join(TOOLS, "verify_relations.py")

CARD = """\
### %s: %s

```yaml
id: %s
type: fact
level: observation
status: active
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

**QUOTES:**
- src=journal :: %s
```
"""


class Withheld(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="withheld-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.know = os.path.join(self.root, "memory", "know")
        os.makedirs(self.know)
        os.makedirs(os.path.join(self.root, "state", "shared"), exist_ok=True)
        self.counter = os.path.join(self.root, "state", "shared", "next_id")
        open(self.counter, "w").write("90500\n")
        self.inbox = os.path.join(self.root, "inbox.md")

    def approve(self):
        env = dict(os.environ, KHMS_ROOT=self.root)
        return subprocess.run([sys.executable, APPROVE, self.inbox],
                              capture_output=True, text=True, env=env)

    def write(self, text):
        open(self.inbox, "w", encoding="utf-8").write(text)

    def test_a_dropped_candidate_never_becomes_a_card(self):
        self.write(
            "## Cards\n\n"
            + CARD % ("C1", "kept", "C1", "the gateway reboots at 03:00 daily.",
                      "the gateway reboots at 03:00 daily.")
            + "\n## DROPPED (no valid RELATION)\n\n"
            + CARD % ("C2", "dropped", "C2", "the mast is four metres tall.",
                      "the mast is four metres tall."))
        r = self.approve()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        written = sorted(os.listdir(self.know))
        self.assertEqual(len(written), 1, f"expected one card, got {written}")
        body = open(os.path.join(self.know, written[0]), encoding="utf-8").read()
        self.assertIn("gateway reboots", body)
        self.assertNotIn("four metres", body)

    def test_a_deferred_candidate_never_becomes_a_card(self):
        self.write(
            "## Cards\n\n"
            + CARD % ("C1", "kept", "C1", "the gateway reboots at 03:00 daily.",
                      "the gateway reboots at 03:00 daily.")
            + "\n## DEFERRED\n\n"
            + CARD % ("C2", "deferred", "C2", "the mast is four metres tall.",
                      "the mast is four metres tall."))
        r = self.approve()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(len(os.listdir(self.know)), 1)

    def test_the_withholding_is_reported_not_silent(self):
        self.write(
            "## Cards\n\n"
            + CARD % ("C1", "kept", "C1", "the gateway reboots at 03:00 daily.",
                      "the gateway reboots at 03:00 daily.")
            + "\n## DROPPED (no valid RELATION)\n\n"
            + CARD % ("C2", "dropped", "C2", "the mast is four metres tall.",
                      "the mast is four metres tall."))
        r = self.approve()
        self.assertIn("withheld: 1 candidate", r.stdout)
        self.assertIn("DROPPED", r.stdout)

    def test_a_file_with_no_withheld_section_is_unaffected(self):
        self.write("## Cards\n\n"
                   + CARD % ("C1", "kept", "C1", "the gateway reboots at 03:00 daily.",
                             "the gateway reboots at 03:00 daily."))
        r = self.approve()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("withheld:", r.stdout)
        self.assertEqual(len(os.listdir(self.know)), 1)


class GateWritesWhatApproveRefusesToRead(unittest.TestCase):
    """End to end: the gate moves a candidate, the approve stage then ignores it."""

    def test_round_trip(self):
        root = tempfile.mkdtemp(prefix="roundtrip-")
        self.addCleanup(shutil.rmtree, root, True)
        know = os.path.join(root, "memory", "know")
        os.makedirs(know)
        os.makedirs(os.path.join(root, "state", "shared"), exist_ok=True)
        open(os.path.join(root, "state", "shared", "next_id"), "w").write("90500\n")
        open(os.path.join(know, "K-90001.md"), "w").write(
            "---\nid: K-90001\nstatus: active\ntype: fact\nlevel: observation\nscope: universal\n"
            "tags: [testing]\nevidence: measured\nsource: fixture\ndate: 2026-01-01\n---\nbody\n")
        inbox = os.path.join(root, "inbox.md")
        open(inbox, "w", encoding="utf-8").write(
            "## Cards\n\n"
            + CARD % ("C1", "has a relation", "C1",
                      "NEAREST: K-90001 (9.9)\nRELATION: supports K-90001\n"
                      "the gateway reboots at 03:00 daily.",
                      "the gateway reboots at 03:00 daily.")
            + CARD % ("C2", "no relation at all", "C2",
                      "the mast is four metres tall.",
                      "the mast is four metres tall."))
        g = subprocess.run([sys.executable, VERIFY, inbox, "--know", know,
                            "--archive", os.path.join(root, "nonexistent")],
                           capture_output=True, text=True)
        self.assertEqual(g.returncode, 1, g.stdout)          # 1 == something dropped
        self.assertIn("C2: DROP", g.stdout)
        text = open(inbox, encoding="utf-8").read()
        self.assertIn("## DROPPED (no valid RELATION)", text)
        self.assertIn("four metres", text, "the dropped candidate must still be there")

        env = dict(os.environ, KHMS_ROOT=root)
        r = subprocess.run([sys.executable, APPROVE, inbox],
                           capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        cards = [f for f in os.listdir(know) if f != "K-90001.md"]
        self.assertEqual(len(cards), 1, f"expected exactly one new card, got {cards}")
        self.assertIn("gateway reboots",
                      open(os.path.join(know, cards[0]), encoding="utf-8").read())


class LinksLine(unittest.TestCase):
    """`**LINKS:** supports=[K-x]` in the body must reach `links:` in the card.

    The consolidate prompt calls this line "the ONLY channel by which a link you
    spotted reaches the knowledge graph". If nothing folds it into the frontmatter
    the sentence is false and every edge a stage found is dropped in silence — no
    error, no warning, a graph that simply never grows an edge. RED-first record:
    against the pre-change approve_inbox.py the first three tests failed.
    """

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="linksline-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.know = os.path.join(self.root, "memory", "know")
        os.makedirs(self.know)
        os.makedirs(os.path.join(self.root, "state", "shared"), exist_ok=True)
        open(os.path.join(self.root, "state", "shared", "next_id"), "w").write("90500\n")
        self.inbox = os.path.join(self.root, "inbox.md")

    def approve(self, line):
        open(self.inbox, "w", encoding="utf-8").write(
            "## Cards\n\n" + CARD % ("C1", "with links", "C1",
                                      "%s\nthe gateway reboots at 03:00 daily." % line,
                                      "the gateway reboots at 03:00 daily."))
        env = dict(os.environ, KHMS_ROOT=self.root)
        r = subprocess.run([sys.executable, APPROVE, self.inbox],
                           capture_output=True, text=True, env=env)
        written = [f for f in os.listdir(self.know)]
        card = open(os.path.join(self.know, written[0]), encoding="utf-8").read() \
            if written else ""
        return r, card

    def test_supports_reaches_the_frontmatter(self):
        _r, card = self.approve("**LINKS:** supports=[K-90001]")
        self.assertIn("supports:", card)
        self.assertIn("K-90001", card.split("---")[1])

    def test_several_keys_on_one_line(self):
        _r, card = self.approve("**LINKS:** supports=[K-90001] contradicts=[K-90002]")
        front = card.split("---")[1]
        self.assertIn("K-90001", front)
        self.assertIn("K-90002", front)

    def test_the_line_is_stripped_from_the_body(self):
        _r, card = self.approve("**LINKS:** supports=[K-90001]")
        self.assertNotIn("**LINKS:**", card)

    def test_an_unknown_key_is_dropped_loudly(self):
        r, card = self.approve("**LINKS:** related=[K-90001]")
        self.assertIn("dropped 'related='", r.stdout)
        self.assertNotIn("K-90001", card.split("---")[1])

    def test_a_card_without_the_line_is_unaffected(self):
        r, card = self.approve("no links here")
        self.assertNotIn("LINKS line:", r.stdout)
        self.assertIn("supports: []", card)


class SupersedeMarking(unittest.TestCase):
    """The other half of a supersedes edge, written on the TARGET.

    An edge on the new card alone leaves the old one `status: active`, and the
    injection layer then serves it as the current state of the world. RED-first
    record: against the pre-change approve_inbox.py the first two tests failed —
    the target kept `status: active` and had no `superseded_by`.
    """

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="supersede-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.know = os.path.join(self.root, "memory", "know")
        os.makedirs(self.know)
        os.makedirs(os.path.join(self.root, "state", "shared"), exist_ok=True)
        open(os.path.join(self.root, "state", "shared", "next_id"), "w").write("90500\n")
        self.inbox = os.path.join(self.root, "inbox.md")

    def target(self, cid, status="active"):
        open(os.path.join(self.know, cid + ".md"), "w", encoding="utf-8").write(
            "---\nid: %s\ntype: fact\nlevel: observation\nstatus: %s\n"
            "scope: universal\nevidence: measured\nsource: fixture\ndate: 2026-01-01\ntags: [testing]\nlinks:\n  derived_from: []\n  supports: []\n"
            "  contradicts: []\n  supersedes: null\n  refuted_by: []\n---\n"
            "the mast is four metres tall.\n" % (cid, status))

    def approve(self, link_line):
        open(self.inbox, "w", encoding="utf-8").write(
            "## Cards\n\n" + CARD % ("C1", "remeasured", "C1",
                                      "%s\nthe mast is 4.20 metres tall." % link_line,
                                      "the mast is 4.20 metres tall."))
        env = dict(os.environ, KHMS_ROOT=self.root)
        return subprocess.run([sys.executable, APPROVE, self.inbox],
                              capture_output=True, text=True, env=env)

    def read(self, cid):
        return open(os.path.join(self.know, cid + ".md"), encoding="utf-8").read()

    def test_an_active_target_is_marked_and_pointed_back(self):
        self.target("K-90001")
        r = self.approve("**LINKS:** supersedes=[K-90001]")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        body = self.read("K-90001")
        self.assertIn("status: superseded", body)
        self.assertRegex(body, r"superseded_by: K-905\d\d")
        self.assertIn("K-90001: status -> superseded", r.stdout)

    def test_the_target_keeps_its_body(self):
        self.target("K-90001")
        self.approve("**LINKS:** supersedes=[K-90001]")
        self.assertIn("four metres tall", self.read("K-90001"))

    def test_a_non_active_target_is_left_alone_and_reported(self):
        # Current core rule: an `active` or `challenged` target is marked superseded (a challenged
        # card is the one most in need of a pointer to its replacement); a refuted one is left alone.
        self.target("K-90001", status="refuted")
        r = self.approve("**LINKS:** supersedes=[K-90001]")
        self.assertIn("status: refuted", self.read("K-90001"))
        self.assertIn("not active", r.stdout)

    def test_a_missing_target_is_reported_not_crashed(self):
        r = self.approve("**LINKS:** supersedes=[K-90404]")
        # Automatic view validation exposes the unresolved target, never a false fresh view.
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(os.path.exists(os.path.join(self.root, 'state/shared/views-dirty')))
        self.assertIn("no such card", r.stdout)

    def test_supports_changes_no_status(self):
        # positive control: the narrowness is the point, not an oversight
        self.target("K-90001")
        r = self.approve("**LINKS:** supports=[K-90001]")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("status: active", self.read("K-90001"))
        self.assertNotIn("superseded_by", self.read("K-90001"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
