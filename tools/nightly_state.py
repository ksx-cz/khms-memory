#!/usr/bin/env python3
"""Is the night pipeline running right now? -- ONE answer for every reader.

Usage: python3 tools/nightly_state.py {status | wip <inbox> [names ...]}   (imported by khms_hook.py; run by
a deployment's review ping)

The SessionStart hook and any morning review ping ask THIS module, so they cannot disagree about whether the
inbox they see is a finished night or a half-written one.

WHAT "RUNNING" MEANS. The night holds an flock on its lock file (KHMS_NIGHTLY_LOCK) for its whole run. The
kernel lists every held flock in /proc/locks; this module only READS that table and never takes the lock --
taking it even for a moment could make a scheduled run exit "already running". The pid in the table is the
`flock(1)` helper's (already exited), so the lock is matched by the lock FILE's device and inode, never by pid.

WHO HOLDS IT AND SINCE WHEN. The holder writes one line into the lock file AFTER it won the lock
(`<kind> <iso start> pid=<pid> [nk=<night key>]`, kind = nightly | weekly). The file's mtime is not a start
time: `exec 9>"$LOCK"` truncates the file BEFORE `flock -n` decides, so every refused second launch would move
it. A lock file without that line (an older script) falls back to the mtime.

The weekly synthesis holds the same lock for the seconds it takes to snapshot its inbox; its holder line says
`weekly`, and `in_progress()` reports it as such -- `night_running()` answers no for it.

CLI (for shell readers; exit codes are the answer):
  nightly_state.py [--brain B] status          -> `RUNNING <epoch> <HH:MM> <nk|->` rc 0
                                                  `IDLE` rc 1 · `UNKNOWN <why>` rc 3
  nightly_state.py [--brain B] wip INBOX NAME... -> the NAMEs the running night wrote (one per line)
Environment: KHMS_NIGHTLY_LOCK (lock path), KHMS_PROC_LOCKS (lock table).
"""
import argparse
import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import khms_paths as P  # noqa: E402

PROC_LOCKS = "/proc/locks"
NIGHT_OWNER_SLACK_S = 120     # owner file is written within this of taking the lock
WIP_SLACK_S = 2               # a file written this close to the start belongs to the night
DEFAULT_BRAIN = P.soft("ROOT")


def lock_path(brain):
    return (os.environ.get("KHMS_NIGHTLY_LOCK") or P.soft("KHMS_NIGHTLY_LOCK")
            or os.path.join(brain, "state", "shared", "nightly.lock"))


def proc_locks_path():
    return os.environ.get("KHMS_PROC_LOCKS") or PROC_LOCKS


def _held(st, table):
    want = (os.major(st.st_dev), os.minor(st.st_dev), st.st_ino)
    with open(table, encoding="ascii", errors="replace") as fh:
        for ln in fh:
            f = ln.split()
            if len(f) < 6 or f[1] != "FLOCK":          # `->` lines are waiters
                continue
            try:
                maj, mnr, ino = f[5].split(":")
                key = (int(maj, 16), int(mnr, 16), int(ino))
            except ValueError:
                continue
            if key == want:
                return True
    return False


def _holder_line(path):
    """(kind, start epoch, nk) from the holder's line, or None when the file has none."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            first = fh.readline().split()
    except OSError:
        return None
    if len(first) < 2 or first[0] not in ("nightly", "weekly"):
        return None
    try:
        start = datetime.datetime.fromisoformat(first[1]).timestamp()
    except ValueError:
        return None
    nk = ""
    for tok in first[2:]:
        if tok.startswith("nk="):
            nk = tok[3:]
    return first[0], start, nk


def _owner_nk(brain, start):
    """Night key of the run whose `.run-<NK>/owner` was written closest to `start`."""
    staging = os.path.join(brain, "memory", "inbox", ".staging")
    nk, best = "", None
    try:
        names = sorted(os.listdir(staging))
    except OSError:
        return ""
    for d in names:
        if not d.startswith(".run-"):
            continue
        try:
            with open(os.path.join(staging, d, "owner"), encoding="utf-8") as fh:
                t = datetime.datetime.fromisoformat(fh.read().split()[0]).timestamp()
        except (OSError, ValueError, IndexError):
            continue
        gap = t - start
        if -NIGHT_OWNER_SLACK_S <= gap <= NIGHT_OWNER_SLACK_S and (best is None or abs(gap) < best):
            best, nk = abs(gap), d[len(".run-"):]
    return nk


def in_progress(brain=DEFAULT_BRAIN, lock=None, table=None):
    """{"kind", "start", "nk"} while a process holds the nightly's flock; None when nobody
    holds it (or there is no lock file). Raises OSError when the lock table cannot be read —
    the caller says so instead of guessing."""
    lock = lock or lock_path(brain)
    try:
        st = os.stat(lock)
    except FileNotFoundError:
        return None
    if not _held(st, table or proc_locks_path()):
        return None
    holder = _holder_line(lock)
    if holder is None:                       # an older script: start = mtime (exec 9>)
        kind, start, nk = "nightly", st.st_mtime, ""
    else:
        kind, start, nk = holder
    if kind == "nightly" and not nk:
        nk = _owner_nk(brain, start)
    return {"kind": kind, "start": start, "nk": nk}


def night_running(brain=DEFAULT_BRAIN, lock=None, table=None):
    """in_progress() restricted to the NIGHT: the weekly's snapshot hold is not a night."""
    info = in_progress(brain, lock, table)
    return info if info and info["kind"] == "nightly" else None


def split_ready_wip(names, inbox, night):
    """(ready, wip): files written before the running night started are reviewable now;
    files written since its start are the night's work in progress."""
    if not night:
        return list(names), []
    ready, wip = [], []
    for f in names:
        try:
            m = os.path.getmtime(os.path.join(inbox, f))
        except OSError:
            m = 0
        (wip if m >= night["start"] - WIP_SLACK_S else ready).append(f)
    return ready, wip


def hhmm(t):
    return datetime.datetime.fromtimestamp(t).strftime("%H:%M")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--brain", default=DEFAULT_BRAIN)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    w = sub.add_parser("wip")
    w.add_argument("inbox")
    w.add_argument("names", nargs="*")
    a = ap.parse_args(argv)
    try:
        night = night_running(a.brain)
    except OSError as exc:
        print("UNKNOWN %s" % exc.__class__.__name__)
        return 3
    if a.cmd == "status":
        if night is None:
            print("IDLE")
            return 1
        print("RUNNING %d %s %s" % (night["start"], hhmm(night["start"]), night["nk"] or "-"))
        return 0
    _ready, wip = split_ready_wip(a.names, a.inbox, night)
    for f in wip:
        print(f)
    return 0


if __name__ == "__main__":
    sys.exit(main())
