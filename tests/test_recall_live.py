"""#247 — recall returns no dead card (owner 2026-10-20: "Recall prece nema vracet karty co uz neplati ne?").

A superseded or refuted card is not returned by default; its live successor takes its place in the ranking
(transitively, once), nothing does when it has none. `challenged` cards stay, flagged. `--history` / `--all`
brings the dead cards back, flagged. The same rule holds on every path that ranks cards: recall.sh (lexical
and hybrid fusion) and the hook's selection (lexical, dense rescue, correctors).
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.join(REPO, "tools")
sys.path.insert(0, TOOLS)


def card(cid, status, body, supersedes=None, refuted_by=(), superseded_by=None, tags=("sensors",)):
    sup = f"  supersedes: {supersedes}\n" if supersedes else "  supersedes: null\n"
    back = f"  superseded_by: {superseded_by}\n" if superseded_by else ""
    return (f"---\nid: {cid}\ntype: fact\nlevel: observation\nstatus: {status}\n"
            f"tags: [{', '.join(tags)}]\nscope: device:weather-station\nevidence: measured\n"
            f"source: 'journal/2026-03-04.md line 1'\ndate: 2026-03-04\nlinks:\n"
            f"  derived_from: []\n  supports: []\n  contradicts: []\n{sup}"
            f"  refuted_by: [{', '.join(refuted_by)}]\n{back}---\n{body}\n")


CARDS = {
    # A superseded card that matches the query better than its replacement
    "K-90001": card("K-90001", "superseded",
                    "anemometer bearing offset is 0.4 m/s — anemometer bearing calibration\nold value",
                    superseded_by="K-90002"),
    "K-90002": card("K-90002", "active", "anemometer offset is 0.1 m/s since the new bearing\nnew value",
                    supersedes="K-90001"),
    # a refuted card with no replacement, and a challenged one
    "K-90003": card("K-90003", "refuted", "rain gauge siphon empties at 5 mm — rain gauge siphon\nwrong"),
    "K-90004": card("K-90004", "challenged", "rain gauge tipping bucket counts double in wind\ndoubted"),
    # a chain A -> B -> C, C active and not matching the query at all
    "K-90005": card("K-90005", "superseded", "humidity sensor drift is 3 % per year — humidity sensor drift\nA"),
    "K-90006": card("K-90006", "superseded", "humidity sensor drift is 2 %\nB", supersedes="K-90005"),
    "K-90007": card("K-90007", "active", "the hygrometer ages by one percent a year\nC", supersedes="K-90006"),
}
DEAD = ("K-90001", "K-90003", "K-90005", "K-90006")


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="khms-recall-live-")
        cls.home = os.path.join(cls.tmp, "home")
        cls.root = os.path.join(cls.tmp, "base")
        os.makedirs(cls.home)
        r = subprocess.run([os.path.join(TOOLS, "khms_init.sh"), cls.root], capture_output=True, text=True,
                           env=cls.env())
        assert r.returncode == 0, r.stderr
        know = os.path.join(cls.root, "memory", "know")
        for cid, text in CARDS.items():
            with open(os.path.join(know, cid + ".md"), "w", encoding="utf-8") as fh:
                fh.write(text)
        # the in-process tests resolve this base (glossary, state) like the CLI does
        import khms_paths as P
        cls._old_root = os.environ.get("KHMS_ROOT")
        os.environ["KHMS_ROOT"] = cls.root
        P.reset()

    @classmethod
    def tearDownClass(cls):
        import khms_paths as P
        if cls._old_root is None:
            os.environ.pop("KHMS_ROOT", None)
        else:
            os.environ["KHMS_ROOT"] = cls._old_root
        P.reset()
        shutil.rmtree(cls.tmp, True)

    @classmethod
    def env(cls, **kw):
        e = {"PATH": os.environ["PATH"], "HOME": cls.home, "PYTHONDONTWRITEBYTECODE": "1",
             "KHMS_ROOT": cls.root}
        e.update(kw)
        return e

    def recall(self, *args):
        r = subprocess.run([os.path.join(TOOLS, "recall.sh"), *args], capture_output=True, text=True,
                           env=self.env(), cwd=self.tmp)
        self.assertIn(r.returncode, (0, 1), r.stderr)
        return r.stdout

    @staticmethod
    def ids(out):
        import re
        return [m.group(1) for m in re.finditer(r"^(K-\d{5})\b", out, re.M)]

    def cards(self):
        import khms_search as ks
        old = ks.MEM
        ks.MEM = os.path.join(self.root, "memory")
        try:
            return ks.load_cards()
        finally:
            ks.MEM = old


class RecallCli(Base):
    def test_superseded_gives_way_to_its_replacement(self):
        out = self.recall("anemometer", "bearing", "calibration")
        self.assertEqual(self.ids(out)[:1], ["K-90002"], out)
        self.assertNotIn("K-90001", out)          # neither as a hit nor as a link line

    def test_history_shows_both_flagged(self):
        out = self.recall("--history", "anemometer", "bearing", "calibration")
        ids = self.ids(out)
        self.assertIn("K-90001", ids, out)
        self.assertIn("K-90002", ids, out)
        self.assertIn("K-90001 [superseded]", out)
        self.assertIn("(history:", out)
        self.assertEqual(self.ids(self.recall("--all", "anemometer", "bearing", "calibration")), ids)

    def test_refuted_without_replacement_is_absent_challenged_stays_flagged(self):
        out = self.recall("rain", "gauge", "siphon")
        self.assertNotIn("K-90003", out)
        self.assertIn("K-90004 [⛔CHALLENGED]", out)
        self.assertIn("K-90003 [⛔REFUTED]", self.recall("--history", "rain", "gauge", "siphon"))

    def test_chain_reaches_the_live_end(self):
        out = self.recall("humidity", "sensor", "drift")
        self.assertEqual(self.ids(out), ["K-90007"], out)
        hist = self.ids(self.recall("--history", "humidity", "sensor", "drift"))
        self.assertEqual(hist[:2], ["K-90005", "K-90006"])
        self.assertNotIn("K-90007", hist)         # history is the ranking as it was: C matches nothing


class SharedRule(Base):
    def test_lexical_search_once_without_duplicates(self):
        import khms_search as ks
        cards = self.cards()
        res, _ = ks.search("anemometer bearing calibration offset", cards=cards)
        ids = [c["id"] for _s, _m, c in res]
        self.assertEqual(ids, ["K-90002"])
        res, _ = ks.search("anemometer bearing calibration offset", cards=cards, history=True)
        self.assertEqual([c["id"] for _s, _m, c in res], ["K-90001", "K-90002"])

    def test_hybrid_fusion_maps_the_dense_channel_too(self):
        import khms_recall_hybrid as hy
        cards = self.cards()
        orig = hy.dense_query
        hy.dense_query = lambda *a, **k: ([("K-90005", 0.9), ("K-90003", 0.8), ("K-90004", 0.7)], None, 1.0)
        try:
            res, meta = hy.hybrid_rank("humidity sensor drift", cards=cards)
            hist, _ = hy.hybrid_rank("humidity sensor drift", cards=cards, history=True)
        finally:
            hy.dense_query = orig
        self.assertEqual(meta["mode"], "hybrid")
        ids = [c["id"] for _s, _m, c in res]
        self.assertEqual(ids, ["K-90007", "K-90004"])
        self.assertFalse(set(ids) & set(DEAD))
        self.assertTrue({"K-90005", "K-90006", "K-90003"} <= {c["id"] for _s, _m, c in hist})

    def test_hook_selection_never_serves_a_dead_card(self):
        import khms_search as ks
        import khms_select as sel
        cards = sel.prepare_cards(self.cards(), cache_path=None)
        p = sel.Params(name="t", threshold={"UserPromptSubmit": 1.0, "PreToolUse": 1.0},
                       dense_events=frozenset({"UserPromptSubmit"}), dense_min_cos=0.1,
                       rescue_min_first_len=5, second_card_min=0.5)
        st = {"injected": {}, "recent": [], "queries": {}, "bypass": []}
        d = sel.decide("UserPromptSubmit", "anemometer bearing calibration and humidity sensor drift",
                       st, 1000.0, cards, p, job=False,
                       dense_fn=lambda q, n: ([("K-90003", 0.9), ("K-90006", 0.8)], None, 1.0))
        self.assertTrue(d.used, d)
        self.assertFalse(set(d.used) & set(DEAD), d.used)
        for cid in DEAD:
            self.assertNotIn(cid, d.text or "")
        # a dead corrector is no corrector: K-90005 is superseded only by the dead K-90006
        by_id = {c["id"]: c for c in cards}
        rev = ks.reverse_correctors(cards)
        self.assertEqual(sel.correctors_of(by_id["K-90006"], by_id, rev), [by_id["K-90007"]])
        self.assertEqual(sel.correctors_of(by_id["K-90005"], by_id, rev), [])


if __name__ == "__main__":
    unittest.main()
