"""The morning review ping (khms_review_ping.sh) and its tmux delivery adapter, on throwaway bases.

What is under test is the CONTRACT a deployment configures: KHMS_PING_EXTRA (project alarm lines that ride in
the same one-line message; a failing command is an alarm itself, never a silence), KHMS_PING_SEND (stdin
delivery; rc 3 = recipient absent; any other failure is a failed run), the dry run that never delivers, and the
watchdogs on the night/week stamps and the failed-sweep file -- each in the resolver's layout (KHMS_SHARED_STATE).
"""
import os
import subprocess
import sys
import time
import shutil
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

TOOLS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools")
PING = os.path.join(TOOLS, "khms_review_ping.sh")
TMUXA = os.path.join(TOOLS, "khms_ping_tmux.sh")
TODAY = "2031-05-06"


@pytest.fixture
def base(tmp_path):
    root = tmp_path / "base"
    shared = root / "state" / "shared"
    (shared / "reviewed").mkdir(parents=True)
    (root / "memory" / "inbox").mkdir(parents=True)
    (shared / "last-nightly").write_text(TODAY + "T03:30:00+00:00\n")
    (shared / "last-weekly").write_text(TODAY + "T04:00:00+00:00\n")
    (shared / "reviewed" / (".reviewed-%s-triage" % TODAY)).write_text("")
    return root


def script(tmp_path, name, body):
    p = tmp_path / name
    p.write_text("#!/usr/bin/env bash\n" + body)
    p.chmod(0o755)
    return str(p)


def ping(tmp_path, root, *args, **env_extra):
    env = {k: v for k, v in os.environ.items() if not k.startswith("KHMS_")}
    env.update(KHMS_ROOT=str(root), KHMS_CONFIG_DIR=str(tmp_path / "cfg"), KHMS_STATE=str(tmp_path / "agent"),
               KHMS_PING_TODAY=TODAY, KHMS_PING_PGREP="no-such-sweep-process-xyz", **env_extra)
    return subprocess.run(["bash", PING, *args], env=env, capture_output=True, text=True, timeout=120)


def message(out):
    return [ln for ln in out.splitlines() if ln.startswith("[AUTO-PING]")]


def test_a_quiet_base_and_no_extra_is_silent(tmp_path, base):
    p = ping(tmp_path, base, KHMS_PING_DRYRUN="1")
    assert p.returncode == 0 and p.stdout == "", (p.stdout, p.stderr)


def test_extra_lines_ride_on_one_line_and_clean_extra_is_silent(tmp_path, base):
    two = script(tmp_path, "two.sh", "echo 'DEVICE: drift a.md'; echo; echo 'BOOTS: gap'\n")
    p = ping(tmp_path, base, KHMS_PING_DRYRUN="1", KHMS_PING_EXTRA=two)
    msg = message(p.stdout)
    assert len(msg) == 1 and "DEVICE: drift a.md  ||  BOOTS: gap" in msg[0], p.stdout
    assert "memory/inbox/" not in msg[0], msg[0]          # no proposals claimed on a clean inbox
    quiet = script(tmp_path, "quiet.sh", "exit 0\n")
    assert ping(tmp_path, base, KHMS_PING_DRYRUN="1", KHMS_PING_EXTRA=quiet).stdout == ""


def test_a_failing_extra_command_is_an_alarm_not_silence(tmp_path, base):
    bad = script(tmp_path, "bad.sh", "echo 'state unreadable' >&2; exit 7\n")
    p = ping(tmp_path, base, KHMS_PING_DRYRUN="1", KHMS_PING_EXTRA=bad)
    msg = message(p.stdout)
    assert len(msg) == 1 and "rc=7" in msg[0] and "state unreadable" in msg[0], p.stdout


def test_pending_proposals_and_extra_share_one_message(tmp_path, base):
    (base / "memory" / "inbox" / ("%s.md" % TODAY)).write_text("## Cards\n")
    one = script(tmp_path, "one.sh", "echo 'DEVICE: drift'\n")
    p = ping(tmp_path, base, KHMS_PING_DRYRUN="1", KHMS_PING_EXTRA=one)
    msg = message(p.stdout)
    assert len(msg) == 1 and "memory/inbox/" in msg[0] and msg[0].endswith("  ||  DEVICE: drift"), p.stdout
    assert "cards: PENDING" in msg[0], msg[0]


def test_the_watchdogs_read_the_shared_state_layout(tmp_path, base):
    shared = base / "state" / "shared"
    old = time.time() - 30 * 3600
    os.utime(shared / "last-nightly", (old, old))
    (shared / "last-weekly").unlink()
    (shared / "sweep-failed").write_text("2031-05-05T03:40 sweep part 1 FAILED\n2031-05-06T03:40 sweep part 1 FAILED\n")
    p = ping(tmp_path, base, KHMS_PING_DRYRUN="1")
    pend = [ln for ln in p.stdout.splitlines() if ln.startswith("DRY-RUN pending:")][0]
    assert "THE NIGHT RUN DID NOT FINISH" in pend and "FAILED 2 TIMES" in pend, pend
    assert "THE WEEKLY RUN DID NOT FINISH (last: never)" in pend, pend


def test_a_review_marker_in_shared_state_silences_a_legacy_date(tmp_path, base):
    (base / "memory" / "inbox" / "2031-05-01.md").write_text("## Cards\n")
    assert "2031-05-01" in ping(tmp_path, base, KHMS_PING_DRYRUN="1").stdout
    (base / "state" / "shared" / "reviewed" / ".reviewed-2031-05-01").write_text("")
    assert ping(tmp_path, base, KHMS_PING_DRYRUN="1").stdout == ""


def test_delivery_goes_through_the_configured_argv_on_stdin(tmp_path, base):
    got = tmp_path / "got.txt"
    send = script(tmp_path, "send.sh", 'cat > "%s"\n' % got)
    one = script(tmp_path, "one.sh", "echo 'DEVICE: drift'\n")
    p = ping(tmp_path, base, KHMS_PING_EXTRA=one, KHMS_PING_SEND=send)
    assert p.returncode == 0 and " pinged: " in p.stdout, (p.stdout, p.stderr)
    assert got.read_text().startswith("[AUTO-PING]") and got.read_text().count("\n") == 1


def test_dry_run_never_delivers(tmp_path, base):
    got = tmp_path / "got.txt"
    send = script(tmp_path, "send.sh", 'cat > "%s"\n' % got)
    one = script(tmp_path, "one.sh", "echo 'DEVICE: drift'\n")
    for kw in ({"KHMS_PING_DRYRUN": "1"}, {}):
        args = () if kw else ("--dry-run",)
        p = ping(tmp_path, base, *args, KHMS_PING_EXTRA=one, KHMS_PING_SEND=send, **kw)
        assert p.returncode == 0 and "DRY-RUN message:" in p.stdout
    assert not got.exists()


@pytest.mark.parametrize("rc,want_rc,word", [(3, 0, "not delivered"), (1, 1, "delivery FAILED rc=1")])
def test_an_absent_recipient_is_logged_and_a_failed_delivery_fails(tmp_path, base, rc, want_rc, word):
    send = script(tmp_path, "send.sh", "cat >/dev/null; exit %d\n" % rc)
    one = script(tmp_path, "one.sh", "echo 'DEVICE: drift'\n")
    p = ping(tmp_path, base, KHMS_PING_EXTRA=one, KHMS_PING_SEND=send)
    assert p.returncode == want_rc and word in p.stdout, (p.returncode, p.stdout)


def test_no_delivery_configured_prints_the_text(tmp_path, base):
    one = script(tmp_path, "one.sh", "echo 'DEVICE: drift'\n")
    p = ping(tmp_path, base, KHMS_PING_EXTRA=one)
    assert p.returncode == 0 and "delivery OFF" in p.stdout and len(message(p.stdout)) == 1


def _fake_tmux(tmp_path, has_session=True, still_visible=False):
    log = tmp_path / "tmux.log"
    return script(tmp_path, "tmux", r'''echo "$*" >> "%s"
case "$1" in
  has-session) exit %d ;;
  capture-pane) %s ;;
esac
exit 0
''' % (log, 0 if has_session else 1, "echo '[AUTO-PING] x'" if still_visible else "echo done")), log


@pytest.mark.parametrize("visible,enters", [(False, 1), (True, 2)])
def test_tmux_adapter_types_text_then_enter_and_retries_once(tmp_path, visible, enters):
    tm, log = _fake_tmux(tmp_path, still_visible=visible)
    p = subprocess.run(["bash", TMUXA, "agent"], input="[AUTO-PING] hello\nworld\n", text=True, capture_output=True,
                       env=dict(os.environ, KHMS_TMUX_BIN=tm, KHMS_TMUX_PAUSE_S="0"), timeout=60)
    assert p.returncode == 0, p.stderr
    calls = log.read_text().splitlines()
    assert calls[1] == "send-keys -t agent -l [AUTO-PING] hello world", calls     # one line, literal
    assert calls.count("send-keys -t agent Enter") == enters, calls


def test_tmux_adapter_reports_an_absent_session_as_rc3(tmp_path):
    tm, log = _fake_tmux(tmp_path, has_session=False)
    p = subprocess.run(["bash", TMUXA, "agent"], input="[AUTO-PING] x\n", text=True, capture_output=True,
                       env=dict(os.environ, KHMS_TMUX_BIN=tm), timeout=60)
    assert p.returncode == 3 and "send-keys" not in log.read_text()


def clock_ping(tmp_path, root, now):
    """Freeze wall-clock date while retaining date's timezone-aware epoch parser."""
    bindir = tmp_path / "clock-bin"
    bindir.mkdir(exist_ok=True)
    wrapper = bindir / "date"
    wrapper.write_text('#!/bin/bash\ncase "$*" in\n'
                       '  +%s|+%F|-Is) exec "' + shutil.which("date") +
                       '" -d "@$FAKE_NOW" "$@" ;;\n'
                       '  *) exec "' + shutil.which("date") + '" "$@" ;;\nesac\n')
    wrapper.chmod(0o755)
    # The old weekly watchdog uses find's clock instead of date; freeze that too
    # so RED proves its elapsed-hour policy, independent of today's real date.
    finder = bindir / "find"
    finder.write_text('#!/bin/bash\n'
                      'case "$1" in\n'
                      '  */last-weekly) epoch=$(stat -c %Y -- "$1"); '
                      'if (( (FAKE_NOW - epoch) / 60 > 10140 )); then echo "$1"; fi ;;\n'
                      '  *) exec "' + shutil.which("find") + '" "$@" ;;\nesac\n')
    finder.chmod(0o755)
    return ping(tmp_path, root, KHMS_PING_DRYRUN="1", TZ="Europe/Prague",
                FAKE_NOW=str(int(datetime.fromisoformat(now).timestamp())),
                PATH=str(bindir) + os.pathsep + os.environ["PATH"])


@pytest.mark.parametrize("completed,due_morning,due_end,missed_morning", [
    ("2026-03-23T06:00:00+01:00", "2026-03-30T05:00:00+02:00",
     "2026-03-30T23:59:59+02:00", "2026-03-31T00:00:00+02:00"),
    ("2026-10-19T06:00:00+02:00", "2026-10-26T06:30:00+01:00",
     "2026-10-26T23:59:59+01:00", "2026-10-27T00:00:00+01:00"),
    ("2026-10-01T00:30:00+02:00", "2026-10-08T06:30:00+02:00",
     "2026-10-08T23:59:59+02:00", "2026-10-09T00:00:00+02:00"),
])
def test_weekly_watchdog_allows_due_day_then_flags_failed_completion(
        tmp_path, base, completed, due_morning, due_end, missed_morning):
    weekly = base / "state/shared/last-weekly"
    # Contents deliberately disagree with mtime: only completion mtime owns the clock.
    weekly.write_text("2099-12-31T23:59:00+00:00\n")
    epoch = datetime.fromisoformat(completed).timestamp()
    os.utime(weekly, (epoch, epoch))
    before = (weekly.read_bytes(), weekly.stat().st_mtime_ns)
    for now in (due_morning, due_end):
        p = clock_ping(tmp_path, base, now)
        assert p.returncode == 0, p.stderr
        assert "THE WEEKLY RUN DID NOT FINISH" not in p.stdout, p.stdout
    p = clock_ping(tmp_path, base, missed_morning)
    assert p.returncode == 0, p.stderr
    assert "THE WEEKLY RUN DID NOT FINISH" in p.stdout, p.stdout
    assert (weekly.read_bytes(), weekly.stat().st_mtime_ns) == before
    # A successful due-day refresh clears the next morning's alarm across either DST.
    epoch = datetime.fromisoformat(due_end).timestamp()
    os.utime(weekly, (epoch, epoch))
    assert "THE WEEKLY RUN DID NOT FINISH" not in clock_ping(tmp_path, base, missed_morning).stdout


def test_nightly_watchdog_keeps_elapsed_policy(tmp_path, base):
    nightly = base / "state/shared/last-nightly"
    now = time.time()
    for age, missed in ((19 * 3600, False), (21 * 3600, True)):
        os.utime(nightly, (now - age,) * 2)
        p = clock_ping(tmp_path, base, "2026-10-26T06:30:00+01:00")
        assert p.returncode == 0, p.stderr
        assert ("THE NIGHT RUN DID NOT FINISH" in p.stdout) == missed, p.stdout


@pytest.mark.parametrize("start", ["2026-03-16", "2026-10-12"])
def test_repeated_weekly_success_has_no_dst_false_alarm(tmp_path, base, start):
    weekly = base / "state/shared/last-weekly"
    first = datetime.fromisoformat(start).replace(hour=6, tzinfo=ZoneInfo("Europe/Prague"))
    os.utime(weekly, (first.timestamp(),) * 2)
    for day in range(1, 29):
        # Ping before completion, whose time varies; refresh every seventh date.
        morning = first + timedelta(days=day, minutes=30)
        p = clock_ping(tmp_path, base, morning.isoformat())
        assert p.returncode == 0, p.stderr
        assert "THE WEEKLY RUN DID NOT FINISH" not in p.stdout, (day, p.stdout)
        if day % 7 == 0:
            completed = morning + timedelta(hours=1 + day % 3)
            os.utime(weekly, (completed.timestamp(),) * 2)


# --- #239: the ping names the live log, says WHY the sweep failed, and a paused weekly is paused ------------
def _pending(p):
    return [ln for ln in p.stdout.splitlines() if ln.startswith("DRY-RUN pending:")][0]


@pytest.mark.parametrize("reason,word", [("usage-limit", "usage limit"), ("failed", "will repeat")])
def test_a_failed_sweep_is_said_with_its_reason_and_the_live_log_path(tmp_path, base, reason, word):
    shared = base / "state" / "shared"
    (shared / "sweep-failed").write_text(f"2031-05-06T00:02 reason={reason} night=2031-05-06 parts=4 kept=0\n")
    p = ping(tmp_path, base, KHMS_PING_DRYRUN="1")
    pend = _pending(p)
    assert word in pend, pend
    assert ("usage limit" in pend) == (reason == "usage-limit"), pend
    # the log the night really writes (KHMS_NIGHT_LOG = KHMS_STATE/nightly.log), never a copied literal
    assert str(tmp_path / "agent" / "nightly.log") in pend and "logs/nightly.log" not in pend, pend


def test_a_paused_weekly_is_said_as_paused_not_as_failed(tmp_path, base):
    (base / "state" / "shared" / "last-weekly").unlink()
    p = ping(tmp_path, base, KHMS_PING_DRYRUN="1", KHMS_WEEK_PAUSED="paused by the principal 2031-05-01")
    pend = _pending(p)
    assert "THE WEEKLY RUN IS PAUSED (paused by the principal 2031-05-01; last: never)" in pend, pend
    assert "DID NOT FINISH" not in pend, pend


def test_a_message_that_spells_out_a_configured_path_fails_the_check(tmp_path):
    """The wording names the SETTING ({KHMS_NIGHT_LOG}); a copied path goes stale when the setting moves."""
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith("KHMS_")}
    env.update(KHMS_ROOT=str(tmp_path / "base"), KHMS_CONFIG_DIR=str(cfg))
    check = [sys.executable, os.path.join(TOOLS, "khms_messages.py"), "--check"]
    r = subprocess.run(check, env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and "LITERAL-PATH" not in r.stdout, r.stdout + r.stderr   # the shipped defaults
    (cfg / "messages.env").write_text(
        "PING_SWEEP_FAILED=failed {n}x ({first}; {last}) -- see logs/nightly.log\n"
        "HOOK_NIGHT_RUNNING=running since {start}{night}; it holds tools/.nightly.lock\n"
        "PING_WEEK_MISSED=weekly missed (last: {last}); see {KHMS_NIGHT_LOG}\n", encoding="utf-8")
    r = subprocess.run(check, env=env, capture_output=True, text=True, timeout=60)
    lit = [ln for ln in r.stdout.splitlines() if ln.startswith("LITERAL-PATH")]
    assert r.returncode == 1 and len(lit) == 2, r.stdout
    assert "PING_SWEEP_FAILED" in lit[0] + lit[1] and "HOOK_NIGHT_RUNNING" in lit[0] + lit[1], lit


# --- #243: the MARK stage's dollar cap is said in the morning -------------------------------------------------
CAPLINE = ("2031-05-06T04:10:00+02:00 permark: MARK stage stopped at the $ cap: $99.96 of $100.00, "
           "412 mark(s) left OPEN for the next night (reserve $0.1300 = the dearest mark so far)\n")


@pytest.mark.parametrize("last_night", [True, False])
def test_a_mark_stage_stopped_at_its_dollar_cap_is_said_only_for_the_last_night(tmp_path, base, last_night):
    log = tmp_path / "agent" / "nightly.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    older = "2031-05-05T03:30:00+02:00 nightly start (night key 2031-05-05)\n"
    last = "2031-05-06T03:30:00+02:00 nightly start (night key 2031-05-06)\n"
    log.write_text(older + (last + CAPLINE if last_night else CAPLINE + last)
                   + "2031-05-06T06:00:00+02:00 nightly done -> 1 inbox unit(s)\n")
    p = ping(tmp_path, base, KHMS_PING_DRYRUN="1")
    if not last_night:
        assert "$ CAP" not in p.stdout, p.stdout
        return
    pend = _pending(p)
    assert "THE MARK STAGE STOPPED AT ITS $ CAP ($99.96 of $100.00); 412 mark(s) stay OPEN" in pend, pend
    assert str(log) in pend, pend


def test_the_cap_message_takes_the_deployments_wording(tmp_path, base):
    log = tmp_path / "agent" / "nightly.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("2031-05-06T03:30:00+02:00 nightly start (night key 2031-05-06)\n" + CAPLINE)
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / "messages.env").write_text("PING_MARK_CAP=cap hit {spent}/{cap}, {n} open, see {KHMS_NIGHT_LOG}\n")
    pend = _pending(ping(tmp_path, base, KHMS_PING_DRYRUN="1"))
    assert f"cap hit $99.96/$100.00, 412 open, see {log}" in pend, pend
