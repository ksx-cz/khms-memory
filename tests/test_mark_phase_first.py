"""The night's order since #237: the GENERATOR first, then the PER-MARK EVALUATOR over one time-sorted queue.

Before #237 the MARK stage ran first (PMARKFIRST, #209) in chunks of 60 with an agent and tools, and the sweep
after it wrote candidate cards that a second call consolidated. Now the sweep is a generator of MARK-shaped lines
(memory/ledger/generated/), committed with its window before the evaluator builds its queue, so the same night
judges those lines among the journal MARKs, one by one, and publishes the result without a consolidate call.

The fixtures vary the dimensions the old defects lived in: a carry queue already holding more than the former
gate, a generator that fails or runs out of its time, a kill right before and right after the evaluator's commit
and in the middle of the evaluator; and the next night, which must neither lose nor repeat a result.
"""
import json
import os
import re
import subprocess

import pytest

from test_night_pipeline import TOOLS, env_for, journal, log_of, units
from test_sweep_resume import make_parts_base

CARRY_HELD = 230          # > the former MARK_PENDING_MAX (225)
STAMP0 = "2026-01-01T00:00:00+00:00"   # older than every fixture input (they are dated now + 1 h)
ID = r"[MG]-[0-9a-f]{8}"


def base(tmp_path, n_marks=12, carry=0):
    root, fake, tr = make_parts_base(tmp_path)
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
    return subprocess.run(["bash", os.path.join(TOOLS, "nightly_distill.sh")],
                          env=env_for(tmp_path, root, fake, **extra), capture_output=True, text=True,
                          timeout=300)


def calls(tmp_path, since=0):
    p = tmp_path / "calls.txt.jsonl"
    return [json.loads(ln) for ln in p.read_text().splitlines()][since:] if p.exists() else []


def journal_ids(root):
    out = subprocess.run(["python3", os.path.join(TOOLS, "mark_ledger.py"), "--root", str(root), "scan",
                          str(root / "journal" / "2099-01-01.md")], capture_output=True, text=True, check=True)
    ids = re.findall(r"\b(M-[0-9a-f]{8})\b", out.stdout)
    assert ids, out.stdout
    return ids


def generated_ids(root):
    import sys
    sys.path.insert(0, TOOLS)
    import mark_ledger as ML
    got = []
    for f in sorted((root / "memory/ledger/generated").glob("*.md")):
        got += [mk["id"] for mk in ML.scan_generated(str(f))]
    return list(dict.fromkeys(got))      # one id however many windows wrote the same line (the ledger keeps one)


def ids_in(files):
    got = []
    for f in files:
        got += re.findall(rf"(?m)^MARKS: ({ID})", f.read_text())
        got += re.findall(rf"(?m)^- ({ID}) ::", f.read_text())
    return got


def published_ids(root):
    return ids_in(units(root))


def carried_ids(root):
    return ids_in(p for p in (root / "memory/ledger/carry").rglob("*.md") if not p.name.endswith(".out.md"))


def stamp(root):
    return (root / "state/shared/last-nightly").read_text().strip()


def test_the_generator_runs_first_and_its_lines_join_the_journal_marks_in_one_evaluator_queue(tmp_path):
    root, fake, tr = base(tmp_path, carry=CARRY_HELD)
    jids = journal_ids(root)
    r = run(tmp_path, root, fake, KHMS_TRANSCRIPTS=tr)
    log = log_of(root)
    assert r.returncode == 0, log[-4000:]
    cs = calls(tmp_path)
    assert cs[0]["stage"] == "sweep", [c["stage"] for c in cs[:5]]                   # the generator first
    last_gen = max(i for i, c in enumerate(cs) if c["stage"] == "sweep")
    first_eval = min(i for i, c in enumerate(cs) if c["stage"] == "permark")
    assert last_gen < first_eval, "every generator part runs before the evaluator"
    gids = generated_ids(root)
    assert gids, "the generator's lines are committed to memory/ledger/generated/"
    evaluated = [c["id"] for c in cs if c["stage"] == "permark"]
    assert sorted(evaluated) == sorted(jids + gids), "ONE queue: every journal MARK and generated line, once"
    assert sorted(published_ids(root)) == sorted(jids + gids), "every id exactly once in the published units"
    assert not [c for c in cs if c["stage"] == "consolidate" and "-m01" in " ".join(c["args"])]
    assert len([c for c in cs if c["stage"] == "consolidate"]) == 1, "only the carried pre-#237 batch"
    text = "".join(u.read_text() for u in units(root))
    assert len(re.findall(r"(?m)^#{2,3} +[A-Z]\d+: carried candidate", text)) == CARRY_HELD
    assert stamp(root) != STAMP0, "the committed generated window is stamped"
    assert not list((root / "memory/ledger/carry").rglob("*.md")), "the carry queue is drained"
    # a generated line is not a journal MARK: it is in neither the journal nor under an M- id
    assert not re.search(r"G-[0-9a-f]{8}", (root / "journal/2099-01-01.md").read_text())
    marks = [json.loads(ln) for ln in open(root / "memory/ledger/marks.jsonl")]
    assert {m["origin"] for m in marks if m["id"].startswith("G-")} == {"generated"}
    assert all(m.get("origin") != "generated" for m in marks if m["id"].startswith("M-"))


@pytest.mark.parametrize("how", ["timeout", "fail"])
def test_a_generator_that_times_out_or_fails_still_lets_the_evaluator_judge_the_journal(tmp_path, how):
    root, fake, tr = base(tmp_path)
    jids = journal_ids(root)
    bad = {"timeout": {"CUT_PART": 1, "SWEEP_BUDGET_S": 25}, "fail": {"FAKE_FAIL": "sweep"}}[how]
    r = run(tmp_path, root, fake, KHMS_TRANSCRIPTS=tr, **bad)
    log = log_of(root)
    assert r.returncode == 1 and "nightly INCOMPLETE" in log and " sweep" in log.split("failed stages:")[-1], log[-3000:]
    assert stamp(root) == STAMP0, "a failed generator must not move its stamp"
    assert not generated_ids(root), "nothing of an incomplete window is committed"
    assert sorted(published_ids(root)) == sorted(jids), "the journal MARKs are judged anyway"

    # the next night: the generator finishes its frozen list, its lines are judged, no mark twice
    mark = len(calls(tmp_path))
    r2 = run(tmp_path, root, fake, KHMS_TRANSCRIPTS=tr, NIGHTLY_DATE="2099-01-03")
    log = log_of(root)
    assert r2.returncode == 0, log[-4000:]
    again = [c["id"] for c in calls(tmp_path, mark) if c["stage"] == "permark"]
    gids = generated_ids(root)
    assert gids and sorted(again) == sorted(gids), (again, gids)
    assert sorted(published_ids(root)) == sorted(jids + gids)
    assert stamp(root) != STAMP0, "the finished generator stamps its window"


@pytest.mark.parametrize("point", ["marks-before-commit-1", "marks-after-commit-1"])
def test_a_kill_around_the_evaluator_commit_neither_loses_nor_duplicates_a_result(tmp_path, point):
    root, fake, tr = base(tmp_path)
    r = run(tmp_path, root, fake, KHMS_TRANSCRIPTS=tr, NIGHTLY_FAULT=point)
    log = log_of(root)
    assert r.returncode == -9 and f"FAULT INJECTED at {point}" in log, (r.returncode, log[-3000:])
    jids, gids = journal_ids(root), generated_ids(root)
    held = carried_ids(root)
    if point.startswith("marks-after"):
        assert sorted(held) == sorted(jids + gids), "the whole result is committed"
        assert (root / "memory/ledger/carry/2099-01-02-m01/cons-01.out.md").exists(), "with its ready output"
    else:
        assert not held, "nothing of an uncommitted evaluator run is used"

    mark = len(calls(tmp_path))
    r2 = run(tmp_path, root, fake, KHMS_TRANSCRIPTS=tr, NIGHTLY_DATE="2099-01-03")
    log = log_of(root)
    assert r2.returncode == 0, log[-4000:]
    redone = [c["id"] for c in calls(tmp_path, mark) if c["stage"] == "permark"]
    assert not set(redone) & set(held), "a committed result is never evaluated again"
    assert sorted(published_ids(root)) == sorted(jids + gids), "every id exactly once after the resume"
    assert not [c for c in calls(tmp_path, mark) if c["stage"] == "consolidate"], "published without a model call"
    assert not list((root / "memory/ledger/carry").rglob("*.md"))


def test_a_kill_in_the_middle_of_the_evaluator_loses_nothing(tmp_path):
    root, fake, tr = base(tmp_path)
    jids = journal_ids(root)
    r = run(tmp_path, root, fake, KHMS_TRANSCRIPTS=tr, FAKE_PERMARK_KILL=jids[4])
    assert r.returncode == -9, (r.returncode, log_of(root)[-2000:])
    assert not carried_ids(root) and not published_ids(root)
    r2 = run(tmp_path, root, fake, KHMS_TRANSCRIPTS=tr, NIGHTLY_DATE="2099-01-03")
    assert r2.returncode == 0, log_of(root)[-4000:]
    assert sorted(published_ids(root)) == sorted(jids + generated_ids(root))
