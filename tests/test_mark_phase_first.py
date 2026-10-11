"""The MARK stage is the night's FIRST model stage and does not depend on the sweep (PMARKFIRST, #209).

The 2026-10-11 manual night logged "648 candidate(s) already wait for consolidation (>= 225) — 42 chunk(s) left
OPEN": the MARK stage ran AFTER the sweep, inside its success branch, and stopped as soon as 225 candidates
waited for consolidation, so the 2537 OPEN journal marks never moved. Diblin's specification (2026-10-11 10:11):
run it before the sweep, outside the sweep's success branch, with its own budget counted from its own start, no
candidate-count gate, and every finished result -- verdicts included -- committed to the carry queue in its own
transaction before the sweep, without moving the sweep's stamp.

The fixtures vary exactly the dimensions the defect lived in: OPEN marks AND a carry queue already holding more
than 225 candidates AND a sweep that yields 648 candidates (or fails, or runs out of its time); a kill right
before and right after a chunk's commit; and the next night, which must neither lose nor repeat a result.
"""
import json
import os
import re
import subprocess

import pytest

from test_night_pipeline import TOOLS, env_for, journal, log_of, units
from test_sweep_resume import PART_FAKE, make_parts_base

# The prepare part writes PART_CANDS candidates (default 1) -- 648 reproduces the 2026-10-11 sweep.
MANY_FAKE = PART_FAKE.replace(
    "    open(out, 'w').write('# nightly sweep - candidates\\n' + card('C1', f'prepare part {k:03d} found widget fact').replace('src=journal', 'src=digest'))\n",
    "    n = int(os.environ.get('PART_CANDS', '1')) if k == 1 else 1\n"
    "    open(out, 'w').write('# nightly sweep - candidates\\n' + ''.join(card(f'C{i}', f'prepare part {k:03d} candidate {i:04d} found widget fact').replace('src=journal', 'src=digest') for i in range(1, n + 1)))\n",
    1)
assert MANY_FAKE != PART_FAKE

CARRY_HELD = 230          # > the former MARK_PENDING_MAX (225)
SWEEP_CANDS = 648         # the 2026-10-11 sweep
STAMP0 = "2026-01-01T00:00:00+00:00"   # older than every fixture input (they are dated now + 1 h)


def base(tmp_path, n_marks=30, carry=CARRY_HELD):
    root, fake, tr = make_parts_base(tmp_path)
    fake.write_text(MANY_FAKE)
    j = root / "journal" / "2099-01-01.md"
    t = j.stat().st_mtime
    j.write_text(journal(n_marks))                                           # n_marks MARKs + 1 status line
    os.utime(j, (t, t))
    (root / "state/shared/last-nightly").write_text(STAMP0 + "\n")
    if carry:
        d = root / "memory/ledger/carry/2099-01-01"
        d.mkdir(parents=True)
        (d / "cons-01.md").write_text("# consolidate batch 1 of run 2099-01-01\n" + "".join(
            f"### C{i}: carried candidate {i:04d}\nWHAT: carried widget fact {i:04d}\nRELATION: new\n"
            f"**QUOTES:**\n- src=digest :: carried widget fact {i:04d}\n\n" for i in range(1, carry + 1)))
    return root, fake, tr


def run(tmp_path, root, fake, **extra):
    extra.setdefault("CONSOLIDATE_BATCH", "100")
    extra.setdefault("MARK_CHUNK", "10")
    return subprocess.run(["bash", os.path.join(TOOLS, "nightly_distill.sh")],
                          env=env_for(tmp_path, root, fake, **extra), capture_output=True, text=True,
                          timeout=300)


def calls(tmp_path, since=0):
    p = tmp_path / "calls.txt.jsonl"
    return [json.loads(ln) for ln in p.read_text().splitlines()][since:] if p.exists() else []


def mark_ids(root):
    """{id: kind} of every tracked journal MARK (the ledger's own scan, no model)."""
    out = subprocess.run(["python3", os.path.join(TOOLS, "mark_ledger.py"), "--root", str(root), "scan",
                          str(root / "journal" / "2099-01-01.md")], capture_output=True, text=True, check=True)
    ids = re.findall(r"\b(M-[0-9a-f]{8})\b", out.stdout)
    assert ids, out.stdout
    return ids


def published_ids(root):
    """Every MARK id the published units account for, as a list (a repeat shows as a duplicate)."""
    got = []
    for u in units(root):
        got += re.findall(r"(?m)^MARKS: (M-[0-9a-f]{8})", u.read_text())
        got += re.findall(r"(?m)^- (M-[0-9a-f]{8}) ::", u.read_text())
    return got


def carried_ids(root):
    got = []
    for f in (root / "memory/ledger/carry").rglob("*.md"):
        got += re.findall(r"(?m)^MARKS: (M-[0-9a-f]{8})", f.read_text())
        got += re.findall(r"(?m)^- (M-[0-9a-f]{8}) ::", f.read_text())
    return got


def mark_dirs(root):
    return sorted(p for p in (root / "memory/ledger/carry").glob("*-m[0-9]*") if p.is_dir())


def stamp(root):
    return (root / "state/shared/last-nightly").read_text().strip()


def test_the_mark_stage_is_the_first_model_call_and_a_full_carry_and_a_648_candidate_sweep_do_not_stop_it(tmp_path):
    root, fake, tr = base(tmp_path)
    ids = mark_ids(root)
    r = run(tmp_path, root, fake, KHMS_TRANSCRIPTS=tr, PART_CANDS=SWEEP_CANDS)
    log = log_of(root)
    assert r.returncode == 0, log[-4000:]
    gate = [ln for ln in log.splitlines() if "already wait for consolidation" in ln]
    assert not gate, gate                                     # the 2026-10-11 gate line, verbatim on the old code
    cs = calls(tmp_path)
    assert cs[0]["stage"] == "marks", [c["stage"] for c in cs[:5]]                   # the first model call
    n_marks = sum(c["stage"] == "marks" for c in cs)
    assert n_marks == -(-len(ids) // 10), (n_marks, len(ids))                          # every chunk ran
    last_mark = max(i for i, c in enumerate(cs) if c["stage"] == "marks")
    first_sweep = min(i for i, c in enumerate(cs) if c["stage"] == "sweep")
    assert last_mark < first_sweep, "every MARK chunk runs before the sweep"
    assert sorted(published_ids(root)) == sorted(ids), "every mark exactly once, verdicts included"
    text = "".join(u.read_text() for u in units(root))
    assert len(re.findall(r"(?m)^#{2,3} +[A-Z]\d+: prepare part 001 candidate", text)) == SWEEP_CANDS
    assert len(re.findall(r"(?m)^#{2,3} +[A-Z]\d+: carried candidate", text)) == CARRY_HELD
    assert not list((root / "memory/ledger/carry").rglob("*.md")), "the carry queue is drained"


@pytest.mark.parametrize("how", ["timeout", "fail"])
def test_a_sweep_that_times_out_or_fails_keeps_the_mark_results_and_does_not_move_its_stamp(tmp_path, how):
    root, fake, tr = base(tmp_path, carry=0)
    ids = mark_ids(root)
    bad = {"timeout": {"CUT_PART": 1, "SWEEP_BUDGET_S": 25}, "fail": {"FAKE_FAIL": "sweep"}}[how]
    r = run(tmp_path, root, fake, KHMS_TRANSCRIPTS=tr, **bad)
    log = log_of(root)
    assert r.returncode == 1 and "nightly INCOMPLETE" in log and " sweep" in log.split("failed stages:")[-1], log[-3000:]
    assert stamp(root) == STAMP0, "a failed sweep must not move its stamp"
    cs = calls(tmp_path)
    assert cs[0]["stage"] == "marks"
    assert sorted(published_ids(root)) == sorted(ids), "the MARK results survive the failed sweep"
    assert log.index("marks-1 committed") < log.index("sweep part 1/"), "committed BEFORE the sweep"
    assert not mark_dirs(root), "the MARK chunks are consolidated and published the same night"

    # the next night: the sweep finishes its frozen list, no mark is processed or published again
    mark = len(cs)
    r2 = run(tmp_path, root, fake, KHMS_TRANSCRIPTS=tr, NIGHTLY_DATE="2099-01-03")
    log = log_of(root)
    assert r2.returncode == 0, log[-4000:]
    assert not [c for c in calls(tmp_path, mark) if c["stage"] == "marks"], "nothing left to queue"
    assert sorted(published_ids(root)) == sorted(ids)
    assert stamp(root) != STAMP0, "the finished sweep stamps its window"


@pytest.mark.parametrize("point", ["marks-before-commit-2", "marks-after-commit-2"])
def test_a_kill_around_a_chunk_commit_neither_loses_nor_duplicates_a_mark_result(tmp_path, point):
    root, fake, tr = base(tmp_path, carry=0)
    ids = mark_ids(root)
    r = run(tmp_path, root, fake, KHMS_TRANSCRIPTS=tr, NIGHTLY_FAULT=point)
    log = log_of(root)
    assert r.returncode == -9 and f"FAULT INJECTED at {point}" in log, (r.returncode, log[-3000:])
    assert [c["stage"] for c in calls(tmp_path)] == ["marks", "marks"], "killed in the MARK stage, before the sweep"
    committed = 2 if point.startswith("marks-after") else 1
    dirs = mark_dirs(root)
    assert [d.name for d in dirs] == [f"2099-01-02-m{k:02d}" for k in range(1, committed + 1)], dirs
    assert not [d for d in dirs if (d / "WINDOW").exists()], "a MARK chunk is not a window of the sweep"
    held = carried_ids(root)
    assert len(held) == len(set(held)) == 10 * committed, held                         # the results survived
    assert stamp(root) == STAMP0

    mark = len(calls(tmp_path))
    r2 = run(tmp_path, root, fake, KHMS_TRANSCRIPTS=tr, NIGHTLY_DATE="2099-01-03")
    log = log_of(root)
    assert r2.returncode == 0, log[-4000:]
    redone = [m for c in calls(tmp_path, mark) if c["stage"] == "marks"
              for m in re.findall(r"(M-[0-9a-f]{8})", open(re.search(r"(?m)^- MARK QUEUE chunk: (.+)$",
                                                                      c["prompt"]).group(1)).read())]
    assert not set(redone) & set(held), "a committed result is never queued again"
    assert sorted(published_ids(root)) == sorted(ids), "every mark exactly once after the resume"
    assert not list((root / "memory/ledger/carry").rglob("*.md"))
