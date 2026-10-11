"""A sweep cut after k of its n parts keeps the k parts and the next run finishes the FROZEN list (#173).

The first night on the shared code (2026-10-11) had 42 one-chunk prepare parts at ~6-7 min each; the 150-min
sweep deadline stopped it at part 23, the 22 finished parts were thrown away with the rest, nothing was stamped,
and the next window was two nights long -- so it would fail the same way, every night, with a growing window.

The contract tested here:
  (a) a sweep cut by the deadline after k of n parts keeps those k parts (completion records against the frozen
      list of the night's inputs); the next run processes exactly the remaining n-k parts of THAT list -- not a
      new window -- and then stamps the window end of the frozen list, so no input is skipped;
  (b) a part whose run is killed while it runs is redone exactly once, and a finished part
      is never redone;
and in both cases every part's candidate reaches the inbox exactly once.
"""
import json
import os
import re
import subprocess
import time

from test_night_pipeline import FAKE, cards_in, env_for, log_of, make_base, units, TOOLS

# The fake prepare stage writes ONE candidate per part, labelled by its part number, so the inbox shows which
# parts arrived and how often. CUT_PART sleeps past the sweep deadline (the retry wrapper clips the call to
# the deadline, exactly as on 2026-10-11).
PART_FAKE = FAKE.replace(
    "else:\n    js = field(",
    "elif re.search(r'prepare PART (\\d+) OF (\\d+)', prompt):\n"
    "    k = int(re.search(r'prepare PART (\\d+) OF (\\d+)', prompt).group(1))\n"
    "    if os.environ.get('CUT_PART') == str(k):\n"
    "        import time; time.sleep(600)\n"
    "    if os.environ.get('KILL_PART') == str(k):\n"
    "        open(out, 'w').write('# nightly sweep - candidates\\n' + card('C1', 'half written part'))\n"
    "        driver = int(open(f'/proc/{os.getppid()}/stat').read().rsplit(')', 1)[1].split()[1])\n"
    "        os.kill(driver, 9)\n"
    "        sys.exit(1)\n"
    "    open(out, 'w').write('# nightly sweep - candidates\\n' + card('C1', f'prepare part {k:03d} found widget fact').replace('src=journal', 'src=digest'))\n"
    "else:\n    js = field(", 1)
assert PART_FAKE != FAKE


def make_parts_base(tmp_path):
    root, fake = make_base(tmp_path, n_marks=0)
    fake.write_text(PART_FAKE)
    tr = tmp_path / "source"
    tr.mkdir()
    f = tr / "s.jsonl"
    f.write_text("".join(json.dumps({"type": "user", "timestamp": "2099-01-01T10:00:00Z",
                                    "message": {"content": "widget unmarked calibration " * 80}}) + "\n"
                         for _ in range(90)))
    os.utime(f, (time.time() + 3600,) * 2)
    return root, fake, tr


def run(tmp_path, root, fake, **extra):
    return subprocess.run(["bash", os.path.join(TOOLS, "nightly_distill.sh")],
                          env=env_for(tmp_path, root, fake, **extra), capture_output=True, text=True,
                          timeout=300)


def sweep_calls(tmp_path, since=0):
    p = tmp_path / "calls.txt.jsonl"
    calls = [json.loads(ln) for ln in p.read_text().splitlines()][since:] if p.exists() else []
    out = []
    for c in calls:
        m = re.search(r"prepare PART (\d+) OF (\d+)", c["prompt"])
        if c["stage"] == "sweep" and m:
            out.append((int(m.group(1)), int(m.group(2)), re.findall(r"^  \* (.+)$", c["prompt"], re.M)))
    return out


def n_calls(tmp_path):
    p = tmp_path / "calls.txt.jsonl"
    return len(p.read_text().splitlines()) if p.exists() else 0


def candidates_per_part(root):
    counts = {}
    for p in units(root):
        for m in re.finditer(r"(?m)^#{2,3} +[A-Z]\d+[a-z]?: prepare part (\d{3})", p.read_text()):
            counts[int(m.group(1))] = counts.get(int(m.group(1)), 0) + 1
    return counts


def test_a_sweep_cut_by_the_deadline_keeps_its_parts_and_the_next_run_finishes_them(tmp_path):
    root, fake, tr = make_parts_base(tmp_path)
    stamp = root / "state/shared/last-nightly"
    cut = 3
    first = run(tmp_path, root, fake, KHMS_TRANSCRIPTS=tr, CUT_PART=cut, SWEEP_BUDGET_S=12)
    log = log_of(root)
    assert first.returncode == 1 and "nightly INCOMPLETE" in log, log
    calls1 = sweep_calls(tmp_path)
    n = calls1[0][1]
    assert n >= cut + 2, ("the fixture must have parts after the cut", n)
    assert [k for k, _n, _p in calls1] == list(range(1, cut + 1)), calls1
    assert not stamp.exists(), "a cut sweep must not stamp"
    frozen_chunks = {k: paths for k, _n, paths in calls1}

    mark = n_calls(tmp_path)
    second = run(tmp_path, root, fake, KHMS_TRANSCRIPTS=tr, NIGHTLY_DATE="2099-01-03")
    log = log_of(root)
    assert second.returncode == 0, log[-4000:]
    calls2 = sweep_calls(tmp_path, mark)
    assert [k for k, _n, _p in calls2] == list(range(cut, n + 1)), calls2      # exactly the n-k remaining
    assert all(nn == n for _k, nn, _p in calls2)
    assert frozen_chunks[cut] == calls2[0][2]                                   # the SAME frozen inputs
    assert all("2099-01-02" in p for _k, _n, paths in calls2 for p in paths)
    # the window marker advanced to the frozen window's end -- the first run's start, not the second's
    first_start = re.search(r"(\S+) nightly start \(night key 2099-01-02\)", log).group(1)
    assert stamp.read_text().strip() == first_start, (stamp.read_text(), first_start)
    assert candidates_per_part(root) == {k: 1 for k in range(1, n + 1)}
    assert not (root / "state/shared/sweep-failed").exists()
    assert not list((root / "memory/inbox/.staging").glob(".prepare-*"))      # the frozen list is consumed
    assert f"{cut - 1} of {n} part(s) kept" in log and "resuming the frozen prepare list" in log, log

    # and the run after that starts a NEW window, from the stamp
    mark = n_calls(tmp_path)
    third = run(tmp_path, root, fake, KHMS_TRANSCRIPTS=tr, NIGHTLY_DATE="2099-01-04")
    assert third.returncode == 0, log_of(root)[-3000:]
    assert all("2099-01-04" in p for _k, _n, paths in sweep_calls(tmp_path, mark) for p in paths)


def test_a_part_killed_before_its_completion_record_is_redone_once_and_finished_parts_never(tmp_path):
    root, fake, tr = make_parts_base(tmp_path)
    killed = 2
    # the agent process SIGKILLs the driver while its part runs (an OOM kill or a reboot, from the driver's view)
    first = run(tmp_path, root, fake, KHMS_TRANSCRIPTS=tr, KILL_PART=killed)
    assert first.returncode == -9, (first.returncode, log_of(root))
    calls1 = sweep_calls(tmp_path)
    n = calls1[0][1]
    assert [k for k, _n, _p in calls1] == list(range(1, killed + 1))
    assert not (root / "state/shared/last-nightly").exists()

    mark = n_calls(tmp_path)
    second = run(tmp_path, root, fake, KHMS_TRANSCRIPTS=tr, NIGHTLY_DATE="2099-01-03")
    assert second.returncode == 0, log_of(root)[-4000:]
    assert [k for k, _n, _p in sweep_calls(tmp_path, mark)] == list(range(killed, n + 1))
    all_calls = [k for k, _n, _p in sweep_calls(tmp_path)]
    assert all_calls.count(killed) == 2 and all(all_calls.count(k) == 1 for k in range(1, n + 1) if k != killed)
    assert candidates_per_part(root) == {k: 1 for k in range(1, n + 1)}


def test_a_damaged_frozen_list_is_set_aside_and_its_window_collected_again(tmp_path):
    root, fake, tr = make_parts_base(tmp_path)
    first = run(tmp_path, root, fake, KHMS_TRANSCRIPTS=tr, CUT_PART=2, SWEEP_BUDGET_S=12)
    assert first.returncode == 1
    staging = root / "memory/inbox/.staging"
    (staging / "nightly-2099-01-02-digest-part05.txt").unlink()        # an input of the frozen list is gone
    mark = n_calls(tmp_path)
    second = run(tmp_path, root, fake, KHMS_TRANSCRIPTS=tr, NIGHTLY_DATE="2099-01-03")
    log = log_of(root)
    assert second.returncode == 0 and "frozen list 2099-01-02 is DAMAGED" in log, log[-3000:]
    calls = sweep_calls(tmp_path, mark)
    assert [k for k, _n, _p in calls] == list(range(1, calls[0][1] + 1))     # the whole window, again
    assert all("2099-01-03" in p for _k, _n, paths in calls for p in paths)
    assert list(staging.glob(".prepare-2099-01-02.damaged-*"))
    assert (root / "state/shared/last-nightly").exists()


def test_a_dry_run_shows_the_resume_and_changes_nothing_of_the_frozen_list(tmp_path):
    root, fake, tr = make_parts_base(tmp_path)
    cut = 3
    assert run(tmp_path, root, fake, KHMS_TRANSCRIPTS=tr, CUT_PART=cut, SWEEP_BUDGET_S=12).returncode == 1
    # a dry run shows the resume and changes nothing of the frozen list
    prep = root / "memory/inbox/.staging/.prepare-2099-01-02"
    frozen = {p.name: p.read_bytes() for p in prep.iterdir()}
    assert len([n for n in frozen if n.startswith("done-")]) == cut - 1, frozen
    dry = subprocess.run(["bash", os.path.join(TOOLS, "nightly_distill.sh"), "--dry-run"],
                         env=env_for(tmp_path, root, fake, KHMS_TRANSCRIPTS=tr, NIGHTLY_DATE="2099-01-03"),
                         capture_output=True, text=True, timeout=300)
    assert dry.returncode == 0 and "RESUMING the frozen list 2099-01-02" in dry.stdout, dry.stdout[-2000:]
    assert {p.name: p.read_bytes() for p in prep.iterdir()} == frozen



# Review FIX (Diblin, 2026-10-11), defect 1: the frozen record proved only the LIST of input paths, so a chunk whose
# BYTES changed at the same path was resumed against: the old part output was reused and the stamp advanced. The
# frozen record now carries the content sha256 of every frozen input; a mismatch sets the whole list aside and the
# window is collected again from the unmoved stamp (no part's old output is reused, nothing is skipped).
import pytest  # noqa: E402


def _frozen_inputs(root):
    staging = root / "memory/inbox/.staging"
    idx = staging / "nightly-2099-01-02-prepare-chunks.txt"
    return {"chunk": lambda: __import__("pathlib").Path(idx.read_text().splitlines()[0].split("\t")[1]),
            "digest": lambda: staging / "nightly-2099-01-02-digest.txt",
            "journals": lambda: staging / "nightly-2099-01-02-journals.md"}


@pytest.mark.parametrize("which", ["chunk", "digest", "journals"])
def test_a_frozen_input_whose_bytes_changed_at_the_same_path_is_set_aside_and_never_reused(tmp_path, which):
    root, fake, tr = make_parts_base(tmp_path)
    first = run(tmp_path, root, fake, KHMS_TRANSCRIPTS=tr, KILL_PART=2)       # part 1 complete, part 2 killed
    assert first.returncode == -9, (first.returncode, log_of(root)[-3000:])
    staging = root / "memory/inbox/.staging"
    prep = staging / ".prepare-2099-01-02"
    assert (prep / "done-001").exists()
    src = _frozen_inputs(root)[which]()
    src.write_text(src.read_text() + "\nCHANGED INPUT CONTENT AFTER CHECKPOINT\n")   # same path, other bytes
    mark = n_calls(tmp_path)
    second = run(tmp_path, root, fake, KHMS_TRANSCRIPTS=tr, NIGHTLY_DATE="2099-01-03")
    log = log_of(root)
    assert second.returncode == 0, log[-4000:]
    assert "frozen list 2099-01-02 is DAMAGED" in log and f"{src.name} changed" in log, log[-3000:]
    calls = sweep_calls(tmp_path, mark)
    assert [k for k, _n, _p in calls] == list(range(1, calls[0][1] + 1)), calls   # part 1 is NOT reused
    assert all("2099-01-03" in p for _k, _n, paths in calls for p in paths)
    assert list(staging.glob(".prepare-2099-01-02.damaged-*"))
    # the stamp is the re-collected window's end (this run's start), written only after its own parts ran
    stamp = (root / "state/shared/last-nightly").read_text().strip()
    assert re.search(re.escape(stamp) + r" nightly start \(night key 2099-01-03\)", log), (stamp, log[-2000:])
    assert "resuming the frozen prepare list" not in log and "the frozen list 2099-01-02 is complete" not in log


def test_a_journal_that_grew_after_the_freeze_is_not_damage(tmp_path):
    # positive control for the test above: the ledger grounding (journals-ledger.md) is refreshed on purpose
    # at every resume, so a live journal that grew must NOT set the frozen list aside
    root, fake, tr = make_parts_base(tmp_path)
    assert run(tmp_path, root, fake, KHMS_TRANSCRIPTS=tr, KILL_PART=2).returncode == -9
    (root / "journal").mkdir(exist_ok=True)
    with open(root / "journal" / "2099-01-02.md", "a", encoding="utf-8") as fh:
        fh.write("\n- a later line written after the freeze\n")
    mark = n_calls(tmp_path)
    second = run(tmp_path, root, fake, KHMS_TRANSCRIPTS=tr, NIGHTLY_DATE="2099-01-03")
    log = log_of(root)
    assert second.returncode == 0 and "DAMAGED" not in log, log[-3000:]
    calls = sweep_calls(tmp_path, mark)
    assert [k for k, _n, _p in calls][0] == 2 and all("2099-01-02" in p for _k, _n, ps in calls for p in ps)
