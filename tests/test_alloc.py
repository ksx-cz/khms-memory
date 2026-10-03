"""One locked id reservation primitive for the whole base, used from two code locations at once.

Two copies of the code (a second clone) share ONE data root. Card ids are reserved concurrently through
new_card.sh, khms_alloc.py and approve_inbox.py from both copies, and tombstone ids through
khms_triage.next_ts_id. Every id must be unique and the counter must end above all of them.
"""
import concurrent.futures as cf
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

TOOLS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools")

INBOX = """# inbox
{cards}
"""
CARD = """
---
id: C{n}
type: fact
level: observation
status: active
tags: [testing]
scope: universal
evidence: measured
source: journal/2026-03-04.md line 1
date: 2026-03-04
---
Synthetic card {tag} number {n}: the pump stops when the tank is empty.
"""


def env(root, home, **kw):
    e = {"PATH": os.environ["PATH"], "HOME": home, "KHMS_ROOT": root, "KHMS_NO_SNAPSHOT": "1",
         "PYTHONDONTWRITEBYTECODE": "1"}
    e.update(kw)
    return e


class Allocation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="khms-alloc-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = os.path.join(self.tmp, "base")
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        self.clone_b = os.path.join(self.tmp, "second clone", "tools")
        shutil.copytree(TOOLS, self.clone_b, ignore=shutil.ignore_patterns("__pycache__"))
        subprocess.run([os.path.join(TOOLS, "khms_init.sh"), self.root], check=True,
                       capture_output=True, env=env(self.root, self.home))

    def run_cmd(self, cmd, **kw):
        return subprocess.run(cmd, capture_output=True, text=True, env=env(self.root, self.home, **kw))

    def counter(self):
        return int(open(os.path.join(self.root, "state", "shared", "next_id")).read())

    def test_concurrent_allocations_from_two_clones_are_unique(self):
        jobs = []
        for i in range(12):
            code = TOOLS if i % 2 == 0 else self.clone_b
            if i % 3 == 0:
                jobs.append(("ids", [os.path.join(code, "new_card.sh"), "3"]))
            elif i % 3 == 1:
                jobs.append(("ids", [sys.executable, os.path.join(code, "khms_alloc.py"), "card", "2"]))
            else:
                f = os.path.join(self.tmp, f"inbox-{i}.md")
                with open(f, "w", encoding="utf-8") as fh:
                    fh.write(INBOX.format(cards="".join(CARD.format(n=n, tag=i) for n in (1, 2))))
                jobs.append(("approve", [sys.executable, os.path.join(code, "approve_inbox.py"), f]))
        with cf.ThreadPoolExecutor(max_workers=12) as ex:
            results = list(ex.map(lambda j: (j[0], self.run_cmd(j[1])), jobs))
        ids = []
        for kind, r in results:
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            if kind == "ids":
                ids += re.findall(r"^K-(\d{5})$", r.stdout, re.M)
            else:
                ids += re.findall(r"-> K-(\d{5})", r.stdout)
        expected = 4 * 3 + 4 * 2 + 4 * 2
        self.assertEqual(len(ids), expected)
        self.assertEqual(len(set(ids)), expected, f"duplicate ids: {sorted(ids)}")
        self.assertGreater(self.counter(), max(int(i) for i in ids))
        written = os.listdir(os.path.join(self.root, "memory", "know"))
        self.assertEqual(len(written), 8)

    def test_tombstone_ids_use_the_same_primitive(self):
        code = ("import sys; sys.path.insert(0, sys.argv[1]); import khms_triage as T; "
                "print(T.next_ts_id(ledger=[]))")
        with cf.ThreadPoolExecutor(max_workers=8) as ex:
            rs = list(ex.map(lambda i: self.run_cmd(
                [sys.executable, "-c", code, TOOLS if i % 2 else self.clone_b]), range(8)))
        got = [r.stdout.strip() for r in rs]
        self.assertTrue(all(re.fullmatch(r"TS-\d{5}", g) for g in got), [r.stderr for r in rs])
        self.assertEqual(len(set(got)), 8, got)

    def test_counter_never_goes_below_an_existing_card(self):
        know = os.path.join(self.root, "memory", "know")
        open(os.path.join(know, "K-90050.md"), "w").write("---\nid: K-90050\n---\nx\n")
        r = self.run_cmd([os.path.join(TOOLS, "new_card.sh")])
        self.assertEqual(r.stdout.strip(), "K-90051")
        self.assertEqual(self.counter(), 90052)

    def test_a_corrupt_counter_is_refused_not_guessed(self):
        path = os.path.join(self.root, "state", "shared", "next_id")
        open(path, "w").write("banana\n")
        r = self.run_cmd([os.path.join(TOOLS, "new_card.sh")])
        self.assertEqual(r.returncode, 3)
        self.assertEqual(open(path).read(), "banana\n")

    def test_read_only_refuses_before_any_write(self):
        before = self.counter()
        r = self.run_cmd([os.path.join(TOOLS, "new_card.sh")], KHMS_READ_ONLY="1")
        self.assertEqual(r.returncode, 4, r.stderr)
        self.assertIn("read-only", r.stderr)
        self.assertEqual(self.counter(), before)
        self.assertFalse(os.path.exists(os.path.join(self.root, "state", "shared", "next_id.tmp")))


if __name__ == "__main__":
    unittest.main()
