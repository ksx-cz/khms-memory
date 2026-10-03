"""Test isolation for the whole suite.

1. LIVE-WRITE GUARD. Before any test assigns its own root, the base this machine would resolve on its own
   ($KHMS_ROOT, else the registered ~/.config/khms/root) is fingerprinted -- path, size and mtime of every
   file -- and checked again after the session. The comparison is over the filesystem, so it catches writes
   by shell scripts and child processes, not only Python code. A changed fingerprint fails the run.
2. ISOLATION. Every KHMS_* variable is removed and HOME points at a fresh temporary directory, so no test
   can resolve the developer's real base, config or registered root by accident.
"""
import os
import shutil
import tempfile

import pytest

TOOLS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools")


def _protected_root():
    root = os.environ.get("KHMS_ROOT")
    if root:
        return root
    reg = os.path.join(os.environ.get("HOME", ""), ".config", "khms", "root")
    if os.path.isfile(reg):
        return (open(reg, encoding="utf-8").readline() or "").strip() or None
    return None


def fingerprint(root):
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in filenames + [d for d in dirnames if os.path.islink(os.path.join(dirpath, d))]:
            p = os.path.join(dirpath, name)
            try:
                st = os.lstat(p)
            except OSError:
                continue
            out[os.path.relpath(p, root)] = (st.st_size, st.st_mtime_ns)
    return out


_PROTECTED = _protected_root()
_BEFORE = fingerprint(_PROTECTED) if _PROTECTED and os.path.isdir(_PROTECTED) else None
_HOME = tempfile.mkdtemp(prefix="khms-test-home-")
for _k in [k for k in os.environ if k.startswith("KHMS_")]:
    del os.environ[_k]
os.environ["HOME"] = _HOME
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(_HOME, ignore_errors=True)
    if _BEFORE is not None:
        after = fingerprint(_PROTECTED)
        if after != _BEFORE:
            changed = sorted(set(after) ^ set(_BEFORE)
                             | {k for k in after if k in _BEFORE and after[k] != _BEFORE[k]})
            print(f"\nLIVE-WRITE GUARD: the protected base {_PROTECTED} changed during the test run: "
                  f"{changed[:10]}")
            session.exitstatus = 1


@pytest.fixture
def tools_dir():
    return TOOLS
