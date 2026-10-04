"""Write policy, code-use lock and whole-night coverage of the triage stage and its morning tools.

  * a reader deployment (KHMS_READ_ONLY=1) cannot write into the base it reads through any of the morning tools
    (converter, apply preview, apply through an alias of the base, report), nor run the standalone triage;
    it may still write a detached report outside the base (positive controls);
  * the standalone triage holds the clone's code-use lock: an updater holding it exclusively stops it (rc 5)
    before any work, and a writer without an updater runs normally;
  * the triage has its own progress watermark: a full night hands it the window it has not covered yet (not the
    sweep's freshly advanced stamp), a failed triage keeps that window for the retry, a successful one advances
    only its own watermark, and a quiet night (no new distillation input) still runs a configured triage.

Isolated: a temporary base, a copy of the tools with fake judge/report children. No model, no network, no
scheduler.
"""
import fcntl
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

REPO = Path(__file__).resolve().parent.parent
TOOLS = REPO / "tools"


def module(name):
    spec = importlib.util.spec_from_file_location("_tb_" + name, REPO / "tests" / (name + ".py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


stage = module("test_triage_stage")
night = module("test_night_pipeline")
conv = module("test_convert_inbox2")


def base(tmp):
    root = tmp / "base"
    for d in ("memory/know", "memory/inbox", "memory/views", "state/shared", "journal"):
        (root / d).mkdir(parents=True, exist_ok=True)
    (root / "memory/views/tags.md").write_text(conv.TAGS)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("KHMS_", "NIGHTLY_", "TRIAGE_"))}
    env.update(KHMS_ROOT=str(root), KHMS_STATE=str(tmp / "agent"), KHMS_READ_ONLY="1",
               KHMS_NO_SNAPSHOT="1", PYTHONDONTWRITEBYTECODE="1")
    return root, env


def run(argv, env):
    return subprocess.run(argv, env=env, text=True, capture_output=True, timeout=60)


def tree(root):
    return sorted(str(p.relative_to(root)) for p in root.rglob("*"))


# --- F5: the write policy of the morning tools ------------------------------------------------------
@pytest.mark.parametrize("mode", ["converter", "apply_alias", "apply_preview", "report"])
def test_reader_new_tools_do_not_write_into_base(tmp_path, mode):
    root, env = base(tmp_path)
    source = root / "memory/inbox/2099-01-01.md"
    if mode == "converter":
        source.write_text(conv.HEADINGS)
        output = source.with_name("2099-01-01.approve.md")
        argv = [sys.executable, str(TOOLS / "convert_inbox2.py"), str(source), "--quiet"]
    elif mode.startswith("apply"):
        source.write_text("# triage\n\n## REVIEW VERDICT\n")
        output = source.with_name("2099-01-01.applied.md")
        argv = [sys.executable, str(TOOLS / "apply_triage.py"), str(source)]
        argv += ["--apply", "--brain", str(root) + "/."] if mode == "apply_alias" else ["--dry-run"]
    else:
        source.write_text('{"results": [], "meta": {}}')
        output = source.with_name("triage-report.md")
        argv = [sys.executable, str(TOOLS / "khms_triage_report.py"), str(source), "--no-subject-model",
                "--out", str(output)]
    before = tree(root)
    r = run(argv, env)
    assert not output.exists(), (mode, r.returncode, "reader wrote " + str(output))
    assert r.returncode == 4 and "read-only" in r.stderr, (mode, r.returncode, r.stderr)
    assert tree(root) == before, (mode, sorted(set(tree(root)) - set(before)))


@pytest.mark.parametrize("mode", ["converter", "apply_preview", "report"])
def test_reader_can_write_detached_report_outside_base(tmp_path, mode):
    root, env = base(tmp_path)
    source = tmp_path / "2099-01-01.md"
    if mode == "converter":
        source.write_text(conv.HEADINGS)
        output = tmp_path / "2099-01-01.approve.md"
        argv = [sys.executable, str(TOOLS / "convert_inbox2.py"), str(source), "--quiet"]
    elif mode == "apply_preview":
        source.write_text("# triage\n\n## REVIEW VERDICT\n")
        output = tmp_path / "2099-01-01.applied.md"
        argv = [sys.executable, str(TOOLS / "apply_triage.py"), str(source), "--dry-run"]
    else:
        source.write_text('{"results": [], "meta": {}}')
        output = tmp_path / "report.md"
        argv = [sys.executable, str(TOOLS / "khms_triage_report.py"), str(source), "--no-subject-model",
                "--out", str(output)]
    before = tree(root)
    r = run(argv, env)
    assert r.returncode == 0 and output.exists(), (r.returncode, r.stderr)
    assert tree(root) == before, sorted(set(tree(root)) - set(before))


def test_apply_through_an_alias_of_the_selected_base_on_a_writer_is_the_same_base(tmp_path):
    """--brain is compared by its real path, never as text: on a writer an alias of the selected base IS the
    selected base, so its review marker goes to the selected shared state, where the morning ping reads it."""
    root, env = base(tmp_path)
    shared = tmp_path / "shared"
    env.update(KHMS_READ_ONLY="0", KHMS_SHARED_STATE=str(shared))
    src = root / "memory/inbox/2099-01-01-triage.md"
    src.write_text("# triage\n\n## REVIEW VERDICT\n")
    r = run([sys.executable, str(TOOLS / "apply_triage.py"), str(src), "--apply", "--brain", str(root) + "/."],
            env)
    assert r.returncode == 0, (r.returncode, r.stderr)
    assert (shared / "reviewed/.reviewed-2099-01-01-triage").exists(), sorted(map(str, tmp_path.rglob("*")))
    assert not (root / "state/shared/reviewed").exists()


def test_reader_contradict_refuses_outputs_inside_the_base(tmp_path):
    """khms_contradict's own outputs (report, json checkpoint, spend ledger) are checked against the policy
    before the sweep starts, not left to an incidental reservation later."""
    root, env = base(tmp_path)
    before = tree(root)
    for flag in ("--out", "--json", "--spend-ledger"):
        target = root / "memory/inbox/.staging" / ("x" + flag.replace("-", "_"))
        r = run([sys.executable, str(TOOLS / "khms_contradict.py"), "--pairs-only", flag, str(target)], env)
        assert r.returncode == 4 and "read-only" in r.stderr, (flag, r.returncode, r.stderr)
    assert tree(root) == before, sorted(set(tree(root)) - set(before))


# --- F6: the standalone triage's policy and code-use lock ------------------------------------------
def stage_code(tmp):
    code = tmp / "clone/tools"
    shutil.copytree(TOOLS, code, ignore=shutil.ignore_patterns("__pycache__"))
    fake = ('import os, sys\nif "--since" in sys.argv:\n'
            ' open(os.environ["CAPTURE_SINCE"], "a").write(sys.argv[sys.argv.index("--since")+1] + "\\n")\n'
            + stage.FAKE_SWEEP)
    (code / "khms_contradict.py").write_text(fake)
    (code / "khms_triage_report.py").write_text(stage.FAKE_REPORT)
    return code


def stage_env(tmp, env):
    return dict(env, FAKE_CALLS=str(tmp / "calls"), FAKE_MODE="ok", CAPTURE_SINCE=str(tmp / "since"),
                NIGHTLY_DATE="2099-01-01")


@pytest.mark.parametrize("policy", ["reader", "maintenance"])
def test_standalone_triage_checks_policy_before_running(tmp_path, policy):
    root, env = base(tmp_path)
    code = stage_code(tmp_path)
    env = stage_env(tmp_path, env)
    if policy == "maintenance":
        env["KHMS_READ_ONLY"] = "0"
    before = tree(root)
    lock = open(code.parent / ".khms-code.lock", "w")
    if policy == "maintenance":
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        r = run(["bash", str(code / "nightly_triage.sh")], env)
    finally:
        lock.close()
    assert r.returncode == (4 if policy == "reader" else 5), (policy, r.returncode, r.stderr)
    assert not (tmp_path / "calls").exists()
    assert tree(root) == before, sorted(set(tree(root)) - set(before))


def test_standalone_writer_triage_without_maintenance_runs(tmp_path):
    root, env = base(tmp_path)
    code = stage_code(tmp_path)
    env = stage_env(tmp_path, env)
    env["KHMS_READ_ONLY"] = "0"
    r = run(["bash", str(code / "nightly_triage.sh")], env)
    assert r.returncode == 0 and (root / "memory/inbox/2099-01-01-triage.md").exists(), r.stderr


def test_a_running_standalone_triage_blocks_an_update(tmp_path):
    """The lock is HELD for the life of the stage, not only checked at its start: an updater that tries while the
    sweep runs is refused."""
    root, env = base(tmp_path)
    code = stage_code(tmp_path)
    env = stage_env(tmp_path, env)
    env["KHMS_READ_ONLY"] = "0"
    lockf = code.parent / ".khms-code.lock"
    probe = tmp_path / "probe"
    # the fake judge tries the exclusive (updater) lock while it runs, and records whether it got it
    (code / "khms_contradict.py").write_text(
        "import fcntl, os, sys\n"
        "if '--provenance' not in sys.argv:\n"
        f"    fh = open({str(lockf)!r}, 'a')\n"
        "    try:\n"
        "        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB); got = 'updater-got-it'\n"
        "    except BlockingIOError:\n"
        "        got = 'updater-refused'\n"
        f"    open({str(probe)!r}, 'w').write(got)\n"
        + stage.FAKE_SWEEP)
    r = run(["bash", str(code / "nightly_triage.sh")], env)
    assert r.returncode == 0, r.stderr
    assert probe.read_text() == "updater-refused"


# --- F7: the triage's own coverage window in the whole night ---------------------------------------
def night_env(tmp_path, root, fake, code, **extra):
    env = night.env_for(tmp_path, root, fake, KHMS_NIGHT_TRIAGE="bash " + str(code / "nightly_triage.sh"))
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    date = bindir / "date"                      # a weekday, so the incremental (--since) mode is used
    date.write_text('#!/bin/bash\nif [ "${1:-}" = +%u ]; then echo 1; else exec /usr/bin/date "$@"; fi\n')
    date.chmod(0o755)
    env.update(CAPTURE_SINCE=str(tmp_path / "since"), FAKE_MODE="ok", PATH=str(bindir) + ":" + env["PATH"])
    env.update({k: str(v) for k, v in extra.items()})
    return env


def sinces(tmp_path):
    p = tmp_path / "since"
    return p.read_text().split() if p.exists() else []


def test_full_night_passes_previous_window_to_triage(tmp_path):
    root, fake = night.make_base(tmp_path, n_marks=1)
    os.utime(root / "memory/know" / f"{night.CARD}.md", (1577836800, 1577836800))
    stamp = root / "state/shared/last-nightly"
    old = "2001-01-01T00:00:00+00:00"
    stamp.write_text(old + "\n")
    code = stage_code(tmp_path)
    r = run(["bash", str(TOOLS / "nightly_distill.sh")], night_env(tmp_path, root, fake, code))
    assert r.returncode == 0, r.stderr + night.log_of(root)
    assert sinces(tmp_path) == [old], ("triage skipped the previous window", old, sinces(tmp_path),
                                       night.log_of(root))
    # the night's own checkpoint moved; the triage's watermark moved only by its own success
    assert stamp.read_text().strip() != old
    assert (root / "state/shared/last-triage").read_text().strip() not in ("", old)


def test_a_failed_triage_keeps_its_window_and_the_retry_covers_it(tmp_path):
    root, fake = night.make_base(tmp_path, n_marks=1)
    stamp = root / "state/shared/last-nightly"
    old = "2001-01-01T00:00:00+00:00"
    stamp.write_text(old + "\n")
    code = stage_code(tmp_path)
    tstamp = root / "state/shared/last-triage"
    # night 1: the judge crashes and there is no local fallback -> the stage FAILS, the night goes on
    r = run(["bash", str(TOOLS / "nightly_distill.sh")], night_env(tmp_path, root, fake, code, FAKE_MODE="crash"))
    assert "triage stage FAILED" in night.log_of(root), night.log_of(root)
    assert sinces(tmp_path) == [old]
    assert stamp.read_text().strip() != old                 # the sweep's checkpoint advanced as before
    assert tstamp.read_text().strip() == old                 # the triage window is kept, not dropped
    # night 2 (the retry): nothing new to distil, the triage still runs and gets the SAME window
    (root / "journal/2099-01-01.md").unlink()
    r = run(["bash", str(TOOLS / "nightly_distill.sh")], night_env(tmp_path, root, fake, code))
    assert r.returncode == 0, r.stderr + night.log_of(root)
    assert sinces(tmp_path) == [old, old], sinces(tmp_path)
    first_ok = tstamp.read_text().strip()
    assert first_ok != old
    # night 3: the window now starts where the successful retry started
    r = run(["bash", str(TOOLS / "nightly_distill.sh")], night_env(tmp_path, root, fake, code))
    assert r.returncode == 0, r.stderr
    assert sinces(tmp_path) == [old, old, first_ok], sinces(tmp_path)


def test_a_triage_that_judged_nothing_does_not_advance_its_window(tmp_path):
    root, env = base(tmp_path)
    code = stage_code(tmp_path)
    env = stage_env(tmp_path, env)
    env.update(KHMS_READ_ONLY="0", FAKE_MODE="errors")                  # every pair LLM-ERROR, rc 0
    tstamp = root / "state/shared/last-triage"
    tstamp.write_text("2001-01-01T00:00:00+00:00\n")
    r = run(["bash", str(code / "nightly_triage.sh")], env)
    assert r.returncode == 0, r.stderr
    assert (root / "memory/inbox/2099-01-01-triage.md").exists()        # the honest morning file is written
    assert tstamp.read_text().strip() == "2001-01-01T00:00:00+00:00"    # ... but the window is not covered


def test_quiet_full_night_still_runs_configured_triage(tmp_path):
    root, fake = night.make_base(tmp_path, n_marks=0)
    (root / "journal/2099-01-01.md").unlink()
    code = stage_code(tmp_path)
    r = run(["bash", str(TOOLS / "nightly_distill.sh")], night_env(tmp_path, root, fake, code))
    assert r.returncode == 0, r.stderr
    assert (root / "memory/inbox/2099-01-02-triage.md").exists(), night.log_of(root)
