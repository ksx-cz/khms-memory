#!/usr/bin/env python3
"""Acceptance tests for tools/verify_relations.py.

Run:  python3 tests/test_verify_relations.py
      python3 -m pytest tests/test_verify_relations.py

WHAT IS GUARDED
---------------
The nightly consolidate stage must name, for every candidate it emits, the
existing card it supersedes / supports / contradicts — or say in one sentence
why its nearest card is unrelated.  A prompt instruction is a request; this
validator is the check, and these tests are the check on the check.

Every case below is a DISCRIMINATING one: for each drop reason there is a
positive control in the same file — a candidate that differs only in the thing
under test and must survive.  A validator that drops everything would pass a
suite of drop-cases alone; it cannot pass this one.

Nothing here touches memory/know: the base is a temp directory of four cards
handed to the tool with --know / --archive.
"""
import os
import re
import subprocess
import sys
import tempfile
import unittest

TOOLS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools")
VERIFY = os.path.join(TOOLS, "verify_relations.py")


def card(cid, status="active"):
    return (f"---\nid: {cid}\ntype: fact\nlevel: observation\nstatus: {status}\n"
            f"tags: [testing]\nscope: universal\ndate: 2026-09-01\n"
            f"links:\n  supersedes: null\n---\n{cid} body text\n")


def cand(label, title, lines):
    """One `### C<n>` candidate block in the consolidate output format."""
    body = "\n".join(lines)
    return (f"### {label}: {title}\n"
            "```yaml\ntype: fact\nlevel: observation\ntags: [testing]\n"
            "scope: universal\nevidence: measured\ndate: 2026-09-03\n```\n"
            f"{body}\n\n**QUOTES:**\n- src=digest :: {title}\n")


NEAREST = "NEAREST: K-90001 (30.0), K-90002 (20.0), K-90003 (10.0)"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.know = os.path.join(self.dir, "know")
        self.arch = os.path.join(self.dir, "archive")
        os.makedirs(self.know)
        os.makedirs(self.arch)
        for cid in ("K-90001", "K-90002"):
            open(os.path.join(self.know, f"{cid}.md"), "w").write(card(cid))
        # K-90003 exists but is already superseded: a supersedes edge onto it is
        # the "chain onto a dead card" case, and it must not pass.
        open(os.path.join(self.know, "K-90003.md"), "w").write(
            card("K-90003", "superseded"))
        # K-90004 lives only in the fog (archive) — flagged, not dropped.
        open(os.path.join(self.arch, "K-90004.md"), "w").write(card("K-90004"))

    def tearDown(self):
        self.tmp.cleanup()

    def run_tool(self, text, *extra):
        src = os.path.join(self.dir, "2026-09-04.md")
        open(src, "w", encoding="utf-8").write(text)
        r = subprocess.run(
            [sys.executable, VERIFY, src, "--know", self.know,
             "--archive", self.arch, *extra],
            capture_output=True, text=True)
        return r, open(src, encoding="utf-8").read()


HEADER = "# nightly consolidate 2026-09-04\n\n## Cards\n\n"


class Relations(Base):
    """Six candidates, one per RELATION outcome, in ONE file."""

    FILE = HEADER + "\n".join([
        # --- must survive (positive controls) ---------------------------------
        cand("C1", "valid supersedes",
             [NEAREST, "RELATION: supersedes K-90001 BECAUSE the old figure was "
                       "measured on a broken rig."]),
        cand("C2", "valid supports", [NEAREST, "RELATION: supports K-90002"]),
        cand("C3", "valid contradicts", [NEAREST, "RELATION: contradicts K-90002"]),
        cand("C4", "valid new", [NEAREST, "RELATION: new — nearest K-90002 "
                                          "unrelated because it is about a "
                                          "different subsystem."]),
        # --- must be dropped ---------------------------------------------------
        cand("C5", "missing relation", [NEAREST, "DECIDED: nothing names a card."]),
        cand("C6", "supersedes a superseded card",
             [NEAREST, "RELATION: supersedes K-90003 BECAUSE it is stale."]),
        cand("C7", "unknown id",
             [NEAREST, "RELATION: supports K-99999"]),
    ])

    def setUp(self):
        super().setUp()
        self.r, self.out = self.run_tool(self.FILE)

    def dropped_section(self):
        m = re.search(r"^## DROPPED \(no valid RELATION\)\s*$(.*)\Z",
                      self.out, re.M | re.S)
        return m.group(1) if m else ""

    def kept_labels(self):
        head = self.out.split("## DROPPED (no valid RELATION)")[0]
        head = head.split("## DEFERRED")[0]
        return re.findall(r"^### (C\d+):", head, re.M)

    def test_exit_code_is_1_when_anything_dropped(self):
        self.assertEqual(self.r.returncode, 1, self.r.stdout + self.r.stderr)

    def test_valid_candidates_survive(self):
        # POSITIVE CONTROL for the whole suite: a validator that dropped
        # everything would satisfy every drop-case below and fail here.
        self.assertEqual(self.kept_labels(), ["C1", "C2", "C3", "C4"])

    def test_missing_relation_is_dropped(self):
        self.assertIn("### C5:", self.dropped_section())
        self.assertRegex(self.r.stdout, r"C5: DROP .*RELATION")

    def test_supersedes_of_superseded_card_is_dropped(self):
        self.assertIn("### C6:", self.dropped_section())
        self.assertRegex(self.r.stdout, r"C6: DROP .*(status|superseded|active)")

    def test_unknown_id_is_dropped(self):
        self.assertIn("### C7:", self.dropped_section())
        self.assertRegex(self.r.stdout, r"C7: DROP .*K-99999")

    def test_nothing_is_deleted(self):
        # "never silently deleted" — every candidate that entered the file is
        # still in it, kept or dropped.
        for label in ("C1", "C2", "C3", "C4", "C5", "C6", "C7"):
            self.assertIn(f"### {label}:", self.out)

    def test_report_counts(self):
        self.assertRegex(self.r.stdout,
                         r"7 candidates .* 4 kept .* 3 dropped")


class EdgeCases(Base):
    def test_archived_target_is_flagged_not_dropped(self):
        r, out = self.run_tool(HEADER + cand(
            "C1", "points into the fog",
            [NEAREST, "RELATION: supports K-90004"]))
        self.assertRegex(r.stdout, r"C1: FLAG .*archive")
        self.assertNotIn("### C1:", out.split(
            "## DROPPED (no valid RELATION)")[-1]
            if "## DROPPED (no valid RELATION)" in out else "")
        self.assertEqual(r.returncode, 0, r.stdout)

    def test_new_must_name_one_of_its_nearest_ids(self):
        r, out = self.run_tool(HEADER + "\n".join([
            cand("C1", "new naming a nearest id",
                 [NEAREST, "RELATION: new — nearest K-90001 unrelated because "
                           "it is about power, not perception."]),
            cand("C2", "new naming a non-nearest id",
                 [NEAREST, "RELATION: new — nearest K-90004 unrelated because "
                           "it is about power, not perception."]),
        ]))
        self.assertIn("### C2:", out.split("## DROPPED (no valid RELATION)")[-1])
        self.assertNotIn("### C1:", out.split(
            "## DROPPED (no valid RELATION)")[-1])
        self.assertEqual(r.returncode, 1)

    def test_two_relation_lines_is_a_drop(self):
        r, out = self.run_tool(HEADER + cand(
            "C1", "two relations",
            [NEAREST, "RELATION: supports K-90001",
             "RELATION: contradicts K-90002"]))
        self.assertIn("### C1:", out.split("## DROPPED (no valid RELATION)")[-1])
        self.assertEqual(r.returncode, 1)

    def test_clean_file_exits_zero_and_is_unchanged_in_substance(self):
        text = HEADER + cand("C1", "fine", [NEAREST, "RELATION: supports K-90001"])
        r, out = self.run_tool(text)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertNotIn("## DROPPED (no valid RELATION)", out)
        self.assertNotIn("## DEFERRED", out)


class Cap(Base):
    """C2: the cap holds regardless of model compliance."""

    def build(self, n):
        return HEADER + "\n".join(
            cand(f"C{i}", f"candidate {i}",
                 [NEAREST, "RELATION: supports K-90001"])
            for i in range(1, n + 1))

    def test_25_candidates_max_20_gives_20_kept_5_deferred_in_rank_order(self):
        r, out = self.run_tool(self.build(25), "--max", "20")
        head, _, tail = out.partition("## DEFERRED")
        kept = re.findall(r"^### (C\d+):", head, re.M)
        deferred = re.findall(r"^### (C\d+):", tail, re.M)
        self.assertEqual(len(kept), 20, kept)
        self.assertEqual(deferred, [f"C{i}" for i in range(21, 26)])
        self.assertRegex(r.stdout, r"20 kept")
        self.assertRegex(r.stdout, r"5 deferred")

    def test_deferred_candidates_are_not_deleted(self):
        _, out = self.run_tool(self.build(25), "--max", "20")
        for i in range(1, 26):
            self.assertIn(f"### C{i}:", out)

    def test_under_the_cap_nothing_is_deferred(self):
        r, out = self.run_tool(self.build(5), "--max", "20")
        self.assertNotIn("## DEFERRED", out)
        self.assertEqual(r.returncode, 0, r.stdout)

    def test_cap_counts_only_kept_candidates(self):
        # one invalid candidate first: it is dropped, so the cap must still let
        # 20 VALID ones through, not 19.
        text = HEADER + cand("C0", "no relation", ["DECIDED: x"]) + \
            "\n".join(cand(f"C{i}", f"c{i}",
                           [NEAREST, "RELATION: supports K-90001"])
                      for i in range(1, 26))
        _, out = self.run_tool(text, "--max", "20")
        head = out.split("## DEFERRED")[0].split(
            "## DROPPED (no valid RELATION)")[0]
        self.assertEqual(len(re.findall(r"^### C\d+:", head, re.M)), 20)


class SupersedesTarget(Base):
    """G2: which target statuses a `supersedes` edge may point at.

    A `challenged` card is the one MOST in need of superseding — `challenged`
    is the status the triage stage writes onto the loser of a contradiction,
    i.e. onto exactly the card a better-measured candidate should replace.
    Dropping such a candidate leaves the challenged card standing AND throws
    away its replacement, which is the opposite of the merge pressure this
    validator exists to create.  `superseded` / `refuted` / `condensed`
    targets stay dropped: an edge onto them chains onto a dead end.
    """

    STATUSES = {"K-90011": "challenged", "K-90012": "superseded",
                "K-90013": "refuted", "K-90014": "condensed"}

    def setUp(self):
        super().setUp()
        for cid, status in self.STATUSES.items():
            open(os.path.join(self.know, f"{cid}.md"), "w").write(
                card(cid, status))

    def one(self, cid):
        near = f"NEAREST: {cid} (30.0), K-90001 (10.0)"
        return self.run_tool(HEADER + cand(
            "C1", f"supersedes a {self.STATUSES[cid]} card",
            [near, f"RELATION: supersedes {cid} BECAUSE it was measured on a "
                   f"rig that has since been fixed."]))

    def dropped(self, out):
        return out.split("## DROPPED (no valid RELATION)")[-1] \
            if "## DROPPED (no valid RELATION)" in out else ""

    def test_challenged_target_is_kept(self):
        r, out = self.one("K-90011")
        self.assertNotIn("### C1:", self.dropped(out),
                         "challenged target was dropped:\n" + r.stdout)
        self.assertRegex(r.stdout, r"C1: OK supersedes K-90011")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_superseded_target_is_still_dropped(self):
        r, out = self.one("K-90012")
        self.assertIn("### C1:", self.dropped(out))
        self.assertRegex(r.stdout, r"C1: DROP .*superseded")
        self.assertEqual(r.returncode, 1)

    def test_refuted_target_is_still_dropped(self):
        r, out = self.one("K-90013")
        self.assertIn("### C1:", self.dropped(out))
        self.assertRegex(r.stdout, r"C1: DROP .*refuted")
        self.assertEqual(r.returncode, 1)

    def test_condensed_target_is_still_dropped(self):
        r, out = self.one("K-90014")
        self.assertIn("### C1:", self.dropped(out))
        self.assertRegex(r.stdout, r"C1: DROP .*condensed")
        self.assertEqual(r.returncode, 1)

    def test_archived_challenged_target_is_still_dropped(self):
        # POSITIVE CONTROL for the archive rule: widening the STATUS set must
        # not widen the WHERE set.  A challenged card in the fog stays dropped.
        open(os.path.join(self.arch, "K-90015.md"), "w").write(
            card("K-90015", "challenged"))
        near = "NEAREST: K-90015 (30.0), K-90001 (10.0)"
        r, out = self.run_tool(HEADER + cand(
            "C1", "supersedes a challenged card in the fog",
            [near, "RELATION: supersedes K-90015 BECAUSE it was mismeasured."]))
        self.assertIn("### C1:", self.dropped(out))
        self.assertRegex(r.stdout, r"C1: DROP .*archive")
        self.assertEqual(r.returncode, 1)

    def test_supports_a_challenged_card_was_always_allowed(self):
        # Discriminator: `supports`/`contradicts` never had the status rule, so
        # this case must be green BEFORE and AFTER the change.  If it ever goes
        # red, the edit leaked out of the supersedes branch.
        near = "NEAREST: K-90011 (30.0), K-90001 (10.0)"
        r, out = self.run_tool(HEADER + cand(
            "C1", "supports a challenged card",
            [near, "RELATION: supports K-90011"]))
        self.assertNotIn("### C1:", self.dropped(out))
        self.assertEqual(r.returncode, 0, r.stdout)


class Idempotence(Base):
    def test_second_run_over_its_own_output_changes_nothing(self):
        text = HEADER + "\n".join([
            cand("C1", "ok", [NEAREST, "RELATION: supports K-90001"]),
            cand("C2", "no relation", ["DECIDED: x"]),
        ])
        src = os.path.join(self.dir, "2026-09-04.md")
        open(src, "w", encoding="utf-8").write(text)
        argv = [sys.executable, VERIFY, src, "--know", self.know,
                "--archive", self.arch]
        subprocess.run(argv, capture_output=True, text=True)
        first = open(src, encoding="utf-8").read()
        subprocess.run(argv, capture_output=True, text=True)
        self.assertEqual(first, open(src, encoding="utf-8").read())


if __name__ == "__main__":
    unittest.main(verbosity=2)
