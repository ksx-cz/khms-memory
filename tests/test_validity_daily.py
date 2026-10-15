"""The daily card-validity judge (khms_validity_daily.sh) and its line in the morning ping, on throwaway bases.

What is under test is the CONTRACT of the unattended job: one run judges a draw past the ledger with the
configured agent CLI (here a fake one that answers from the prompt and counts its calls), writes the proposal
rows round-robin into part files and ONE status line into the night log; a second run the same day judges
nothing; a batch whose parts were never applied (no APPLIED marker) stops the next one, said in the log
instead of piling batches up; an empty eligible pool is said as DONE. The ping repeats the last status line
and names every batch whose parts wait for verification.
"""
import json
import os
import subprocess
import sys

import pytest

TOOLS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools")
DAILY = os.path.join(TOOLS, "khms_validity_daily.sh")
PING = os.path.join(TOOLS, "khms_review_ping.sh")
TODAY = "2031-05-06"
YESTERDAY = "2031-05-05"

# The fake judge: reads the prompt on stdin, answers a schema-valid cannot-check-offline verdict for the card
# the prompt names (no evidence needed for that verdict), and appends the card id to a call log.
FAKE_JUDGE = r'''#!/usr/bin/env python3
import json, os, re, sys
prompt = sys.stdin.read()
cid = re.search(r"THE CARD (K-\d+)", prompt).group(1)
with open(os.environ["FAKE_JUDGE_LOG"], "a") as fh:
    fh.write(cid + "\n")
ans = {"card_id": cid, "verdict": "cannot-check-offline", "false_sentence": "", "suggested_path": "none",
       "knowhow": "yes", "knowhow_reason": "a lesson",
       "subclaims": [{"claim": "the claim", "verdict": "cannot-check-offline", "evidence": [],
                      "reason": "no file in the folder can show it"}]}
print(json.dumps({"result": json.dumps(ans), "total_cost_usd": 0.01, "num_turns": 1, "is_error": False}))
'''


def card(cid, status="active"):
    return (f"---\nid: {cid}\ntype: fact\nlevel: observation\nstatus: {status}\ntags: [testing]\n"
            f"scope: universal\nevidence: measured\nsource: test\ndate: 2031-01-01\nlinks:\n  derived_from: []\n"
            f"  supports: []\n  contradicts: []\n  refuted_by: []\n  supersedes: null\n---\n"
            f"the helper `tool_{cid[2:]}.py` prints the verdict line\n\nBody of {cid}.\n")


@pytest.fixture
def env(tmp_path):
    root = tmp_path / "base"
    know = root / "memory" / "know"
    know.mkdir(parents=True)
    for i in range(1, 7):
        (know / f"K-0000{i}.md").write_text(card(f"K-0000{i}"))
    (know / "K-00009.md").write_text(card("K-00009", status="superseded"))     # never drawn
    (root / "state" / "shared").mkdir(parents=True)
    tree = tmp_path / "proj"
    (tree / "lib").mkdir(parents=True)
    (tree / "lib" / "tool_00001.py").write_text("print('VERDICT')\n")
    judge = tmp_path / "fake-claude"
    judge.write_text(FAKE_JUDGE)
    judge.chmod(0o755)
    e = {k: v for k, v in os.environ.items() if not k.startswith("KHMS_")}
    e.update(KHMS_ROOT=str(root), KHMS_CONFIG_DIR=str(tmp_path / "cfg"), KHMS_STATE=str(tmp_path / "agent"),
             KHMS_RUNTIME=str(tmp_path / "runtime"), KHMS_AGENT=str(judge),
             KHMS_VALIDITY_TREES=f"proj=dir:{tree}", KHMS_VALIDITY_TODAY=TODAY,
             FAKE_JUDGE_LOG=str(tmp_path / "judge-calls.log"))
    return {"root": root, "tmp": tmp_path, "env": e, "daily": root / "plans" / "card-validity" / "daily",
            "log": tmp_path / "agent" / "nightly.log", "calls": tmp_path / "judge-calls.log",
            "ledger": root / "memory" / "ledger" / "validity.jsonl"}


def run(env, *args, **extra):
    e = dict(env["env"], **extra)
    return subprocess.run(["bash", DAILY, *args], env=e, capture_output=True, text=True, timeout=300)


def validity_lines(env):
    if not env["log"].exists():
        return []
    return [ln for ln in env["log"].read_text().splitlines() if " VALIDITY " in ln]


def calls(env):
    return env["calls"].read_text().split() if env["calls"].exists() else []


def table_ids(path):
    return [ln.split("|")[1].strip() for ln in path.read_text().splitlines() if ln.startswith("| K-")]


def test_a_run_judges_writes_four_round_robin_parts_and_one_status_line(env):
    p = run(env, "--n", "5")
    assert p.returncode == 0, p.stdout + p.stderr
    out = env["daily"] / TODAY
    full = table_ids(out / "proposals.md")
    assert len(full) == 5 and len(calls(env)) == 5, (full, calls(env))
    parts = [out / f"proposals-part{i}.md" for i in range(1, 5)]
    assert all(x.exists() for x in parts), sorted(os.listdir(out))
    assert not (out / "proposals-part5.md").exists()
    # round-robin: row i of the full table goes to part (i mod 4) + 1, each part keeps the header and the
    # verdict line, and carries the command block of its own rows only
    assert [table_ids(x) for x in parts] == [full[0::4], full[1::4], full[2::4], full[3::4]]
    p1 = parts[0].read_text()
    assert "VERDICTS holds=0" in p1 and f"### {full[0]} -- none" in p1 and f"### {full[1]} " not in p1
    lines = validity_lines(env)
    assert len(lines) == 1, lines
    assert (f"VALIDITY {TODAY}: sampled=5 holds=0 no-longer-holds=0 cannot-check=5 unverified=0 knowhow-no=0 "
            f"cost=$0.0500 parts=4 rows=5") in lines[0], lines[0]
    # the judged cards entered the ledger; the sandbox is gone; the seed is the date
    led = [json.loads(x) for x in env["ledger"].read_text().splitlines()]
    assert sorted(r["id"] for r in led) == sorted(full)
    assert json.loads((out / "sample.json").read_text())["seed"] == 20310506
    assert not (env["tmp"] / "runtime" / "validity-sandbox").exists()


def test_a_second_run_the_same_day_judges_nothing(env):
    assert run(env, "--n", "2").returncode == 0
    before = calls(env)
    p = run(env, "--n", "2")
    assert p.returncode == 0, p.stdout + p.stderr
    assert "already ran" in p.stdout, p.stdout
    assert calls(env) == before and len(validity_lines(env)) == 1


def test_a_run_while_another_holds_the_lock_is_refused(env):
    lock = env["tmp"] / "runtime" / "validity-daily.lock"
    lock.parent.mkdir(parents=True)
    holder = subprocess.Popen(["flock", str(lock), "sleep", "30"])
    try:
        for _ in range(100):
            if subprocess.run(["flock", "-n", str(lock), "true"]).returncode != 0:
                break
            subprocess.run(["sleep", "0.05"])
        p = run(env, "--n", "2")
    finally:
        holder.kill()
        holder.wait()
    assert p.returncode == 75, p.stdout + p.stderr
    assert calls(env) == [] and validity_lines(env) == []


def test_an_unapplied_batch_stops_the_next_one_and_says_so(env):
    prev = env["daily"] / YESTERDAY
    prev.mkdir(parents=True)
    (prev / "proposals-part1.md").write_text("| K-00001 | x |\n")
    p = run(env, "--n", "2")
    assert p.returncode == 0, p.stdout + p.stderr
    assert calls(env) == [], "judged although a batch waits"
    lines = validity_lines(env)
    assert len(lines) == 1 and f"VALIDITY {TODAY}: WAITING" in lines[0] and YESTERDAY in lines[0], lines
    assert not (env["daily"] / TODAY / "sample.json").exists()
    # once the coordinator marks it applied, the next run judges
    (prev / "APPLIED").write_text("applied\n")
    p = run(env, "--n", "2")
    assert p.returncode == 0 and len(calls(env)) == 2, p.stdout + p.stderr


def test_no_eligible_card_left_is_said_as_done(env):
    rows = []
    for i in range(1, 7):
        import hashlib
        raw = (env["root"] / "memory" / "know" / f"K-0000{i}.md").read_bytes()
        rows.append(json.dumps({"id": f"K-0000{i}", "sha256": hashlib.sha256(raw).hexdigest()}))
    env["ledger"].parent.mkdir(parents=True)
    env["ledger"].write_text("\n".join(rows) + "\n")
    p = run(env, "--n", "5")
    assert p.returncode == 0, p.stdout + p.stderr
    assert calls(env) == []
    lines = validity_lines(env)
    assert len(lines) == 1 and f"VALIDITY {TODAY}: DONE — no eligible cards left" in lines[0], lines


def test_a_failure_is_a_failed_line_and_a_nonzero_exit(env):
    p = run(env, "--n", "2", KHMS_VALIDITY_TREES="")
    assert p.returncode != 0
    lines = validity_lines(env)
    assert len(lines) == 1 and f"VALIDITY {TODAY}: FAILED" in lines[0] and "KHMS_VALIDITY_TREES" in lines[0]


def test_out_root_and_log_redirect_a_trial_run(env):
    trial = env["tmp"] / "trial"
    log = env["tmp"] / "trial.log"
    p = run(env, "--n", "3", "--out-root", str(trial), "--log", str(log))
    assert p.returncode == 0, p.stdout + p.stderr
    assert (trial / TODAY / "proposals-part3.md").exists() and not env["daily"].exists()
    assert "parts=3 rows=3" in log.read_text() and validity_lines(env) == []


# --- the morning ping -------------------------------------------------------------------------------------
def ping(env):
    e = dict(env["env"], KHMS_PING_TODAY=TODAY, KHMS_PING_PGREP="no-such-sweep-process-xyz", KHMS_PING_DRYRUN="1")
    shared = env["root"] / "state" / "shared"
    (shared / "last-nightly").write_text(TODAY + "T03:30:00+00:00\n")
    (shared / "last-weekly").write_text(TODAY + "T04:00:00+00:00\n")
    (env["root"] / "memory" / "inbox").mkdir(parents=True, exist_ok=True)
    return subprocess.run(["bash", PING], env=e, capture_output=True, text=True, timeout=120)


def test_the_ping_names_a_waiting_batch_and_the_last_status_line(env):
    batch = env["daily"] / YESTERDAY
    batch.mkdir(parents=True)
    for i in range(1, 5):
        (batch / f"proposals-part{i}.md").write_text("x\n")
    done = env["daily"] / "2031-05-01"          # an applied batch is not named
    done.mkdir()
    (done / "proposals-part1.md").write_text("x\n")
    (done / "APPLIED").write_text("")
    env["log"].parent.mkdir(parents=True)
    env["log"].write_text(f"2031-05-04T05:31:00+02:00 VALIDITY 2031-05-04: sampled=500 old\n"
                          f"2031-05-05T06:01:00+02:00 VALIDITY {YESTERDAY}: sampled=500 holds=300 parts=4 rows=190\n")
    p = ping(env)
    assert p.returncode == 0, p.stdout + p.stderr
    pend = [ln for ln in p.stdout.splitlines() if ln.startswith("DRY-RUN pending:")][0]
    assert f"validity batch {YESTERDAY}: 4 parts ready for verification" in pend, pend
    assert "2031-05-01" not in pend, pend
    msg = p.stdout.split("DRY-RUN message:", 1)[1]
    assert f"VALIDITY {YESTERDAY}: sampled=500 holds=300 parts=4 rows=190" in msg, msg
    assert "VALIDITY 2031-05-04" not in msg, msg


def test_a_failed_validity_run_is_an_alarm_on_its_own(env):
    env["log"].parent.mkdir(parents=True)
    env["log"].write_text(f"2031-05-06T05:31:00+02:00 VALIDITY {TODAY}: FAILED run rc=1\n")
    p = ping(env)
    pend = [ln for ln in p.stdout.splitlines() if ln.startswith("DRY-RUN pending:")][0]
    assert f"VALIDITY {TODAY}: FAILED run rc=1" in pend, p.stdout


def test_a_quiet_validity_state_adds_nothing(env):
    p = ping(env)
    assert p.returncode == 0 and p.stdout == "", p.stdout
