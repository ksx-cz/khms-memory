"""The night finishes every night (#232): stage budgets of their own, no silently wrong agent CLI, served models said.

2026-10-12 (ISWEEP12): the sweep's deadline was counted from the night's start (03:30) while the MARK stage
before it used ~100 of its 150 minutes, so the sweep got ~47 min and stopped at part 6 of 31; and the agent CLI
was the bare name `claude`, resolved through PATH under systemd to a stale CLI whose `sonnet` alias served an
older, ~5x slower model -- two nights, and nothing in the log said which model ran.

Every acceptance below has a sibling that differs in the dimension the defect lives in:
  * a MARK stage LONGER than the sweep's budget must not stop the sweep (the deadline is the sweep's own),
    while a sweep part that runs past the sweep's own budget is still clipped;
  * the same for consolidation: batch 1 starts after a long MARK stage, a batch that runs past the
    consolidation's own budget stops the next one;
  * a bare agent name on PATH is refused unless KHMS_AGENT_FROM_PATH=1 allows it; an absolute path runs;
  * a real run logs the agent's path and --version once; every model call logs the model that served it
    (from the CLI's JSON result), UNKNOWN when there is no JSON; the morning ping says the LAST night's models.
"""
import json
import os
import re
import shutil
import subprocess
import sys

from test_night_pipeline import TOOLS, WFAKE, env_for, journal, log_of, make_base, run_week
from test_sweep_resume import PART_FAKE, make_parts_base

# The fake answers --version, can sleep in the MARK stage and in the FIRST consolidate call, and prints the
# CLI's JSON result (modelUsage) when it is asked for `--output-format json` and FAKE_SERVED names a model.
NF_FAKE = PART_FAKE.replace(
    "args = sys.argv[1:]\n",
    "args = sys.argv[1:]\n"
    "if args[:1] == ['--version']:\n"
    "    print('9.9.9 (fake agent)'); sys.exit(0)\n"
    "import atexit, time\n"
    "if '--output-format' in args and os.environ.get('FAKE_SERVED'):\n"
    "    atexit.register(lambda: print(json.dumps({'type': 'result', 'is_error': False, 'num_turns': 3,\n"
    "        'result': 'stage said done', 'total_cost_usd': 0.0123,\n"
    "        'modelUsage': {os.environ['FAKE_SERVED']: {'costUSD': 0.0123}}})))\n", 1).replace(
    "with open(os.environ[\"FAKE_CALLS\"], \"a\") as fh:\n    fh.write(stage",
    "if stage == 'marks' and os.environ.get('FAKE_MARK_SLEEP'):\n"
    "    time.sleep(float(os.environ['FAKE_MARK_SLEEP']))\n"
    "_slept = os.environ['FAKE_CALLS'] + '.cons-slept'\n"
    "if stage == 'consolidate' and os.environ.get('FAKE_CONS_SLEEP') and not os.path.exists(_slept):\n"
    "    open(_slept, 'w').close(); time.sleep(float(os.environ['FAKE_CONS_SLEEP']))\n"
    "with open(os.environ[\"FAKE_CALLS\"], \"a\") as fh:\n    fh.write(stage", 1)
assert NF_FAKE.count("FAKE_MARK_SLEEP") == 2 and "--version" in NF_FAKE


def base(tmp_path, n_marks=10):
    root, fake, tr = make_parts_base(tmp_path)
    fake.write_text(NF_FAKE)
    j = root / "journal" / "2099-01-01.md"
    t = j.stat().st_mtime
    j.write_text(journal(n_marks))
    os.utime(j, (t, t))
    return root, fake, tr


def run(tmp_path, root, fake, tr, **extra):
    extra.setdefault("MARK_CHUNK", "10")
    extra.setdefault("KHMS_TRANSCRIPTS", str(tr))
    return subprocess.run(["bash", os.path.join(TOOLS, "nightly_distill.sh")],
                          env=env_for(tmp_path, root, fake, **extra), capture_output=True, text=True,
                          timeout=300)


def calls(tmp_path, stage):
    p = tmp_path / "calls.txt"
    return [ln for ln in p.read_text().splitlines() if ln.split(" ")[0] == stage] if p.exists() else []


def n_parts(log):
    m = re.search(r"input list frozen in \S+ \((\d+) part\(s\)", log)
    assert m, log
    return int(m.group(1))


# --- 1. own stage budgets ---------------------------------------------------------------------------
def test_a_mark_stage_longer_than_the_sweep_budget_does_not_stop_the_sweep(tmp_path):
    root, fake, tr = base(tmp_path)
    r = run(tmp_path, root, fake, tr, FAKE_MARK_SLEEP="9", SWEEP_BUDGET_S="8")
    log = log_of(root)
    assert "NOT STARTED" not in log, log
    k = n_parts(log)
    assert len(calls(tmp_path, "sweep")) == k and k >= 1, (calls(tmp_path, "sweep"), log)
    assert "sweep OK" in log or "nightly done" in log, log
    assert r.returncode == 0, log


def test_a_sweep_part_is_still_clipped_to_the_sweeps_own_deadline(tmp_path):
    root, fake, tr = base(tmp_path)
    r = run(tmp_path, root, fake, tr, FAKE_MARK_SLEEP="2", SWEEP_BUDGET_S="5", CUT_PART="1")
    log = log_of(root)
    assert r.returncode == 1 and "sweep-1 FAILED" in log and "rc=124" in log, log
    assert "sweep part 1/" in log and "sweep part 1/1 NOT STARTED" not in log, log


def test_consolidation_counts_its_budget_from_its_own_start(tmp_path):
    # 10 marks in one MARK chunk -> 2 batches of 5 (plus the sweep's own); the MARK stage alone outlasts the
    # consolidation budget (so batch 1 must still start), batch 1 outlasts it (so batch 2 must not)
    root, fake, tr = base(tmp_path)
    run(tmp_path, root, fake, tr, FAKE_MARK_SLEEP="7", CARD_BUDGET_S="5", FAKE_CONS_SLEEP="6",
        CONSOLIDATE_BATCH="5")
    log = log_of(root)
    assert len(calls(tmp_path, "consolidate")) == 1, (calls(tmp_path, "consolidate"), log)
    assert re.search(r"consolidate budget of .+? reached — [1-9]\d* batch\(es\) stay", log), log


# --- 2. the agent CLI: never a silent PATH lookup, its version and the served models said ------------
def on_path(tmp_path, fake, name="fakeclaude"):
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    shutil.copy(fake, bindir / name)
    (bindir / name).chmod(0o755)
    return bindir / name, str(bindir) + os.pathsep + os.environ.get("PATH", "")


def test_a_bare_agent_name_on_path_is_refused_in_one_line(tmp_path):
    root, fake, tr = base(tmp_path, n_marks=3)
    _p, path = on_path(tmp_path, fake)
    r = run(tmp_path, root, fake, tr, KHMS_AGENT="fakeclaude", PATH=path)
    log = log_of(root)
    refusals = [ln for ln in log.splitlines() if "AGENT REFUSED" in ln]
    assert r.returncode == 1 and refusals, log
    assert "fakeclaude" in refusals[0] and "KHMS_AGENT_FROM_PATH=1" in refusals[0], refusals
    assert not (tmp_path / "calls.txt").exists(), "a refused agent was called"
    assert not (root / "state/shared/last-nightly").exists()


def test_a_bare_agent_name_runs_when_the_path_lookup_is_allowed_and_says_where_it_went(tmp_path):
    root, fake, tr = base(tmp_path, n_marks=3)
    found, path = on_path(tmp_path, fake)
    r = run(tmp_path, root, fake, tr, KHMS_AGENT="fakeclaude", PATH=path, KHMS_AGENT_FROM_PATH="1")
    log = log_of(root)
    assert r.returncode == 0, log
    agent = [ln for ln in log.splitlines() if re.search(r" agent: /", ln)]
    assert len(agent) == 1 and str(found) in agent[0] and "PATH" in agent[0], agent


def test_a_real_run_logs_the_agent_path_and_version_once(tmp_path):
    root, fake, tr = base(tmp_path, n_marks=3)
    r = run(tmp_path, root, fake, tr)
    log = log_of(root)
    assert r.returncode == 0, log
    agent = [ln for ln in log.splitlines() if re.search(r" agent: /", ln)]
    assert len(agent) == 1, agent
    assert str(fake) in agent[0] and "9.9.9 (fake agent)" in agent[0], agent
    assert len(calls(tmp_path, "sweep")) >= 1          # --version is not a stage call


def test_every_model_call_logs_the_model_that_served_it(tmp_path):
    root, fake, tr = base(tmp_path, n_marks=10)
    r = run(tmp_path, root, fake, tr, FAKE_SERVED="claude-fake-7", CONSOLIDATE_BATCH="5")
    log = log_of(root)
    assert r.returncode == 0, log
    served = re.findall(r"(?m) (\S+) served-by: (\S+)", log)
    stages = {s.rsplit("-", 1)[0] if re.search(r"-\d+$", s) else s for s, _m in served}
    assert {"marks", "sweep"} <= stages and any(s.startswith("consolidate") for s, _ in served), served
    assert all(m == "claude-fake-7" for _s, m in served), served
    n_model_calls = sum(len(calls(tmp_path, s)) for s in ("marks", "sweep", "consolidate"))
    assert len(served) == n_model_calls, (served, n_model_calls)
    assert "stage said done" in log                      # the stage's own answer is still in the log
    summary = [ln for ln in log.splitlines() if "models served this night:" in ln]
    assert len(summary) == 1 and "claude-fake-7" in summary[0], summary


def test_a_call_without_a_json_result_is_logged_as_unknown_not_skipped(tmp_path):
    root, fake, tr = base(tmp_path, n_marks=3)
    r = run(tmp_path, root, fake, tr)                     # FAKE_SERVED unset: the CLI printed no JSON
    log = log_of(root)
    assert r.returncode == 0, log
    served = re.findall(r"(?m) (\S+) served-by: (\S+)", log)
    assert served and all(m == "UNKNOWN" for _s, m in served), served


def test_the_weekly_refuses_a_bare_agent_name_too(tmp_path):
    root, _ = make_base(tmp_path)
    wf = tmp_path / "wsrc.py"
    wf.write_text(WFAKE)
    _p, path = on_path(tmp_path, wf, "weekagent")
    r = run_week(tmp_path, root, KHMS_AGENT="weekagent", PATH=path)
    log = log_of(root)
    assert r.returncode != 0 and "AGENT REFUSED" in log and "weekagent" in log, log
    assert not (tmp_path / "calls.txt").exists()


def test_the_triage_judge_does_not_resolve_a_bare_agent_name_through_path(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(TOOLS)
    for key in [k for k in os.environ if k.startswith("KHMS_")]:
        monkeypatch.delenv(key)
    monkeypatch.setenv("KHMS_ROOT", str(tmp_path / "nobase"))
    sys.modules.pop("khms_contradict", None)
    sys.modules.pop("khms_paths", None)
    import khms_contradict as C
    (tmp_path / "x.sh").write_text("#!/bin/sh\n")
    _found, path = on_path(tmp_path, tmp_path / "x.sh", "judgeagent")
    monkeypatch.setenv("PATH", path)
    b, why = C.resolve_agent({"KHMS_AGENT": "judgeagent"})
    assert b == "" and "AGENT REFUSED" in why and "KHMS_AGENT_FROM_PATH=1" in why, (b, why)
    b, why = C.resolve_agent({"KHMS_AGENT": "judgeagent", "KHMS_AGENT_FROM_PATH": "1"})
    assert b == str(tmp_path / "bin" / "judgeagent") and why == "", (b, why)
    b, why = C.resolve_agent({"KHMS_AGENT": str(tmp_path / "bin" / "judgeagent")})
    assert b == str(tmp_path / "bin" / "judgeagent") and why == "", (b, why)


# --- the morning ping says which models served the LAST night ---------------------------------------
def test_the_review_ping_names_the_models_of_the_last_night_only(tmp_path):
    root = tmp_path / "base"
    shared = root / "state" / "shared"
    (shared / "reviewed").mkdir(parents=True)
    (root / "memory" / "inbox").mkdir(parents=True)
    (shared / "last-nightly").write_text("2031-05-06T03:30:00+00:00\n")
    (shared / "last-weekly").write_text("2031-05-06T04:00:00+00:00\n")
    (root / "memory" / "inbox" / "2031-05-06-b01.md").write_text("## Cards\n\n### C1: x\nbody\n")
    agent_state = tmp_path / "agent"
    agent_state.mkdir()
    (agent_state / "nightly.log").write_text(
        "2031-05-05T03:30:00+00:00 nightly start (night key 2031-05-05)\n"
        "2031-05-05T03:40:00+00:00 sweep-1 served-by: claude-old-1 (attempt 1/3, rc=0)\n"
        "2031-05-06T03:30:00+00:00 nightly start (night key 2031-05-06)\n"
        "2031-05-06T03:31:00+00:00 agent: /opt/cli/claude (KHMS_AGENT, -> /opt/cli/v/9.9.9) version: 9.9.9 (Claude Code)\n"
        "2031-05-06T03:40:00+00:00 marks-1 served-by: claude-new-2 (attempt 1/2, rc=0)\n"
        "2031-05-06T04:40:00+00:00 sweep-1 served-by: claude-new-2 (attempt 1/3, rc=0)\n"
        "2031-05-06T04:41:00+00:00 sweep-2 served-by: UNKNOWN (attempt 1/3, rc=124, no JSON result)\n"
        "judge usage: 4 calls (0 failed) · in 1 · out 2 (thinking 0) · $0.0100 (CLI total_cost_usd) · models ['claude-judge-3']\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith("KHMS_")}
    env.update(KHMS_ROOT=str(root), KHMS_CONFIG_DIR=str(tmp_path / "cfg"), KHMS_STATE=str(agent_state),
               KHMS_PING_TODAY="2031-05-06", KHMS_PING_PGREP="no-such-sweep-process-xyz", KHMS_PING_DRYRUN="1")
    p = subprocess.run(["bash", os.path.join(TOOLS, "khms_review_ping.sh")], env=env, capture_output=True,
                       text=True, timeout=120)
    msg = [ln for ln in p.stdout.splitlines() if ln.startswith("[AUTO-PING]")]
    assert p.returncode == 0 and len(msg) == 1, (p.stdout, p.stderr)
    assert "claude-new-2" in msg[0] and "claude-judge-3" in msg[0] and "UNKNOWN" in msg[0], msg
    assert "9.9.9" in msg[0], msg
    assert "claude-old-1" not in msg[0], msg
