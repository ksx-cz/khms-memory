"""Daily catch-up and quota handling with isolated bases and a stub model."""
import os
import subprocess
import time
import shutil
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from test_night_pipeline import TOOLS, WFAKE, env_for, log_of, make_base


def setup_week(tmp_path, **extra):
    root, fake = make_base(tmp_path)
    # Keep the coverage transcript implementation of the existing stub.
    fake.write_text(WFAKE.replace('root, skip =', r'''stage = "part" if "WEEKLY PART " in prompt else "merge"
with open(os.environ["FAKE_CALLS"] + ".outputs", "a") as fh:
    fh.write(out + "\n")
mode = os.environ.get("FAKE_WEEK_MODE", "")
if mode == "limit-" + stage:
    open(out, "w").write("partial output must stay private")
    print("You've hit your weekly limit · resets tomorrow")
    sys.exit(int(os.environ.get("FAKE_LIMIT_RC", "1")))
if mode == "error-part":
    print("temporary server failure")
    sys.exit(1)
root, skip ='''))
    (root / "memory/inbox/2099-01-03.md").write_text("# source\nwidget wear line\n" * 2000)
    (root / "memory/views/condense-candidates.md").write_text("# none\n")
    env = env_for(tmp_path, root, fake, WEEKLY_DATE="2099-01-04",
                  WEEKLY_TRANSCRIPT_ROOT=tmp_path / "transcripts", WEEKLY_PART_ATTEMPTS=2,
                  WEEKLY_PART_CHUNKS=1, **extra)
    return root, env


def run(env):
    return subprocess.run(["bash", os.path.join(TOOLS, "weekly_synthesis.sh")], env=env,
                          capture_output=True, text=True, timeout=60)


def stamp(root, age):
    p = root / "state/shared/last-weekly"
    p.write_text("last successful run\n")
    os.utime(p, (time.time() - age,) * 2)
    return p


def calls(tmp_path):
    p = tmp_path / "calls.txt"
    return p.read_text().splitlines() if p.exists() else []


def test_recent_success_skips_before_model_and_preserves_output(tmp_path):
    root, env = setup_week(tmp_path)
    s = stamp(root, 6 * 86400)
    before = s.stat().st_mtime_ns
    out = root / "memory/inbox/2099-01-04-weekly.md"
    out.write_text("previous successful output")
    result = run(env)
    assert result.returncode == 0, result.stderr
    assert calls(tmp_path) == []
    assert "not due" in log_of(root)
    assert s.stat().st_mtime_ns == before
    assert out.read_text() == "previous successful output"


@pytest.mark.parametrize("age", [None, 7 * 86400 + 2])
def test_missing_or_expired_stamp_runs_and_next_daily_run_skips(tmp_path, age):
    root, env = setup_week(tmp_path)
    if age is not None:
        stamp(root, age)
    result = run(env)
    assert result.returncode == 0, log_of(root) + result.stderr
    first = calls(tmp_path)
    assert first[-1] == "merge opus"
    assert (root / "state/shared/last-weekly").exists()
    assert run(env).returncode == 0
    assert calls(tmp_path) == first


@pytest.mark.parametrize("stage", ["part", "merge"])
@pytest.mark.parametrize("model_rc", [0, 1])
def test_first_quota_message_stops_all_calls_and_preserves_stamp(tmp_path, stage, model_rc):
    root, env = setup_week(tmp_path, FAKE_WEEK_MODE="limit-" + stage, FAKE_LIMIT_RC=model_rc)
    s = stamp(root, 8 * 86400)
    before = (s.read_bytes(), s.stat().st_mtime_ns)
    result = run(env)
    assert result.returncode == 75, log_of(root) + result.stderr
    cs = calls(tmp_path)
    assert len([c for c in cs if c.startswith(stage)]) == 1, cs
    if stage == "part":
        assert len(cs) == 1, cs
    assert "USAGE LIMIT" in log_of(root)
    # Check where the stub actually wrote, not merely the final state after cleanup.
    outputs = (tmp_path / "calls.txt.outputs").read_text().splitlines()
    assert all("/.staging/" in p for p in outputs), outputs
    assert (s.read_bytes(), s.stat().st_mtime_ns) == before
    assert not (root / "memory/inbox/2099-01-04-weekly.md").exists()


def test_general_failure_still_retries_and_does_not_stamp(tmp_path):
    root, env = setup_week(tmp_path, FAKE_WEEK_MODE="error-part")
    result = run(env)
    assert result.returncode == 1, result.stderr
    assert len(calls(tmp_path)) == 2
    assert not (root / "state/shared/last-weekly").exists()
    assert not (root / "memory/inbox/2099-01-04-weekly.md").exists()


def test_due_is_checked_again_after_acquiring_weekly_lock(tmp_path):
    root, env = setup_week(tmp_path)
    stamp(root, 8 * 86400)
    # Simulate a previous holder completing between the initial check and acquisition.
    bindir = tmp_path / "bin"
    bindir.mkdir()
    real_flock = shutil.which("flock")
    wrapper = bindir / "flock"
    wrapper.write_text('#!/bin/bash\nif [ "$1" = "-n" ] && [ "$2" = "7" ]; then\n'
                       '  date -Is > "$RACE_STAMP"\nfi\nexec "' + real_flock + '" "$@"\n')
    wrapper.chmod(0o755)
    env.update(PATH=str(bindir) + os.pathsep + env["PATH"],
               RACE_STAMP=str(root / "state/shared/last-weekly"))
    result = run(env)
    assert result.returncode == 0, result.stderr
    assert calls(tmp_path) == []
    assert "not due" in log_of(root)
    assert "weekly start" not in log_of(root)


@pytest.mark.parametrize("skip", ["inboxes-part", "headers-part"])
def test_coverage_failure_still_retries_without_publishing(tmp_path, skip):
    root, env = setup_week(tmp_path, FAKE_SKIP_READ=skip)
    result = run(env)
    assert result.returncode == 1, result.stderr
    stage = "part" if skip == "inboxes-part" else "merge"
    assert len([c for c in calls(tmp_path) if c.startswith(stage)]) == 2
    assert "did not read all of its input" in log_of(root)
    assert not (root / "memory/inbox/2099-01-04-weekly.md").exists()
    assert not (root / "state/shared/last-weekly").exists()


def test_merge_quota_leaves_previous_final_output_untouched(tmp_path):
    root, env = setup_week(tmp_path, FAKE_WEEK_MODE="limit-merge")
    out = root / "memory/inbox/2099-01-04-weekly.md"
    out.write_text("previous completed proposal")
    assert run(env).returncode == 75
    assert out.read_text() == "previous completed proposal"


def simulated_clock(tmp_path, env, now):
    """Override only wall-clock date calls; preserve GNU date's timestamp parsing."""
    bindir = tmp_path / "clock-bin"
    bindir.mkdir(exist_ok=True)
    wrapper = bindir / "date"
    wrapper.write_text('#!/bin/bash\ncase "$*" in\n'
                       '  +%s|+%F|-Is|+%H%M%S) exec "' + shutil.which("date") +
                       '" -d "@$FAKE_NOW" "$@" ;;\n'
                       '  *) exec "' + shutil.which("date") + '" "$@" ;;\nesac\n')
    wrapper.chmod(0o755)
    env.update(PATH=str(bindir) + os.pathsep + os.environ["PATH"],
               TZ="Europe/Prague", FAKE_NOW=str(int(now.timestamp())))


@pytest.mark.parametrize("completed,now,due", [
    ("2026-10-01T23:59:00+02:00", "2026-10-08T00:01:00+02:00", True),
    ("2026-10-01T00:01:00+02:00", "2026-10-07T23:59:00+02:00", False),
    # Seven local dates span less than 168 hours across the spring DST change.
    ("2026-03-23T06:00:00+01:00", "2026-03-30T05:00:00+02:00", True),
    # The UTC date differs: the completion must be interpreted in scheduler TZ.
    ("2026-10-01T00:30:00+02:00", "2026-10-07T23:59:00+02:00", False),
    ("2026-10-19T06:00:00+02:00", "2026-10-26T05:00:00+01:00", True),
])
def test_calendar_due_boundaries(tmp_path, completed, now, due):
    root, env = setup_week(tmp_path)
    s = stamp(root, 0)
    completed = datetime.fromisoformat(completed)
    os.utime(s, (completed.timestamp(),) * 2)
    simulated_clock(tmp_path, env, datetime.fromisoformat(now))
    result = run(env)
    assert result.returncode == 0, log_of(root) + result.stderr
    assert bool(calls(tmp_path)) == due


@pytest.mark.parametrize("start", ["2026-03-01", "2026-10-01"])
def test_84_daily_completions_keep_seven_calendar_day_cycle(tmp_path, start):
    root, env = setup_week(tmp_path)
    zone = ZoneInfo("Europe/Prague")
    first = datetime.fromisoformat(start).replace(hour=4, tzinfo=zone)
    s = stamp(root, 0)
    os.utime(s, (first.timestamp(),) * 2)
    weekly_days = []
    false_alarms = 0
    # Night duration varies; weekly itself completes later than its due check.
    for day in range(1, 85):
        now = first + timedelta(days=day, minutes=(day * 37) % 180)
        simulated_clock(tmp_path, env, now)
        env["WEEKLY_DATE"] = now.date().isoformat()
        before = len(calls(tmp_path))
        result = run(env)
        assert result.returncode == 0, log_of(root) + result.stderr
        if len(calls(tmp_path)) > before:
            weekly_days.append(day)
            completed = now + timedelta(minutes=90)
            os.utime(s, (completed.timestamp(),) * 2)
        # The deployed stale threshold remains 7 elapsed days + 1 hour.
        false_alarms += now.timestamp() - s.stat().st_mtime > 7 * 86400 + 3600
    assert weekly_days == list(range(7, 85, 7))
    assert false_alarms == 0
