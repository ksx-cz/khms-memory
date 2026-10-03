"""The boundary scanner: the shipped tree is clean, and every rule fires on a synthetic positive control.

The controls are generated at test time in a temporary directory -- never tracked -- so they cannot trip
the repository scan themselves; strings that would look like a credential are assembled at run time.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCAN = os.path.join(REPO, "tools", "khms_boundary.py")


def run(*args, cwd=None):
    return subprocess.run([sys.executable, SCAN, *args], capture_output=True, text=True, cwd=cwd)


class Boundary(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="khms-boundary-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def write(self, name, text):
        p = os.path.join(self.tmp, name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(text)
        return p

    def test_the_shipped_tree_is_clean(self):
        r = run("--repo", REPO)
        self.assertEqual(r.returncode, 0, r.stdout)

    def test_each_generic_rule_fires_on_its_control(self):
        controls = {
            "home-path": "LOG = '/home/" + "alice/.base/log'\n",
            "secret": "token = '" + "ghp_" + "A" * 36 + "'\n",
            "project-import": "import " + "board_gh\n",
            "non-english": "MSG = 'prázdná báze'\n",
        }
        for cls, text in controls.items():
            with self.subTest(cls=cls):
                r = run("--scan-files", self.write(f"{cls}.py", text))
                self.assertEqual(r.returncode, 1, r.stdout)
                self.assertIn(cls, r.stdout)

    def test_diacritics_as_declared_data_are_allowed(self):
        p = self.write("data.py", "# boundary: allow-diacritics\nWORD = 'báze'\n")
        self.assertEqual(run("--scan-files", p).returncode, 0)

    def test_terms_mode_reports_class_never_value_and_needs_its_control(self):
        terms = self.write("terms.txt", "agent\tliteral\tZorblax\nhost\tregex\tsecret-host-\\d+\n"
                                        "person\tliteral\tQuimby Fennel\n")
        corpus = os.path.join(self.tmp, "corpus")
        self.write("corpus/a.txt", "Zorblax talked to secret-host-7 about Quimby Fennel\n")
        repo = os.path.join(self.tmp, "repo")
        os.makedirs(repo)
        git = lambda *a: subprocess.run(["git", "-C", repo, *a], check=True, capture_output=True,
                                        env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x",
                                             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x"})
        git("init", "-q", "-b", "master")
        with open(os.path.join(repo, "a.py"), "w") as fh:
            fh.write("X = 1\n")
        git("add", "a.py")
        git("commit", "-qm", "base")
        with open(os.path.join(repo, "a.py"), "w") as fh:
            fh.write("WHO = 'Zorblax'\n")
        git("commit", "-qam", "leak")
        with open(os.path.join(repo, "a.py"), "w") as fh:
            fh.write("WHO = 'someone'\n")
        git("commit", "-qam", "clean tip")
        # clean tip: the tree scan alone passes ...
        r = run("--repo", repo, "--terms", terms, "--control", corpus)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("positive control: 3 term class(es)", r.stdout)
        # ... but the outgoing history still carries the leak, reported by class only
        r = run("--repo", repo, "--terms", terms, "--control", corpus, "--history", "master~2..master")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn(": agent", r.stdout)
        self.assertNotIn("Zorblax", r.stdout)
        # a control corpus that lights fewer than three classes refuses to certify anything
        self.write("thin/a.txt", "only Zorblax here\n")
        r = run("--repo", repo, "--terms", terms, "--control", os.path.join(self.tmp, "thin"))
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn("GATE BROKEN", r.stdout)


if __name__ == "__main__":
    unittest.main()
