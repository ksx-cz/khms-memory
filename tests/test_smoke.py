"""Fresh-home smoke: a clean HOME, a temporary base, no embedding service, no private files.

init -> idempotent re-init -> explicit registration (and refusal to replace it) -> fixture approval ->
views -> recall of a known card through the REGISTERED root (no KHMS_ROOT) -> lint -> hook SessionStart.
An empty base is supported too. Missing optional features are reported as such, not as failures.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.join(REPO, "tools")
FIXTURE = os.path.join(REPO, "tests", "fixtures", "smoke-inbox.md")


class FreshHome(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="khms smoke ")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        self.root = os.path.join(self.tmp, "my base")
        os.makedirs(self.home)

    def run_(self, cmd, inp=None, **kw):
        e = {"PATH": os.environ["PATH"], "HOME": self.home, "KHMS_NO_SNAPSHOT": "1",
             "PYTHONDONTWRITEBYTECODE": "1"}
        e.update(kw)
        return subprocess.run(cmd, capture_output=True, text=True, env=e, input=inp, cwd=self.tmp)

    def ok(self, cmd, inp=None, **kw):
        r = self.run_(cmd, inp, **kw)
        self.assertEqual(r.returncode, 0, f"{cmd}\n{r.stdout}\n{r.stderr}")
        return r

    def test_the_whole_path(self):
        self.ok([os.path.join(TOOLS, "khms_init.sh"), "--register", self.root])
        counter = os.path.join(self.root, "state", "shared", "next_id")
        self.assertEqual(open(counter).read().strip(), "1")
        # idempotent: nothing replaced, counter and settings preserved
        with open(os.path.join(self.root, "config", "khms.env"), "a") as fh:
            fh.write("KHMS_PRINCIPAL=operator\n")
        open(counter, "w").write("90007\n")
        r = self.ok([os.path.join(TOOLS, "khms_init.sh"), "--register", self.root])
        self.assertIn("nothing created", r.stdout)
        self.assertEqual(open(counter).read(), "90007\n")
        self.assertIn("KHMS_PRINCIPAL=operator", open(os.path.join(self.root, "config", "khms.env")).read())
        # a different registration is refused, never replaced
        other = os.path.join(self.tmp, "other base")
        r = self.run_([os.path.join(TOOLS, "khms_init.sh"), "--register", other])
        self.assertEqual(r.returncode, 3)
        self.assertIn("refusing to replace", r.stderr)
        self.assertEqual(open(os.path.join(self.home, ".config", "khms", "root")).read().strip(), self.root)
        # no code was copied into the base
        self.assertFalse(os.path.exists(os.path.join(self.root, "tools")))
        # cards through the supported path, then views, recall, lint, hook -- all via the registration
        inbox = os.path.join(self.root, "memory", "inbox", "2026-03-05.md")
        shutil.copy(FIXTURE, inbox)
        r = self.ok([sys.executable, os.path.join(TOOLS, "approve_inbox.py"), inbox])
        self.assertIn("C1 -> K-90007", r.stdout)
        self.assertIn("not installed (optional", r.stdout)
        r = self.ok([sys.executable, os.path.join(TOOLS, "build_views.py")])
        self.assertIn("dense index: not configured (optional", r.stdout)
        r = self.ok([os.path.join(TOOLS, "recall.sh"), "anemometer", "bearing"])
        self.assertIn("K-90007", r.stdout)
        self.assertIn("channel=lexical(dense not configured", r.stdout)
        self.ok([sys.executable, os.path.join(TOOLS, "khms_lint.py"), os.path.join(self.root, "memory", "know")])
        r = self.ok([sys.executable, os.path.join(TOOLS, "khms_hook.py")],
                    inp=json.dumps({"hook_event_name": "SessionStart", "session_id": "smoke"}))
        ctx = json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertNotIn("UNREVIEWED INBOX", ctx)       # approve recorded the review of that unit

    def test_an_empty_base_is_supported(self):
        self.ok([os.path.join(TOOLS, "khms_init.sh"), self.root], KHMS_ROOT="")
        r = self.ok([sys.executable, os.path.join(TOOLS, "build_views.py")], KHMS_ROOT=self.root)
        self.assertIn("OK: 0 cards", r.stdout)
        r = self.run_([os.path.join(TOOLS, "recall.sh"), "anything"], KHMS_ROOT=self.root)
        self.assertEqual(r.returncode, 1)
        self.assertIn("nothing on record", r.stdout)

    def test_init_refuses_a_base_inside_the_checkout(self):
        r = self.run_([os.path.join(TOOLS, "khms_init.sh"), os.path.join(REPO, "base")])
        self.assertEqual(r.returncode, 3)
        self.assertFalse(os.path.exists(os.path.join(REPO, "base")))

    def test_without_a_base_every_tool_says_how_to_make_one(self):
        for cmd in ([os.path.join(TOOLS, "recall.sh"), "x"], [os.path.join(TOOLS, "new_card.sh")],
                    [sys.executable, os.path.join(TOOLS, "build_views.py")]):
            r = self.run_(cmd)
            self.assertEqual(r.returncode, 3, cmd)
            self.assertIn("khms_init.sh", r.stderr)
            self.assertNotIn("Traceback", r.stderr)


if __name__ == "__main__":
    unittest.main()
