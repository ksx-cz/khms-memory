"""The morning review ping (khms_review_ping.sh) and its tmux delivery adapter, on throwaway bases.

What is under test is the CONTRACT a deployment configures: KHMS_PING_EXTRA (project alarm lines that ride in
the same one-line message; a failing command is an alarm itself, never a silence), KHMS_PING_SEND (stdin
delivery; rc 3 = recipient absent; any other failure is a failed run), the dry run that never delivers, and the
watchdogs on the night/week stamps and the failed-sweep file -- each in the resolver's layout (KHMS_SHARED_STATE).
"""
import os
import subprocess
import time

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
