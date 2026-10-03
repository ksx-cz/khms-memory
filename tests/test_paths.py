"""The resolver pair: khms_paths.py and khms_paths.sh must give the same answer to the same question.

Every case runs BOTH readers as separate processes with a scrubbed environment, a temporary HOME and a
working directory unrelated to the checkout, and compares exit code and output. Paths contain spaces on
purpose. Error cases compare the exit code and the error class word, not the full message.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

TOOLS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools")
PY = os.path.join(TOOLS, "khms_paths.py")
SH = os.path.join(TOOLS, "khms_paths.sh")


class Readers(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="khms paths ")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home dir")
        self.root = os.path.join(self.tmp, "the base")
        self.cwd = os.path.join(self.tmp, "elsewhere")
        for d in (self.home, self.root, self.cwd):
            os.makedirs(d)

    def env(self, **kw):
        e = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": self.home}
        e.update({k: v for k, v in kw.items() if v is not None})
        return e

    def both(self, args, **env):
        out = []
        for cmd in ([sys.executable, PY, *args], ["bash", SH, *args]):
            r = subprocess.run(cmd, capture_output=True, text=True, env=self.env(**env), cwd=self.cwd)
            out.append((r.returncode, r.stdout, r.stderr))
        return out

    def agree(self, args, **env):
        (rp, op, ep), (rs, os_, es) = self.both(args, **env)
        self.assertEqual((rp, op), (rs, os_), f"python: {ep!r}\nshell: {es!r}")
        return rp, op, ep

    def fail_same(self, args, word, **env):
        (rp, op, ep), (rs, os_, es) = self.both(args, **env)
        self.assertEqual(rp, 3, ep)
        self.assertEqual(rs, 3, es)
        self.assertIn(word, ep)
        self.assertIn(word, es)

    def write(self, path, text):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    # ---------------------------------------------------------------- discovery and precedence
    def test_env_root_and_every_default_agree(self):
        rc, out, _ = self.agree(["--all"], KHMS_ROOT=self.root)
        self.assertEqual(rc, 0)
        self.assertIn(f"KHMS_STATE={self.root}/state\n", out)
        self.assertIn(f"KHMS_SHARED_STATE={self.root}/state/shared\n", out)
        self.assertIn("KHMS_GLOSSARY=\n", out)

    def test_explicit_config_names_the_root_and_overrides(self):
        cfg = self.write(os.path.join(self.tmp, "agent cfg", "khms.env"),
                         f'# a comment\n\nKHMS_ROOT="{self.root}"\nKHMS_STATE=${{HOME}}/state x\n'
                         "KHMS_READ_ONLY='1'\nKHMS_RECALL_LOG=${KHMS_STATE}/r.log\n")
        rc, out, _ = self.agree(["--all"], KHMS_CONFIG_FILE=cfg)
        self.assertEqual(rc, 0)
        self.assertIn(f"KHMS_ROOT={self.root}\n", out)
        self.assertIn(f"KHMS_STATE={self.home}/state x\n", out)
        self.assertIn(f"KHMS_RECALL_LOG={self.home}/state x/r.log\n", out)
        self.assertIn("KHMS_READ_ONLY=1\n", out)

    def test_environment_beats_the_file(self):
        cfg = self.write(os.path.join(self.root, "config", "khms.env"), "KHMS_STATE=/from/file\n")
        rc, out, _ = self.agree(["KHMS_STATE"], KHMS_ROOT=self.root, KHMS_STATE="/from/env")
        self.assertEqual(out, "/from/env\n")
        rc, out, _ = self.agree(["KHMS_STATE"], KHMS_ROOT=self.root)
        self.assertEqual(out, "/from/file\n")
        self.assertTrue(os.path.exists(cfg))

    def test_registered_root(self):
        self.write(os.path.join(self.home, ".config", "khms", "root"), self.root + "\n")
        rc, out, _ = self.agree(["--root"])
        self.assertEqual((rc, out), (0, self.root + "\n"))

    def test_legacy_recall_log_override_still_moves_the_log(self):
        rc, out, _ = self.agree(["KHMS_RECALL_LOG"], KHMS_ROOT=self.root, KHMS_RECALL_LOG="/x/y.log")
        self.assertEqual(out, "/x/y.log\n")

    def test_an_empty_value_means_the_default(self):
        self.write(os.path.join(self.root, "config", "khms.env"), "KHMS_STATE=\n")
        rc, out, _ = self.agree(["KHMS_STATE"], KHMS_ROOT=self.root)
        self.assertEqual(out, f"{self.root}/state\n")

    # ---------------------------------------------------------------- refusals
    def test_no_root_fails_and_names_init(self):
        self.fail_same(["--root"], "khms_init.sh")

    def test_no_checkout_fallback(self):
        # Running from inside the code tree must not invent a root there.
        for cmd in ([sys.executable, PY, "--root"], ["bash", SH, "--root"]):
            r = subprocess.run(cmd, capture_output=True, text=True, env=self.env(), cwd=TOOLS)
            self.assertEqual(r.returncode, 3, r.stdout)

    def test_missing_explicit_config(self):
        self.fail_same(["--root"], "does not exist", KHMS_CONFIG_FILE=os.path.join(self.tmp, "nope.env"))

    def test_reference_cycle(self):
        self.write(os.path.join(self.root, "config", "khms.env"),
                   "KHMS_STATE=${KHMS_GLOSSARY}/s\nKHMS_GLOSSARY=${KHMS_STATE}/g\n")
        self.fail_same(["KHMS_STATE"], "refers to itself", KHMS_ROOT=self.root)

    def test_root_through_itself_is_a_cycle(self):
        cfg = self.write(os.path.join(self.tmp, "c.env"), "KHMS_ROOT=${KHMS_STATE}/x\n")
        self.fail_same(["--root"], "refers to itself", KHMS_CONFIG_FILE=cfg)

    def test_unresolved_reference(self):
        self.write(os.path.join(self.root, "config", "khms.env"), "KHMS_STATE=${NO_SUCH_THING}/s\n")
        self.fail_same(["KHMS_STATE"], "nothing defines", KHMS_ROOT=self.root)

    def test_malformed_line(self):
        self.write(os.path.join(self.root, "config", "khms.env"), "export KHMS_STATE=/x\n")
        self.fail_same(["KHMS_STATE"], "malformed", KHMS_ROOT=self.root)

    def test_conflicting_root_in_the_default_file(self):
        self.write(os.path.join(self.root, "config", "khms.env"), "KHMS_ROOT=/somewhere/else\n")
        self.fail_same(["--root"], "discovered root", KHMS_ROOT=self.root)

    def test_bad_read_only_value(self):
        self.fail_same(["--root"], "KHMS_READ_ONLY", KHMS_ROOT=self.root, KHMS_READ_ONLY="yes")

    def test_relative_root(self):
        self.fail_same(["--root"], "absolute", KHMS_ROOT="relative/base")

    def test_the_file_is_never_executed(self):
        marker = os.path.join(self.tmp, "pwned")
        self.write(os.path.join(self.root, "config", "khms.env"),
                   f"KHMS_STATE=$(touch '{marker}')\n")
        rc, out, _ = self.agree(["KHMS_STATE"], KHMS_ROOT=self.root)
        self.assertEqual(out, f"$(touch '{marker}')\n")
        self.assertFalse(os.path.exists(marker))


class SourcedShell(unittest.TestCase):
    def test_functions_work_when_sourced_with_spaces(self):
        tmp = tempfile.mkdtemp(prefix="khms src ")
        self.addCleanup(shutil.rmtree, tmp, True)
        root = os.path.join(tmp, "b a s e")
        script = f'. "{SH}"; khms_load || exit $?; khms_get KHMS_SHARED_STATE; ' \
                 'khms_require_writable "test"; echo rc=$?'
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                           env={"PATH": os.environ["PATH"], "HOME": tmp, "KHMS_ROOT": root,
                                "KHMS_READ_ONLY": "1"})
        self.assertIn(f"{root}/state/shared", r.stdout)
        self.assertIn("rc=4", r.stdout)
        self.assertIn("read-only", r.stderr)


if __name__ == "__main__":
    unittest.main()
