"""A reader deployment (KHMS_READ_ONLY=1) cannot change the shared base -- proven on the filesystem.

The base (data, config and shared state, which lives inside it here) is frozen: made unwritable to this
process AND fingerprinted (path, size, mtime of every file) before and after. The reader has its own
writable KHMS_STATE outside the base. Recall runs over every channel state -- lexical, dense with a fresh
index, dense with a stale index, dense unavailable, missing agent state -- and every mutating command is
run; each mutator must refuse with its documented code, not crash. A positive control proves that the
fingerprint notices a write at all, and the writer's and the reader's logs stay separate.
"""
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from conftest import fingerprint  # noqa: E402

TOOLS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools")
CARDS = """# inbox
---
id: C1
type: fact
level: observation
status: active
tags: [testing]
scope: universal
evidence: measured
source: journal/2026-03-04.md line 1
date: 2026-03-04
---
The greenhouse heater relay chatters when the supply voltage sags.

---
id: C2
type: fact
level: observation
status: active
tags: [testing, gotcha]
scope: universal
evidence: measured
source: journal/2026-03-04.md line 2
date: 2026-03-04
---
The rain sensor reports dry for ten minutes after a storm begins.
"""


class FakeDaemon(threading.Thread):
    """The embedding daemon's wire protocol, answering every query with fixed hits."""

    def __init__(self, path, hits):
        super().__init__(daemon=True)
        self.path, self.hits = path, hits
        self.srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.srv.bind(path)
        self.srv.listen(8)

    def run(self):
        while True:
            try:
                c, _ = self.srv.accept()
            except OSError:
                return
            with c:
                buf = b""
                while b"\n" not in buf:
                    chunk = c.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
                c.sendall((json.dumps({"ok": True, "model": "bge", "hits": self.hits}) + "\n").encode())

    def stop(self):
        self.srv.close()


def freeze(root):
    for dirpath, dirnames, filenames in os.walk(root):
        for f in filenames:
            p = os.path.join(dirpath, f)
            os.chmod(p, os.stat(p).st_mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
        os.chmod(dirpath, os.stat(dirpath).st_mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))


def thaw(root):
    for dirpath, dirnames, filenames in os.walk(root):
        os.chmod(dirpath, os.stat(dirpath).st_mode | stat.S_IWUSR)
        for f in filenames:
            p = os.path.join(dirpath, f)
            os.chmod(p, os.stat(p).st_mode | stat.S_IWUSR)


class ReaderCannotWrite(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="khms-ro-")
        cls.root = os.path.join(cls.tmp, "shared base")
        cls.home = os.path.join(cls.tmp, "home")
        cls.reader_state = os.path.join(cls.tmp, "reader state")
        cls.sockdir = os.path.join(cls.tmp, "sock")
        for d in (cls.home, cls.sockdir):
            os.makedirs(d)
        w = cls.writer_env()
        subprocess.run([os.path.join(TOOLS, "khms_init.sh"), cls.root], check=True, capture_output=True, env=w)
        inbox = os.path.join(cls.root, "memory", "inbox", "2026-03-05.md")
        with open(inbox, "w", encoding="utf-8") as fh:
            fh.write(CARDS)
        for cmd in ([sys.executable, os.path.join(TOOLS, "approve_inbox.py"), inbox],
                    [sys.executable, os.path.join(TOOLS, "build_views.py")],
                    [os.path.join(TOOLS, "recall.sh"), "heater relay"]):
            r = subprocess.run(cmd, capture_output=True, text=True, env=w)
            assert r.returncode == 0, r.stdout + r.stderr
        # A FRESH shared dense index manifest (hashes of every card) and a STALE one (one card missing).
        code = ("import sys, json, os; sys.path.insert(0, sys.argv[1]); import embed_index as ei, khms_search as ks; "
                "cards = ks.load_cards(); m = ei.MODELS['bge']['hf']; "
                "h = {c['id']: ei.content_hash(ei.card_text(c), m) for c in cards}; "
                "os.makedirs(ei.EMBED_DIR, exist_ok=True); "
                "json.dump({'model_id': m, 'hashes': h, 'count': len(h), 'built': 'test'}, "
                "open(ei.paths('bge')[1], 'w')); "
                "h.pop(sorted(h)[0]); "
                "json.dump({'model_id': m, 'hashes': h, 'count': len(h), 'built': 'test'}, "
                "open(ei.paths('bge')[1] + '.stale', 'w'))")
        r = subprocess.run([sys.executable, "-c", code, TOOLS], capture_output=True, text=True, env=w)
        assert r.returncode == 0, r.stderr
        cls.manifest = os.path.join(cls.root, "state", "shared", "embed", "bge.manifest.json")
        cls.daemon = FakeDaemon(os.path.join(cls.sockdir, "embed.sock"), [["K-00002", 0.9], ["K-00001", 0.5]])
        cls.daemon.start()
        freeze(cls.root)
        cls.before = fingerprint(cls.root)

    @classmethod
    def tearDownClass(cls):
        cls.daemon.stop()
        thaw(cls.root)
        shutil.rmtree(cls.tmp, True)

    @classmethod
    def writer_env(cls, **kw):
        e = {"PATH": os.environ["PATH"], "HOME": cls.home, "KHMS_ROOT": cls.root, "KHMS_NO_SNAPSHOT": "1",
             "PYTHONDONTWRITEBYTECODE": "1"}
        e.update(kw)
        return e

    def reader_env(self, **kw):
        e = self.writer_env(KHMS_READ_ONLY="1", KHMS_STATE=self.reader_state,
                            KHMS_DENSE_SOCKET=os.path.join(self.sockdir, "embed.sock"))
        e.update(kw)
        return e

    def run_reader(self, cmd, **kw):
        return subprocess.run(cmd, capture_output=True, text=True, env=self.reader_env(**kw))

    def assertBaseUnchanged(self):
        self.assertEqual(fingerprint(self.root), self.before)

    def tearDown(self):
        self.assertBaseUnchanged()

    # ------------------------------------------------------------------ the freeze is real
    def test_the_frozen_base_rejects_a_write(self):
        if os.geteuid() == 0:
            self.skipTest("root ignores file modes; the fingerprint still guards")
        with self.assertRaises(PermissionError):
            open(os.path.join(self.root, "memory", "probe"), "w")

    # ------------------------------------------------------------------ recall in every channel state
    def test_lexical_recall_logs_only_into_the_reader_state(self):
        r = self.run_reader([os.path.join(TOOLS, "recall.sh"), "--lexical", "heater", "relay"])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("K-00001", r.stdout)
        self.assertIn("heater relay", open(os.path.join(self.reader_state, "recall.log")).read())

    def test_dense_with_a_fresh_shared_index_is_queried_not_written(self):
        r = self.run_reader([os.path.join(TOOLS, "recall.sh"), "storm", "sensor"])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("channel=hybrid", r.stdout)

    def test_dense_with_a_stale_shared_index_falls_back_to_lexical_labelled(self):
        thaw(self.root)
        os.replace(self.manifest, self.manifest + ".fresh")
        shutil.copy2(self.manifest + ".stale", self.manifest)
        freeze(self.root)
        before = fingerprint(self.root)
        try:
            r = self.run_reader([os.path.join(TOOLS, "recall.sh"), "storm", "sensor"])
            self.assertEqual(fingerprint(self.root), before)
        finally:
            thaw(self.root)
            os.replace(self.manifest + ".fresh", self.manifest)
            freeze(self.root)
            os.utime(self.manifest, ns=(self.before["state/shared/embed/bge.manifest.json"][1],) * 2)
            self.before = fingerprint(self.root)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("KHMS-INDEX-STALE", r.stdout)
        self.assertIn("lexical(fallback: stale index, read-only)", r.stdout)

    def test_dense_unavailable_falls_back_to_lexical_labelled(self):
        r = self.run_reader([os.path.join(TOOLS, "recall.sh"), "storm", "sensor"],
                            KHMS_DENSE_SOCKET=os.path.join(self.sockdir, "nobody.sock"),
                            KHMS_EMBED_PY="/nonexistent/python3")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("lexical(fallback: daemon down)", r.stdout)

    def test_missing_agent_state_is_created_outside_the_base(self):
        fresh = os.path.join(self.tmp, "brand new state")
        r = self.run_reader([os.path.join(TOOLS, "recall.sh"), "--lexical", "rain"], KHMS_STATE=fresh)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(os.path.exists(os.path.join(fresh, "recall.log")))

    def test_precheck_runs_and_logs_only_into_the_reader_state(self):
        r = self.run_reader([os.path.join(TOOLS, "precheck.sh"), "gotcha"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("GOTCHA K-00002", r.stdout)
        self.assertTrue(os.path.exists(os.path.join(self.reader_state, "precheck.log")))

    def test_a_reader_whose_state_points_into_the_base_writes_nothing(self):
        r = self.run_reader([os.path.join(TOOLS, "recall.sh"), "--lexical", "rain"],
                            KHMS_STATE=os.path.join(self.root, "state"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("not logged", r.stderr)

    # ------------------------------------------------------------------ every mutator refuses
    def test_every_mutator_refuses(self):
        inbox = os.path.join(self.root, "memory", "inbox", "2026-03-05.md")
        cases = [
            ([sys.executable, os.path.join(TOOLS, "approve_inbox.py"), inbox], 4),
            ([os.path.join(TOOLS, "new_card.sh"), "2"], 4),
            ([sys.executable, os.path.join(TOOLS, "khms_alloc.py"), "tombstone"], 4),
            ([sys.executable, os.path.join(TOOLS, "build_views.py")], 4),
            ([sys.executable, os.path.join(TOOLS, "embed_index.py"), "--model", "bge"], 4),
            ([sys.executable, os.path.join(TOOLS, "verify_relations.py"), inbox], 4),
            ([sys.executable, os.path.join(TOOLS, "nearest_cards.py"), inbox, "--no-dense"], None),
            ([os.path.join(TOOLS, "khms_snapshot.sh"), "--restore", "latest", "memory/know"], 3),
            ([os.path.join(TOOLS, "khms_init.sh"), self.root], 4),
        ]
        for cmd, code in cases:
            with self.subTest(cmd=os.path.basename(cmd[1] if cmd[0] == sys.executable else cmd[0])):
                r = self.run_reader(cmd, BRAIN_SNAPSHOT_ROOT=os.path.join(self.tmp, "snaps"))
                self.assertNotIn("Traceback", r.stderr)
                if code is not None:
                    self.assertEqual(r.returncode, code, r.stdout + r.stderr)
                self.assertBaseUnchanged()


class FingerprintPositiveControl(unittest.TestCase):
    """The guard is only evidence if it notices a write: a writer on a writable copy changes it."""

    def test_a_writer_changes_the_fingerprint(self):
        tmp = tempfile.mkdtemp(prefix="khms-ro-ctl-")
        self.addCleanup(shutil.rmtree, tmp, True)
        root = os.path.join(tmp, "base")
        e = {"PATH": os.environ["PATH"], "HOME": tmp, "KHMS_ROOT": root}
        subprocess.run([os.path.join(TOOLS, "khms_init.sh"), root], check=True, capture_output=True, env=e)
        before = fingerprint(root)
        r = subprocess.run([os.path.join(TOOLS, "new_card.sh")], capture_output=True, text=True, env=e)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotEqual(fingerprint(root), before)


if __name__ == "__main__":
    unittest.main()
