#!/usr/bin/env python3
"""khms_alloc.py -- the ONE identifier reservation primitive of a KHMS base.

Usage:
  khms_alloc.py card [N]        reserve N card ids (default 1), print them one per line (K-00042)
  khms_alloc.py tombstone [N]   reserve N tombstone ids (TS-00007)
  khms_alloc.py --peek card     print the next card id without reserving anything
  import khms_alloc; khms_alloc.reserve("card", 3) -> [42, 43, 44]

Every writer that hands out an id -- new_card.sh, approve_inbox.py, the tombstone ledger -- goes through
`reserve`, so two agents (or two clones of the code) working on one base never receive the same number:

  1. refuse at once on a read-only deployment (KHMS_READ_ONLY=1), before any lock or write;
  2. take an exclusive lock on ${KHMS_SHARED_STATE}/alloc.lock (fcntl.flock: released by the kernel when the
     holder dies, so a crashed writer never leaves a stale lock behind);
  3. read and validate the counter; the floor is one above the highest id that already exists, so a counter
     restored from an old copy can never hand out a number that is in use;
  4. persist the advanced counter (temporary file + fsync + rename) BEFORE printing a single id: an id that has
     been seen is never seen again, even if the caller dies; gaps are harmless because ids are opaque.

The counter is never moved backwards, and nothing else in the code writes it.
"""
import fcntl
import glob
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import khms_paths as P  # noqa: E402

KINDS = {
    "card": {"prefix": "K", "missing_ok": False},
    "tombstone": {"prefix": "TS", "missing_ok": True},
}


class AllocError(RuntimeError):
    pass


def counter_path(kind):
    if kind == "card":
        return P.COUNTER
    if kind == "tombstone":
        return os.path.join(P.TOMBSTONES, ".next_ts_id")
    raise AllocError(f"unknown id kind: {kind}")


def existing_floor(kind):
    """One above the highest id of this kind that exists anywhere in the base (1 for an empty base)."""
    hi = 0
    if kind == "card":
        pat = re.compile(r"K-(\d+)\.md$")
        for d in (P.KNOW, P.ARCHIVE_KNOW):
            for path in glob.glob(os.path.join(d, "K-*.md")):
                m = pat.search(path)
                if m:
                    hi = max(hi, int(m.group(1)))
    else:
        ledger = os.path.join(P.TOMBSTONES, "LEDGER.md")
        try:
            with open(ledger, encoding="utf-8") as fh:
                for m in re.finditer(r"\bTS-(\d+)\b", fh.read()):
                    hi = max(hi, int(m.group(1)))
        except FileNotFoundError:
            pass
    return hi + 1


def _read(path, missing_ok):
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read().strip()
    except FileNotFoundError:
        if missing_ok:
            return 1
        raise AllocError(f"counter {path} is missing -- an uninitialised base? run khms_init.sh")
    if not re.fullmatch(r"\d+", text) or int(text) < 1:
        raise AllocError(f"counter {path} holds {text!r}, not a positive integer -- refusing to guess")
    return int(text)


def _write(path, value):
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(f"{value}\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def reserve(kind="card", n=1, floor_extra=0, path=None):
    """Reserve `n` consecutive ids of `kind`; returns the integers. `floor_extra` lets a caller that knows a
    higher id from another record (an in-memory ledger) raise the floor further. `path` overrides the
    counter file (tests); the lock is always the base's one allocator lock."""
    if n < 1:
        raise AllocError(f"cannot reserve {n} ids")
    P.require_writable(f"reserve {kind} ids")
    spec = KINDS.get(kind) or {}
    path = path or counter_path(kind)
    os.makedirs(P.SHARED_STATE, exist_ok=True)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(P.ALLOC_LOCK, "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            first = max(_read(path, spec.get("missing_ok", False)), existing_floor(kind), floor_extra)
            _write(path, first + n)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
    return list(range(first, first + n))


def peek(kind="card"):
    """The id the next reservation would return. Takes the lock shared; never writes."""
    path = counter_path(kind)
    lockfile = P.ALLOC_LOCK
    if os.path.exists(lockfile):
        with open(lockfile) as lock:
            fcntl.flock(lock, fcntl.LOCK_SH)
            try:
                return max(_read(path, KINDS[kind]["missing_ok"]), existing_floor(kind))
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
    return max(_read(path, KINDS[kind]["missing_ok"]), existing_floor(kind))


def fmt(kind, i):
    return f"{KINDS[kind]['prefix']}-{i:05d}"


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__.strip().split("\n\n")[0])
        return 0 if argv else 2
    try:
        if argv[0] == "--peek":
            kind = argv[1] if len(argv) > 1 else "card"
            print(fmt(kind, peek(kind)))
            return 0
        kind = argv[0]
        if kind not in KINDS:
            print(f"khms_alloc: unknown id kind {kind!r} (card | tombstone)", file=sys.stderr)
            return 2
        n = int(argv[1]) if len(argv) > 1 else 1
        ids = reserve(kind, n)
    except (AllocError, P.KhmsPathError, ValueError) as exc:
        print(f"khms_alloc: {exc}", file=sys.stderr)
        return 3
    # The range is already on disk; a closed pipe on the way out cannot un-reserve it.
    try:
        for i in ids:
            print(fmt(kind, i))
    except BrokenPipeError:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
