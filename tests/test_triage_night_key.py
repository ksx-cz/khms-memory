"""Every per-night triage artefact is named by the NIGHT KEY, not by the calendar date (#237).

A second night on the same date (night key DATE-2, claimed by nightly_distill.sh like its card units
`DATE-2-m01-b01.md`) used to write `memory/inbox/DATE-triage.md` again -- over the morning's file, already
reviewed -- and its apply report over the morning's `DATE-triage.applied.md`; its skip of recently judged pairs
read only the sweep JSONs of EARLIER DATES, so it re-judged every pair the morning night had judged.

  * two nights of one date (NK=DATE, then DATE-2) write two triage files, and the second skips the pairs the
    first judged (the shell stage passes its night key; the real `recently_judged` honours it);
  * applying the second night's file leaves the first file's `.applied.md` as it was;
  * the night driver hands its key to the triage stage (two real nights of one date -> two files);
  * the skip reads a DATE-N sweep JSON on later nights and never reads its own night's files;
  * the review ping, the hook's census and the weekly window see a DATE-N triage file like a DATE one;
  * a malformed night key is refused before any work;
  * the know-how rule's placeholder card id is gone from the tree.

Synthetic fixtures only: a throwaway base, a copy of the tools with a fake sweep, no model, no network.
"""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

REPO = Path(__file__).resolve().parent.parent
TOOLS = REPO / "tools"
DATE = "2099-01-01"


def module(name):
    spec = importlib.util.spec_from_file_location("_nk_" + name, REPO / "tests" / (name + ".py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


night = module("test_night_pipeline")

# The fake sweep judges three fixed pairs minus the pairs the REAL skip (khms_contradict.recently_judged, kept
# beside it as real_contradict.py) returns for the arguments the stage passed. Old and new signatures alike:
# `--night-key` is forwarded only when the stage passes it.
FAKE_SWEEP = r'''
import datetime, json, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
a = sys.argv[1:]
if "--provenance" in a:
    print("# provenance (fake)"); sys.exit(0)
def opt(name):
    return a[a.index(name) + 1] if name in a else None
import real_contradict as kc
kw = {"night_key": opt("--night-key")} if opt("--night-key") else {}
skip, _ = kc.recently_judged(opt("--skip-judged-dir"), int(opt("--skip-nights") or 0), opt("--today"),
                             lambda cid: 0.0, **kw)
pairs = [("K-90001", "K-90002"), ("K-90003", "K-90004"), ("K-90005", "K-90006")]
todo = [p for p in pairs if tuple(sorted(p)) not in skip]
open(os.environ["FAKE_CALLS"], "a").write(" ".join(f"{x}/{y}" for x, y in todo) + "\n")
res = [{"a": x, "b": y, "verdict": {"contradiction": "no", "outcome": "OK"}} for x, y in todo]
json.dump({"results": res, "meta": {"judge": os.environ.get("KHMS_JUDGE", "?"),
                                     "progress": {"planned": len(todo),
                                                  "started": datetime.datetime.now().isoformat()}}},
          open(opt("--json"), "w"))
'''
FAKE_REPORT = r'''
import json, os, sys
from collections import Counter
def judgement_census(results, meta):
    c = Counter((r.get("verdict") or {}).get("outcome") or "OK" for r in results)
    planned = max((meta.get("progress") or {}).get("planned") or 0, len(results))
    return {"judged": c["OK"], "planned": planned, "not_judged": len(results) - c["OK"],
            "no_record": planned - len(results), "partial": c["OK"] < planned, "by_outcome": dict(c)}
if __name__ == "__main__":
    a = sys.argv[1:]
    d = json.load(open(a[0]))
    pairs = " ".join(f"{r['a']}/{r['b']}" for r in d["results"]) or "none"
    open(a[a.index("--out") + 1], "w").write(f"# triage (fake, written by pid {os.getpid()})\njudged: {pairs}\n\n## REVIEW VERDICT\n")
'''


def stage_code(tmp):
    code = tmp / "clone/tools"
    shutil.copytree(TOOLS, code, ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy(TOOLS / "khms_contradict.py", code / "real_contradict.py")
    (code / "khms_contradict.py").write_text(FAKE_SWEEP)
    (code / "khms_triage_report.py").write_text(FAKE_REPORT)
    return code


def base(tmp):
    root = tmp / "base"
    for d in ("memory/know", "memory/inbox", "state/shared"):
        (root / d).mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("KHMS_", "NIGHTLY_", "TRIAGE_"))}
    env.update(KHMS_ROOT=str(root), KHMS_STATE=str(tmp / "agent"), KHMS_READ_ONLY="0", KHMS_NO_SNAPSHOT="1",
               PYTHONDONTWRITEBYTECODE="1", NIGHTLY_DATE=DATE, FAKE_CALLS=str(tmp / "calls"))
    return root, env


def run(argv, env):
    return subprocess.run(argv, env=env, text=True, capture_output=True, timeout=120)


def calls(tmp):
    return (tmp / "calls").read_text().splitlines()


def two_nights(tmp_path):
    root, env = base(tmp_path)
    code = stage_code(tmp_path)
    r1 = run(["bash", str(code / "nightly_triage.sh")], env)                      # the first night: NK = DATE
    assert r1.returncode == 0, r1.stderr
    inbox = root / "memory/inbox"
    first = inbox / f"{DATE}-triage.md"
    morning = first.read_text()
    r2 = run(["bash", str(code / "nightly_triage.sh")], dict(env, NIGHTLY_NK=f"{DATE}-2"))
    assert r2.returncode == 0, r2.stderr
    return root, env, inbox, first, morning


def test_two_nights_of_one_date_write_two_files_and_the_second_skips_what_the_first_judged(tmp_path):
    root, env, inbox, first, morning = two_nights(tmp_path)
    second = inbox / f"{DATE}-2-triage.md"
    assert first.read_text() == morning, "the second night overwrote the first night's triage file"
    assert second.exists(), sorted(p.name for p in inbox.iterdir())
    assert "judged: K-90001/K-90002 K-90003/K-90004 K-90005/K-90006" in morning
    assert "judged: none" in second.read_text(), second.read_text()
    assert calls(tmp_path)[1] == "", calls(tmp_path)          # the second night sent no pair at all
    staging = root / "memory/inbox/.staging"
    assert (staging / f"triage-{DATE}-sweep.json").exists()
    assert (staging / f"triage-{DATE}-2-sweep.json").exists()


def test_applying_the_second_file_leaves_the_first_applied_report_alone(tmp_path):
    root, env, inbox, first, morning = two_nights(tmp_path)
    apply = [sys.executable, str(TOOLS / "apply_triage.py")]
    r = run(apply + [str(first), "--apply"], env)
    assert r.returncode == 0, r.stderr
    first_applied = inbox / f"{DATE}-triage.applied.md"
    before = first_applied.read_text()
    second = inbox / f"{DATE}-2-triage.md"
    r = run(apply + [str(second), "--apply"], env)
    assert r.returncode == 0, r.stderr
    assert first_applied.read_text() == before
    assert (inbox / f"{DATE}-2-triage.applied.md").exists()
    rev = root / "state/shared/reviewed"
    assert (rev / f".reviewed-{DATE}-triage").exists() and (rev / f".reviewed-{DATE}-2-triage").exists()


def test_a_malformed_night_key_is_refused_before_any_work(tmp_path):
    root, env = base(tmp_path)
    code = stage_code(tmp_path)
    for bad in ("2099-01-01-x", "../2099-01-01", "2099-01-01-2 x"):
        r = run(["bash", str(code / "nightly_triage.sh")], dict(env, NIGHTLY_NK=bad))
        assert r.returncode == 2 and "night key" in r.stderr, (bad, r.returncode, r.stderr)
    assert not (tmp_path / "calls").exists() and list((root / "memory/inbox").iterdir()) == []


def test_the_night_driver_hands_its_night_key_to_the_triage_stage(tmp_path):
    """Two real nights of one date: nightly_distill.sh claims DATE then DATE-2 and the triage follows it."""
    root, fake = night.make_base(tmp_path, n_marks=1)
    code = stage_code(tmp_path)
    env = night.env_for(tmp_path, root, fake, KHMS_NIGHT_TRIAGE="bash " + str(code / "nightly_triage.sh"),
                        FAKE_CALLS=str(tmp_path / "calls"))
    r = run(["bash", str(TOOLS / "nightly_distill.sh")], env)
    assert r.returncode == 0, r.stderr + night.log_of(root)
    (root / "journal/2099-01-01.md").unlink()
    r = run(["bash", str(TOOLS / "nightly_distill.sh")], env)
    assert r.returncode == 0, r.stderr + night.log_of(root)
    inbox = root / "memory/inbox"
    names = sorted(p.name for p in inbox.iterdir() if p.name.endswith("-triage.md"))
    assert names == ["2099-01-02-2-triage.md", "2099-01-02-triage.md"], (names, night.log_of(root))
    assert "judged: none" in (inbox / "2099-01-02-2-triage.md").read_text()


# --- the skip itself (khms_contradict.recently_judged) -----------------------------------------------------
def _real(monkeypatch, tmp_path):
    for k in [k for k in os.environ if k.startswith("KHMS_")]:
        monkeypatch.delenv(k)
    monkeypatch.setenv("KHMS_ROOT", str(tmp_path / "base"))
    monkeypatch.syspath_prepend(str(TOOLS))
    for m in ("khms_contradict", "khms_paths", "khms_patterns", "khms_messages", "khms_search",
              "khms_triage_gate", "khms_triage_report", "khms_triage"):
        sys.modules.pop(m, None)
    import khms_contradict as kc
    return kc


def _sweep(staging, key, a, b):
    staging.mkdir(parents=True, exist_ok=True)
    (staging / f"triage-{key}-sweep.json").write_text(json.dumps({
        "results": [{"a": a, "b": b, "verdict": {"outcome": "OK"}}],
        "meta": {"progress": {"started": "2099-01-01T03:30:00"}}}))


def test_the_skip_reads_other_nights_of_the_date_and_later_dates_but_never_its_own(tmp_path, monkeypatch):
    kc = _real(monkeypatch, tmp_path)
    st = tmp_path / "st"
    _sweep(st, DATE, "K-90001", "K-90002")
    _sweep(st, f"{DATE}-2", "K-90003", "K-90004")
    old = lambda cid: 0.0                                       # noqa: E731  every card older than the run
    second, acc = kc.recently_judged(str(st), 28, DATE, old, night_key=f"{DATE}-2")
    assert second == {("K-90001", "K-90002")}, (second, acc)    # the morning's pair; not its own night's
    first, _ = kc.recently_judged(str(st), 28, DATE, old, night_key=DATE)
    assert first == {("K-90003", "K-90004")}                    # a rerun of DATE sees DATE-2, not itself
    later, acc = kc.recently_judged(str(st), 28, "2099-01-02", old)
    assert later == {("K-90001", "K-90002"), ("K-90003", "K-90004")}, acc
    assert f"triage-{DATE}-2-sweep.json" in acc["files"]


# --- the morning tools see a DATE-N triage file ---------------------------------------------------------------
def test_the_review_ping_lists_a_second_nights_triage_file(tmp_path):
    today = "2031-05-06"
    root = tmp_path / "base"
    (root / "state/shared/reviewed").mkdir(parents=True)
    (root / "memory/inbox").mkdir(parents=True)
    (root / "state/shared/last-nightly").write_text(today + "T03:30:00+00:00\n")
    (root / "state/shared/last-weekly").write_text(today + "T04:00:00+00:00\n")
    census = "Sweep: **2 of 2 planned pairs were actually judged**\n"
    (root / f"memory/inbox/{today}-triage.md").write_text(census)
    (root / f"state/shared/reviewed/.reviewed-{today}-triage").write_text("")
    (root / f"memory/inbox/{today}-2-triage.md").write_text(census)
    env = {k: v for k, v in os.environ.items() if not k.startswith("KHMS_")}
    env.update(KHMS_ROOT=str(root), KHMS_CONFIG_DIR=str(tmp_path / "cfg"), KHMS_STATE=str(tmp_path / "agent"),
               KHMS_PING_TODAY=today, KHMS_PING_PGREP="no-such-sweep-process-xyz", KHMS_PING_DRYRUN="1")
    p = run(["bash", str(TOOLS / "khms_review_ping.sh")], env)
    msg = [ln for ln in p.stdout.splitlines() if ln.startswith("[AUTO-PING]")]
    assert p.returncode == 0 and msg, (p.stdout, p.stderr)
    assert f"{today}-2-triage.md: PENDING (judged 2 of 2 planned pairs)" in msg[0], msg[0]
    assert "triage: reviewed (judged 2 of 2 planned pairs)" in msg[0], msg[0]


def test_the_weekly_window_treats_a_second_nights_triage_report_as_an_artefact(tmp_path):
    names = ["2026-10-07-2-triage.md", "2026-10-07-2-triage.applied.md", "2026-10-07-2-triage.preview.md"]
    for n in names:
        (tmp_path / n).write_text("source\n")
    r = run([sys.executable, str(TOOLS / "weekly_inbox_window.py"), str(tmp_path), "2026-10-11"], dict(os.environ))
    assert r.returncode == 0, r.stderr
    assert [Path(p).name for p in r.stdout.splitlines()] == ["2026-10-07-2-triage.md"], (r.stdout, r.stderr)
    assert "NOT SELECTED 2026-10-07-2-triage.applied.md: triage artifact" in r.stderr


# --- #237 item 2: the placeholder id the know-how rule cited -------------------------------------------------
def test_the_know_how_rule_cites_no_placeholder_card_id():
    wrong = "K-9" + "3929"                          # built, so this file does not hold it either
    hits = []
    for p in REPO.rglob("*"):
        if ".git" in p.parts or not p.is_file() or p.suffix not in (".md", ".py", ".sh", ".txt", ".json", ".env"):
            continue
        if wrong in p.read_text(encoding="utf-8", errors="replace"):
            hits.append(str(p.relative_to(REPO)))
    assert hits == [], hits
