"""The night pipeline on fixed synthetic fixtures: coverage, carry, failure handling, code-use lock, prompts.

Every run is a throwaway base (tmp_path) with a fake agent CLI that answers each stage by the shape of its
prompt -- no model, no network, no real transcripts. The fixtures vary the dimension each property lives in:
more MARKs than any former cap, a consolidate batch that fails, a sweep that fails, an agent that is missing.
"""
import json
import os
import re
import subprocess
import sys
import time

import pytest

TOOLS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools")
sys.path.insert(0, TOOLS)

CARD = "K-%05d" % 11          # an id a fresh base would allocate; built, never written as a literal
KINDS = ["decision", "finding", "requirement", "gotcha", "error", "khms-issue", "measurement", "refuted"]

FAKE = r'''#!/usr/bin/env python3
# Fake agent CLI: sweep / MARK stage / consolidate, by the shape of the prompt (`-p <prompt>`).
import json, os, re, sys
args = sys.argv[1:]
if args[:1] == ["--version"]:        # a real run logs the agent's version once (#232); not a stage call
    print("0.0.0 (fake agent)"); sys.exit(0)
prompt = args[args.index("-p") + 1] if "-p" in args else ""
def field(rx):
    m = re.findall(rx, prompt, re.M)
    return m[-1].strip() if m else ""
out = field(r"^OUTPUT FILE: (.+)$")
stage = ("marks" if "MARK QUEUE chunk:" in prompt else
         "consolidate" if "sweep candidates" in prompt else "sweep")
with open(os.environ["FAKE_CALLS"], "a") as fh:
    fh.write(stage + " " + os.path.basename(out) + "\n")
with open(os.environ["FAKE_CALLS"] + ".jsonl", "a") as fh:
    fh.write(json.dumps({"stage": stage, "prompt": prompt, "args": args, "pid": os.getpid()}) + "\n")
if os.environ.get("FAKE_FAIL") == stage:
    sys.exit(1)
def card(label, text, extra=""):
    return (f"### {label}: {text[:40]}\nWHAT: {text}\n{extra}RELATION: supports CARDID\n"
            f"NEAREST: CARDID (0.0300)\n**QUOTES:**\n- src=journal :: {text}\n\n")
if stage == "marks":
    body = "# marks\n"
    dropped = []
    for i, line in enumerate(open(field(r"^- MARK QUEUE chunk: (.+)$")), 1):
        m = re.match(r"^- (M-[0-9a-f]{8}) \|[^|]*\|[^|]*\|[^|]*\| (.*)$", line)
        if m and "status line" in m.group(2):
            dropped.append(f"- {m.group(1)} :: not know-how: status line")
        elif m:
            body += card(f"M{i}", m.group(2), f"MARKS: {m.group(1)}\n")
    body += "## MARKS NOT CARDED\n" + "\n".join(dropped) + "\n"
    open(out, "w").write(body)
elif stage == "consolidate":
    text = open(field(r"^- sweep candidates[^:]*: (.+)$")).read()
    cards = [re.split(r"(?m)^## ", b)[0] for b in re.split(r"(?m)^(?=#{2,3} +[A-Z]\d+)", text)
             if re.match(r"#{2,3} +[A-Z]\d+", b)]
    open(out, "w").write("## Cards\n\n" + "".join(c.rstrip("\n") + "\n\n" for c in cards))
else:
    js = field(r"^- journal[^:]*: (.+)$")
    body, n = "# nightly sweep - candidates\n", 0
    for line in open(js) if js and os.path.exists(js) else []:
        m = re.search(r"MARK\[[^\]]+\]\s*(.*)$", line)
        if not m or "status line" in line:
            continue
        n += 1
        body += card(f"C{n}", m.group(1).strip())
    open(out, "w").write(body)
'''.replace("CARDID", CARD)


def journal(n):
    lines = ["# journal 2099-01-01 (synthetic)", ""]
    for i in range(1, n + 1):
        lines.append(f"- {10 + i // 60:02d}:{i % 60:02d} MARK[{KINDS[i % len(KINDS)]}] "
                     f"fixture fact number {i:03d}: widget {i} measured at {i * 7} units")
    lines.append("- 23:00 MARK[done] status line, not knowledge")
    return "\n".join(lines) + "\n"


def make_base(tmp_path, n_marks=30):
    root = tmp_path / "base"
    for d in ("journal", "memory/know", "memory/inbox", "memory/views", "state/shared"):
        (root / d).mkdir(parents=True, exist_ok=True)
    (root / "memory/know" / f"{CARD}.md").write_text(
        f"---\nid: {CARD}\ntype: fact\nlevel: observation\nstatus: active\ntags: [widget]\n"
        f"date: '2099-01-01'\n---\nWidgets are measured in units.\n")
    j = root / "journal" / "2099-01-01.md"
    j.write_text(journal(n_marks))
    t = time.time() + 3600                       # newer than any stamp the test writes
    os.utime(j, (t, t))
    fake = tmp_path / "fake_agent.py"
    fake.write_text(FAKE)
    fake.chmod(0o755)
    return root, fake


def env_for(tmp_path, root, fake, **extra):
    e = {k: v for k, v in os.environ.items() if not k.startswith(("KHMS_", "NIGHTLY_", "MARK_LEDGER"))}
    e.update({"KHMS_ROOT": str(root), "KHMS_AGENT": str(fake), "FAKE_CALLS": str(tmp_path / "calls.txt"),
              "NIGHTLY_DATE": "2099-01-02", "SWEEP_TIMEOUT": "60s", "CONSOLIDATE_TIMEOUT": "60s",
              "MARK_TIMEOUT": "60s", "RETRY_BASE_DELAY_S": "0", "BATCH_RETRY_MAX": "1",
              "RETRY_MAX_ATTEMPTS": "1", "KHMS_AGENT_TRANSCRIPTS": str(tmp_path / "transcripts")})
    e.update({k: str(v) for k, v in extra.items()})
    return e


def run_night(tmp_path, root, fake, *args, **extra):
    return subprocess.run(["bash", os.path.join(TOOLS, "nightly_distill.sh"), *args],
                          env=env_for(tmp_path, root, fake, **extra), capture_output=True, text=True,
                          timeout=300)


def log_of(root):
    p = root / "state" / "nightly.log"
    return p.read_text() if p.exists() else ""


def units(root):
    return sorted(p for p in (root / "memory/inbox").iterdir() if re.match(r"^\d{4}-\d{2}-\d{2}(-\d+)?(-m\d+)?-b\d+\.md$", p.name))


def cards_in(files):
    n = 0
    for f in files:
        text = f.read_text()
        m = re.search(r"(?m)^## (DEFERRED|DROPPED|MARKS NOT CARDED)", text)
        n += len(re.findall(r"(?m)^#{2,3} +[A-Z]\d+", text[:m.start()] if m else text))
    return n


# --- coverage --------------------------------------------------------------------------------------
def test_every_mark_reaches_the_inbox_and_the_ledger_with_no_cap(tmp_path):
    root, fake = make_base(tmp_path, n_marks=30)
    r = run_night(tmp_path, root, fake, CONSOLIDATE_BATCH="7")
    log = log_of(root)
    assert r.returncode == 0, log + r.stderr
    us = units(root)
    assert len(us) == 6 and cards_in(us) == 30, (us, log)          # 30 candidates in batches of 7
    assert "DEFERRED" not in "".join(u.read_text() for u in us)
    marks = [json.loads(ln) for ln in open(root / "memory/ledger/marks.jsonl")]
    assert len(marks) == 31 and {m["kind"] for m in marks} == set(KINDS) | {"done"}
    assert all(m["state"] == "OPEN" and m.get("proposed_in") for m in marks if m["kind"] != "done"), marks[:2]
    assert (root / "state/shared/last-nightly").read_text().strip(), "the window was not stamped"
    assert "nightly done" in log and "INCOMPLETE" not in log


def test_unconfigured_sources_and_triage_are_said_not_guessed(tmp_path):
    root, fake = make_base(tmp_path, n_marks=2)
    assert run_night(tmp_path, root, fake).returncode == 0
    log = log_of(root)
    for line in ("source transcripts: OFF", "source git: OFF", "source chat: OFF",
                 "principal-quote check: OFF", "triage stage: OFF"):
        assert line in log, (line, log)


def test_configured_sources_are_read(tmp_path):
    root, fake = make_base(tmp_path, n_marks=2)
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "a.txt").write_text("x\n")
    subprocess.run(["git", "-C", str(repo), "add", "a.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t",
                    "commit", "-qm", "widget calibration committed"], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t",
                    "commit", "--allow-empty", "-qm", "MARK[finding] secret marked commit"], check=True)
    tr = tmp_path / "transcripts-src" / "proj-widgets"
    tr.mkdir(parents=True)
    (tr / "s1.jsonl").write_text(json.dumps({"type": "user", "timestamp": "2099-01-01T10:00:00Z",
                                             "message": {"role": "user", "content": "calibrate widget 9"}}) + "\n")
    os.utime(tr / "s1.jsonl", (time.time() + 3600,) * 2)
    triage = tmp_path / "triage.sh"
    triage.write_text("#!/bin/sh\necho triage-ran \"$@\"\n")
    triage.chmod(0o755)
    r = run_night(tmp_path, root, fake, KHMS_GIT_REPOS=str(repo), KHMS_TRANSCRIPTS=str(tmp_path / "transcripts-src"),
                  KHMS_PROJECT_SLUG="widgets", KHMS_NIGHT_TRIAGE=f"{triage} --limit 0")
    assert r.returncode == 0, log_of(root)
    staging = root / "memory/inbox/.staging"
    assert "widget calibration committed" in (staging / "nightly-2099-01-02-git.txt").read_text()
    assert "secret marked commit" not in (staging / "nightly-2099-01-02-git.txt").read_text()
    assert "calibrate widget 9" in (staging / "nightly-2099-01-02-digest.txt").read_text()
    assert "triage-ran --limit 0" in log_of(root)


# --- carry and failure handling ---------------------------------------------------------------------
def test_a_failed_batch_is_carried_reported_and_finished_the_next_night(tmp_path):
    root, fake = make_base(tmp_path, n_marks=10)
    r = run_night(tmp_path, root, fake, FAKE_FAIL="consolidate", CONSOLIDATE_BATCH="5")
    log = log_of(root)
    assert r.returncode == 1 and "nightly INCOMPLETE" in log and "consolidate-" in log, log
    assert cards_in(units(root)) == 0
    carried = list((root / "memory/ledger/carry").rglob("cons-*.md"))
    assert len(carried) == 2, carried
    assert (root / "state/shared/last-nightly").exists()            # the window itself is persisted
    r2 = run_night(tmp_path, root, fake, NIGHTLY_DATE="2099-01-03", CONSOLIDATE_BATCH="5")
    assert r2.returncode == 0, log_of(root)
    assert cards_in(units(root)) == 10 and not list((root / "memory/ledger/carry").rglob("cons-*.md"))


def test_a_failed_sweep_is_reported_unstamped_and_recorded(tmp_path):
    root, fake = make_base(tmp_path, n_marks=3)
    tr = tmp_path / "source"
    tr.mkdir()
    f = tr / "s.jsonl"
    f.write_text(json.dumps({"type": "user", "timestamp": "2099-01-01T10:00:00Z",
                             "message": {"content": "unmarked fact " * 30}}) + "\n")
    os.utime(f, (time.time() + 3600,) * 2)
    r = run_night(tmp_path, root, fake, FAKE_FAIL="sweep", KHMS_TRANSCRIPTS=tr)
    log = log_of(root)
    assert r.returncode == 1 and "sweep FAILED after retries" in log and "nightly INCOMPLETE" in log, log
    assert not (root / "state/shared/last-nightly").exists()
    assert "window-since=" in (root / "state/shared/sweep-failed").read_text()


def test_a_missing_agent_fails_the_model_stages_visibly(tmp_path):
    root, fake = make_base(tmp_path, n_marks=3)
    r = run_night(tmp_path, root, fake, KHMS_AGENT=str(tmp_path / "no-such-agent"))
    log = log_of(root)
    assert r.returncode == 1 and "AGENT UNAVAILABLE" in log and "agent-unavailable" in log, log
    assert not (root / "state/shared/last-nightly").exists()
    assert not (tmp_path / "calls.txt").exists()


def test_an_unknown_argument_is_refused_before_any_write(tmp_path):
    root, fake = make_base(tmp_path)
    r = run_night(tmp_path, root, fake, "--dry")
    assert r.returncode == 0 and "DRY RUN" in r.stdout
    assert not (root / "state/shared/last-nightly").exists() and not (tmp_path / "calls.txt").exists()
    os.remove(root / "state/nightly.log")
    r = run_night(tmp_path, root, fake, "--bogus")
    assert r.returncode == 2 and "unknown argument" in r.stderr
    assert not (root / "state/nightly.log").exists()


def test_a_reader_deployment_refuses_the_night(tmp_path):
    root, fake = make_base(tmp_path)
    r = run_night(tmp_path, root, fake, KHMS_READ_ONLY="1")
    assert r.returncode == 4 and "read-only" in r.stderr
    assert not (root / "state").joinpath("nightly.log").exists() and not (root / "memory/ledger").exists()


# --- the code-use lock -----------------------------------------------------------------------------
def test_an_update_holding_the_code_lock_stops_the_night_and_a_running_night_stops_the_update(tmp_path):
    root, fake = make_base(tmp_path, n_marks=2)
    env = env_for(tmp_path, root, fake)
    lockf = subprocess.run([sys.executable, os.path.join(TOOLS, "khms_paths.py"), "--code-lock-file"],
                           env=env, capture_output=True, text=True, check=True).stdout.strip()
    assert lockf == os.path.join(os.path.dirname(TOOLS), ".git", "khms-code.lock") or not os.path.isdir(
        os.path.join(os.path.dirname(TOOLS), ".git")), lockf
    os.makedirs(os.path.dirname(lockf), exist_ok=True)
    holder = subprocess.Popen(["flock", "-x", lockf, "sleep", "30"], start_new_session=True)
    try:
        time.sleep(0.3)
        r = run_night(tmp_path, root, fake)
        assert r.returncode == 5 and "being updated" in r.stderr, r.stderr
        assert not (tmp_path / "calls.txt").exists()
    finally:
        os.killpg(holder.pid, 9)
        holder.wait()
    # a running user (shared) blocks the exclusive update; a nested user does not deadlock
    script = (f". {TOOLS}/khms_paths.sh; khms_load; khms_code_lock || exit 9; "
              f"bash -c '. {TOOLS}/khms_paths.sh; khms_load; khms_code_lock' || exit 8; "
              f"flock -x -n {lockf} true && exit 7; exit 0")
    r = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, (r.returncode, r.stderr)


# --- prompts ---------------------------------------------------------------------------------------
@pytest.mark.parametrize("name", ["nightly-sweep", "nightly-marks", "nightly-consolidate",
                                  "weekly-part", "weekly-synthesis"])
def test_every_stage_prompt_renders_completely_without_private_extensions(tmp_path, name):
    env = dict(os.environ, KHMS_ROOT=str(tmp_path))
    r = subprocess.run([sys.executable, os.path.join(TOOLS, "khms_prompt.py"), name], env=env,
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "{{" not in r.stdout and str(tmp_path) in r.stdout or name == "weekly-part"


def test_an_extension_replaces_the_shipped_default_and_a_bad_field_fails(tmp_path):
    ext = tmp_path / "config" / "prompts"
    ext.mkdir(parents=True)
    env = dict(os.environ, KHMS_ROOT=str(tmp_path))
    run = lambda n: subprocess.run([sys.executable, os.path.join(TOOLS, "khms_prompt.py"), n], env=env,
                                   capture_output=True, text=True)
    base = run("nightly-sweep").stdout
    assert "English only." in base
    (ext / "nightly-sweep.summary-line.md").write_text("Use temp ids as headings. Add a SUMMARY line.\n")
    got = run("nightly-sweep").stdout
    assert "Add a SUMMARY line." in got and "English only." not in got
    import khms_prompt
    tpl = os.path.join(TOOLS, "prompts", "zz-test.md")
    try:
        open(tpl, "w").write("{{NO_SUCH_FIELD}}\n")
        r = run("zz-test")
        assert r.returncode == 3 and "unknown field" in r.stderr
    finally:
        os.remove(tpl)
    assert khms_prompt.FIELD_RE


# --- the ledger's vocabulary ---------------------------------------------------------------------------
def test_the_kind_table_comes_from_private_patterns_and_keeps_ids(tmp_path):
    import importlib
    j = tmp_path / "journal"
    j.mkdir()
    (j / "2099-01-01.md").write_text("- 10:00 MARK[entscheidung] widgets are blue\n"
                                     "- 10:01 MARK[decision] widgets are red\n")
    pat = tmp_path / "patterns.env"
    pat.write_text("LEDGER_KINDS=entscheidung:entscheidung,decision;finding:finding\n")
    code = ("import sys; sys.path.insert(0, %r); import mark_ledger as ml; "
            "p = ml.Paths(%r); print(sorted((m['kind'], m['id']) for m in ml.collect_marks(p, '2099-01-01').values()))"
            % (TOOLS, str(tmp_path)))
    base = {k: v for k, v in os.environ.items() if not k.startswith("KHMS_")}
    eng = subprocess.run([sys.executable, "-c", code], env=base, capture_output=True, text=True)
    priv = subprocess.run([sys.executable, "-c", code], env=dict(base, KHMS_PATTERNS=str(pat)),
                          capture_output=True, text=True)
    assert eng.returncode == 0 and priv.returncode == 0, eng.stderr + priv.stderr
    # New #209 policy: unfamiliar non-status kinds stay visible; aliases still
    # decide the canonical name (and therefore preserve deployment IDs).
    assert eng.stdout.count("'decision'") == 1 and eng.stdout.count("'entscheidung'") == 1
    assert priv.stdout.count("'entscheidung'") == 2, priv.stdout
    importlib.import_module("mark_ledger")


# --- the weekly synthesis ----------------------------------------------------------------------------
WFAKE = r'''#!/usr/bin/env python3
# Fake agent for the weekly: "reads" every `  * <path>` of its prompt into its own session transcript
# (the records read_coverage.py checks) except paths containing FAKE_SKIP_READ, then writes its output.
import json, os, re, sys
args = sys.argv[1:]
if args[:1] == ["--version"]:
    print("0.0.0 (fake weekly agent)"); sys.exit(0)
prompt = args[args.index("-p") + 1]
sid = args[args.index("--session-id") + 1]
out = re.findall(r"^OUTPUT FILE: (.+)$", prompt, re.M)[-1].strip()
with open(os.environ["FAKE_CALLS"], "a") as fh:
    fh.write(("part" if "WEEKLY PART " in prompt else "merge") + " " + args[args.index("--model") + 1] + "\n")
root, skip = os.environ["WEEKLY_TRANSCRIPT_ROOT"], os.environ.get("FAKE_SKIP_READ") or None
os.makedirs(os.path.join(root, "fake"), exist_ok=True)
with open(os.path.join(root, "fake", sid + ".jsonl"), "w") as tr:
    for i, path in enumerate(re.findall(r"^  \* (\S+)\s*$", prompt, re.M)):
        if skip and skip in path:
            continue
        n = len(open(path, encoding="utf-8", errors="replace").read().split("\n"))
        tid = "toolu_%s_%d" % (sid[:8], i)
        tr.write(json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": tid, "name": "Read", "input": {"file_path": path}}]}}) + "\n")
        tr.write(json.dumps({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": tid, "content": "..."}]},
            "toolUseResult": {"type": "text", "file": {"filePath": path, "numLines": n,
                                                       "startLine": 1, "totalLines": n}}}) + "\n")
if "WEEKLY PART " in prompt:
    open(out, "w").write("# Weekly part - findings\n\n### F1: something\nKIND: other\n")
else:
    open(out, "w").write("# weekly synthesis\n\n## Cards\n\n" + "".join(
        f"### C{i}: weekly finding {i:02d}\nWHAT: widget {i}\nRELATION: supports CARDID\n**QUOTES:**\n"
        f"- src=CARDID :: Widgets are measured in units.\n\n" for i in range(1, 13)))
'''.replace("CARDID", CARD)


def run_week(tmp_path, root, **extra):
    fake = tmp_path / "fake_week.py"
    fake.write_text(WFAKE)
    fake.chmod(0o755)
    inbox = root / "memory/inbox"
    for k in range(1, 4):
        (inbox / f"2099-01-0{k}.md").write_text(f"# 2099-01-0{k}\nSENTINEL<{k}>\n" + "widget wear line\n" * 3000)
    (root / "memory/views/condense-candidates.md").write_text("# none\n")
    env = env_for(tmp_path, root, fake, WEEKLY_DATE="2099-01-04",
                  WEEKLY_TRANSCRIPT_ROOT=str(tmp_path / "transcripts"), WEEKLY_PART_ATTEMPTS="1")
    env.update({k: str(v) for k, v in extra.items()})
    return subprocess.run(["bash", os.path.join(TOOLS, "weekly_synthesis.sh")], env=env,
                          capture_output=True, text=True, timeout=300)


def test_the_week_is_read_in_proven_parts_and_every_candidate_is_kept(tmp_path):
    root, _ = make_base(tmp_path)
    r = run_week(tmp_path, root)
    log = log_of(root)
    assert r.returncode == 0, log + r.stderr
    out = (root / "memory/inbox/2099-01-04-weekly.md").read_text()
    assert len(re.findall(r"(?m)^### C\d+", out)) == 12 and "DEFERRED" not in out
    calls = (tmp_path / "calls.txt").read_text().split("\n")
    assert calls.count("merge opus") == 1 and any(c == "part sonnet" for c in calls), calls
    assert (root / "state/shared/last-weekly").read_text().strip() and "weekly done" in log


def test_a_week_part_that_did_not_read_its_input_fails_the_weekly_unstamped(tmp_path):
    root, _ = make_base(tmp_path)
    r = run_week(tmp_path, root, FAKE_SKIP_READ="inboxes-part")
    log = log_of(root)
    assert r.returncode == 1 and "weekly FAILED" in log and "did not read all of its input" in log, log
    assert not (root / "memory/inbox/2099-01-04-weekly.md").exists()
    assert not (root / "state/shared/last-weekly").exists()


def test_the_weekly_waits_for_a_running_night_and_holds_the_code_lock(tmp_path):
    root, _ = make_base(tmp_path)
    lock = root / "state/shared/nightly.lock"
    holder = subprocess.Popen(["flock", "-x", str(lock), "sleep", "30"], start_new_session=True)
    try:
        time.sleep(0.3)
        r = run_week(tmp_path, root, WEEKLY_NIGHT_WAIT_MIN="0", KHMS_NIGHTLY_LOCK=str(lock))
    finally:
        os.killpg(holder.pid, 9)
        holder.wait()
    log = log_of(root)
    assert r.returncode == 1 and "weekly waits for the nightly" in log and "half-published" in log, log
    assert not (root / "memory/inbox/2099-01-04-weekly.md").exists()


# --- review F1 (#173 step 3a): ONE code-use lock per clone, whatever base or agent state is selected -------
def _lock_path(env, shell=False):
    if shell:
        cmd = ["bash", "-c", '. "%s/khms_paths.sh" && khms_load && khms_code_lock_file' % TOOLS]
    else:
        cmd = [sys.executable, os.path.join(TOOLS, "khms_paths.py"), "--code-lock-file"]
    return subprocess.run(cmd, env=env, capture_output=True, text=True, check=True).stdout.strip()


@pytest.mark.parametrize("key", ["KHMS_ROOT", "KHMS_STATE"])
def test_the_same_clone_with_another_base_or_state_cannot_update_while_in_use(tmp_path, key):
    import fcntl
    root, fake = make_base(tmp_path / "one", 2)
    env = env_for(tmp_path, root, fake)
    other = tmp_path / "other"
    (other / "memory" / "know").mkdir(parents=True)
    one = _lock_path(env)
    two = _lock_path(dict(env, **{key: str(other)}))
    assert one == two == _lock_path(env, shell=True), (one, two)
    os.makedirs(os.path.dirname(one), exist_ok=True)
    with open(one, "a") as held, open(two, "a") as update:
        fcntl.flock(held, fcntl.LOCK_SH | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            fcntl.flock(update, fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_the_code_lock_lives_with_the_clone_not_in_any_data_or_state_root(tmp_path):
    root, fake = make_base(tmp_path, 2)
    env = env_for(tmp_path, root, fake)
    lockf = _lock_path(env)
    clone = os.path.realpath(os.path.dirname(TOOLS))
    assert os.path.realpath(lockf).startswith(clone + os.sep), lockf
    assert not os.path.realpath(lockf).startswith(os.path.realpath(str(tmp_path)) + os.sep), lockf


# --- review F2 (#173 step 3a): a configured source that fails keeps its window ----------------------------
def test_a_failed_git_source_keeps_its_window_until_the_source_is_back(tmp_path):
    root, fake = make_base(tmp_path, 2)
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "a").write_text("fixture\n")
    subprocess.run(["git", "-C", str(repo), "add", "a"], check=True)
    e = dict(os.environ, GIT_AUTHOR_DATE="2026-01-01T12:00:00Z", GIT_COMMITTER_DATE="2026-01-01T12:00:00Z")
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=fixture", "-c", "user.email=fixture",
                    "commit", "-qm", "UNREAD_SOURCE_SENTINEL"], env=e, check=True)
    offline = tmp_path / "offline-repo"
    repo.rename(offline)
    first = run_night(tmp_path, root, fake, KHMS_GIT_REPOS=repo)
    log = log_of(root)
    assert first.returncode != 0 and "source FAILED: git:" in log and "nightly INCOMPLETE" in log, log
    assert not (root / "state" / "shared" / "last-nightly").exists(), "a failed source advanced the stamp"
    assert "sources-failed=1" in (root / "state" / "shared" / "sweep-failed").read_text()
    offline.rename(repo)
    second = run_night(tmp_path, root, fake, KHMS_GIT_REPOS=repo, NIGHTLY_DATE="2099-01-03")
    gitinput = (root / "memory" / "inbox" / ".staging" / "nightly-2099-01-03-git.txt").read_text()
    assert second.returncode == 0 and "UNREAD_SOURCE_SENTINEL" in gitinput, (second.returncode, gitinput)
    assert (root / "state" / "shared" / "last-nightly").exists()


def test_a_missing_configured_transcript_directory_is_not_checkpointed(tmp_path):
    root, fake = make_base(tmp_path, 2)
    r = run_night(tmp_path, root, fake, KHMS_TRANSCRIPTS=tmp_path / "not-mounted")
    log = log_of(root)
    assert r.returncode != 0 and not (root / "state" / "shared" / "last-nightly").exists(), (r.returncode, log)
    assert "source FAILED: transcripts:" in log, log


def test_an_unconfigured_source_is_off_not_failed(tmp_path):
    root, fake = make_base(tmp_path, 2)
    r = run_night(tmp_path, root, fake)
    log = log_of(root)
    assert r.returncode == 0 and "source FAILED" not in log and "source git: OFF" in log, log


def test_prepare_receives_zero_journal_tokens_and_no_marked_source_records(tmp_path):
    root, fake = make_base(tmp_path, n_marks=2)
    tr = tmp_path / "source"
    tr.mkdir()
    records = ["unmarked widget calibration " + "useful knowledge " * 30,
               "MARK[finding] marked transcript secret\ncontinuation of marked secret"]
    f = tr / "s.jsonl"
    f.write_text("".join(json.dumps({"type": "assistant", "timestamp": "2099-01-01T10:00:00Z",
                                    "message": {"content": text}}) + "\n" for text in records))
    os.utime(f, (time.time() + 3600,) * 2)
    r = run_night(tmp_path, root, fake, "--dry", KHMS_TRANSCRIPTS=tr)
    assert r.returncode == 0, r.stderr
    block = r.stdout.split("--- SWEEP stage INPUTS block", 1)[1].split("--- CONSOLIDATE", 1)[0]
    assert "journal" not in block.lower(), block
    digest = (root / "memory/inbox/.staging/nightly-2099-01-02-dry-digest.txt").read_text()
    assert "unmarked widget calibration" in digest
    assert "marked transcript secret" not in digest and "continuation of marked secret" not in digest
    assert "prepare journal tokens: 0" in r.stdout


def test_prepare_cannot_inject_journal_candidates(tmp_path):
    root, fake = make_base(tmp_path, n_marks=1)
    tr = tmp_path / "source"
    tr.mkdir()
    f = tr / "s.jsonl"
    f.write_text(json.dumps({"type": "user", "timestamp": "2099-01-01T10:00:00Z",
                             "message": {"content": "unmarked calibration " * 30}}) + "\n")
    os.utime(f, (time.time() + 3600,) * 2)
    # Deliberately disobedient fake prepare output: real journal text is not permission to ingest it.
    fake.write_text(FAKE.replace('js = field(r"^- journal[^:]*: (.+)$")',
                                'js = os.environ["INJECT_JOURNAL"]'))
    r = run_night(tmp_path, root, fake, KHMS_TRANSCRIPTS=tr,
                  INJECT_JOURNAL=root / "journal/2099-01-01.md")
    assert r.returncode == 0, log_of(root)
    assert cards_in(units(root)) == 1
    text = "".join(p.read_text() for p in units(root))
    assert "### C1:" not in text and "MARKS: M-" in text


@pytest.mark.parametrize("group_size", [1, 2])
def test_prepare_calls_one_fresh_process_per_digest_and_git_chunk(tmp_path, group_size):
    root, fake = make_base(tmp_path, n_marks=0)
    tr = tmp_path / "source"
    tr.mkdir()
    f = tr / "s.jsonl"
    f.write_text("".join(json.dumps({"type": "user", "timestamp": "2099-01-01T10:00:00Z",
                                    "message": {"content": "widget unmarked calibration " * 80}}) + "\n"
                         for _ in range(45)))
    os.utime(f, (time.time() + 3600,) * 2)
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t",
                    "commit", "--allow-empty", "-qm", "git calibration " * 2000], check=True)
    r = run_night(tmp_path, root, fake, KHMS_TRANSCRIPTS=tr, KHMS_GIT_REPOS=repo,
                  SWEEP_GROUP_CHUNKS=group_size)
    assert r.returncode == 0, log_of(root) + r.stderr
    staging = root / "memory/inbox/.staging"
    expected = {}
    for source, suffix in (("digest", "digest"), ("gitlog", "git")):
        for p in (staging / f"nightly-2099-01-02-{suffix}-chunks.txt").read_text().splitlines():
            expected[p] = source
    calls = [json.loads(line) for line in (tmp_path / "calls.txt.jsonl").read_text().splitlines()]
    sweep = [call for call in calls if call["stage"] == "sweep"]
    assert len(sweep) == (len(expected) + group_size - 1) // group_size
    assert len({call["pid"] for call in sweep}) == len(sweep)
    seen = []
    for call in sweep:
        block = call["prompt"].rsplit("INPUTS:", 1)[1]
        paths = re.findall(r"^  \* (.+)$", block, re.M)
        assert 1 <= len(paths) <= group_size, block
        assert all(f"src={expected[path]}" in block for path in paths)
        assert "whole file" not in block and "journal" not in block
        assert "--resume" not in call["args"] and "--continue" not in call["args"]
        seen.extend(paths)
    assert sorted(seen) == sorted(expected)


@pytest.mark.parametrize("source", ["digest", "gitlog"])
def test_prepare_singleton_sources_use_one_owned_chunk(tmp_path, source):
    root, fake = make_base(tmp_path, n_marks=0)
    extra = {}
    if source == "digest":
        tr = tmp_path / "source"
        tr.mkdir()
        f = tr / "s.jsonl"
        f.write_text(json.dumps({"type": "user", "timestamp": "2099-01-01T10:00:00Z",
                                 "message": {"content": "unmarked calibration " * 30}}) + "\n")
        os.utime(f, (time.time() + 3600,) * 2)
        extra["KHMS_TRANSCRIPTS"] = tr
    else:
        repo = tmp_path / "repo"
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t",
                        "commit", "--allow-empty", "-qm", "calibration commit"], check=True)
        extra["KHMS_GIT_REPOS"] = repo
    r = run_night(tmp_path, root, fake, **extra)
    assert r.returncode == 0, log_of(root) + r.stderr
    calls = [json.loads(line) for line in (tmp_path / "calls.txt.jsonl").read_text().splitlines()]
    sweep = [c for c in calls if c["stage"] == "sweep"]
    assert len(sweep) == 1
    inputs = sweep[0]["prompt"].rsplit("INPUTS:", 1)[1]
    assert len(re.findall(r"^  \* (.+)$", inputs, re.M)) == 1
    assert f"src={source}" in inputs and "whole file" not in inputs


@pytest.mark.parametrize("failed_source", ["digest", "git"])
def test_failed_chunker_with_partial_index_keeps_window_and_never_sweeps(tmp_path, failed_source):
    root, fake = make_base(tmp_path, n_marks=0)
    tr = tmp_path / "source"
    tr.mkdir()
    f = tr / "s.jsonl"
    f.write_text("".join(json.dumps({"type": "user", "timestamp": "2099-01-01T10:00:00Z",
                                    "message": {"content": "unmarked knowledge " * 100}}) + "\n"
                         for _ in range(35)))
    os.utime(f, (time.time() + 3600,) * 2)
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t",
                    "commit", "--allow-empty", "-qm", "calibration " * 3000], check=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    wrapper = bin_dir / "python3"
    wrapper.write_text(f"#!{sys.executable}\n" + r'''
import os, pathlib, subprocess, sys
args = sys.argv[1:]
rc = subprocess.run([sys.executable, *args]).returncode
if args and pathlib.Path(args[0]).name == "digest_chunk.py" and args[1].endswith("-" + os.environ["FAIL_CHUNK_SOURCE"] + ".txt"):
    index = pathlib.Path(args[args.index("--index") + 1])
    rows = index.read_text().splitlines()
    assert len(rows) > 1
    index.write_text(rows[0] + "\n")
    sys.exit(1)
sys.exit(rc)
''')
    wrapper.chmod(0o755)
    r = run_night(tmp_path, root, fake, KHMS_TRANSCRIPTS=tr, KHMS_GIT_REPOS=repo,
                  PATH=str(bin_dir) + os.pathsep + os.environ["PATH"], FAIL_CHUNK_SOURCE=failed_source)
    assert r.returncode != 0 and "sweep NOT RUN" in log_of(root), log_of(root)
    assert not (root / "state/shared/last-nightly").exists()
    call_log = tmp_path / "calls.txt.jsonl"
    calls = [json.loads(line) for line in call_log.read_text().splitlines()] if call_log.exists() else []
    assert not any(call["stage"] == "sweep" for call in calls)
    index = root / f"memory/inbox/.staging/nightly-2099-01-02-{failed_source}-chunks.txt"
    assert len(index.read_text().splitlines()) == 1


def test_split_success_with_missing_chunk_blocks_prepare_and_stamp(tmp_path):
    root, fake = make_base(tmp_path, n_marks=0)
    tr = tmp_path / "source"
    tr.mkdir()
    f = tr / "s.jsonl"
    f.write_text("".join(json.dumps({"type": "user", "timestamp": "2099-01-01T10:00:00Z",
                                    "message": {"content": "unmarked calibration " * 100}}) + "\n"
                         for _ in range(35)))
    os.utime(f, (time.time() + 3600,) * 2)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    wrapper = bin_dir / "split"
    wrapper.write_text(f"#!{sys.executable}\n" + r'''
import pathlib, subprocess, sys
args = sys.argv[1:]
rc = subprocess.run(["/usr/bin/split", *args]).returncode
if rc == 0 and "prepare-group" in args[-1]:
    prefix = pathlib.Path(args[-1])
    groups = sorted(prefix.parent.glob(prefix.name + "*.txt"))
    assert len(groups) > 1
    groups[-1].unlink()
sys.exit(rc)
''')
    wrapper.chmod(0o755)
    r = run_night(tmp_path, root, fake, KHMS_TRANSCRIPTS=tr,
                  PATH=str(bin_dir) + os.pathsep + os.environ["PATH"])
    assert r.returncode != 0 and "sweep NOT RUN" in log_of(root), log_of(root)
    assert not (root / "state/shared/last-nightly").exists()
    call_log = tmp_path / "calls.txt.jsonl"
    calls = [json.loads(line) for line in call_log.read_text().splitlines()] if call_log.exists() else []
    assert not any(call["stage"] == "sweep" for call in calls)
