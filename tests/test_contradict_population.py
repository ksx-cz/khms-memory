"""Which pairs the contradiction sweep plans (#208, 2026-10-05).

  * an EMPTY `--since` window plans NO pair -- it is never widened to a full sweep (in the reference deployment
    an empty window silently swept the whole base twice while the log said `--since`); positive control: the
    same base without `--since` plans pairs;
  * `--skip-judged-dir`: a pair JUDGED (outcome OK) in a sweep JSON of the last `--skip-nights` nights is not
    planned again, unless one of its cards changed after that run started; a pair planned and NOT judged is
    planned again; today's own sweep files are never read.

Isolated: a temporary base of synthetic cards, the real tool in `--pairs-only` mode. No model, no network.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

TOOLS = Path(__file__).resolve().parents[1] / "tools"
WORDS = ["flux capacitor", "zorbium coil", "quantel lattice", "brimwire spindle"]


def make_base(tmp):
    root = tmp / "base"
    for d in ("memory/know", "memory/inbox/.staging", "memory/views", "state/shared", "journal"):
        (root / d).mkdir(parents=True, exist_ok=True)
    old = time.time() - 30 * 86400
    for n in range(1, 7):
        cid = "K-%05d" % (90100 + n)
        w = WORDS[n % 2]
        body = (f"The {w} runs at {10 * n} hertz on the grommet rig; the {w} was measured on the bench "
                f"and the {w} reading is {n} volts.")
        p = root / "memory/know" / f"{cid}.md"
        p.write_text(f"---\nid: {cid}\ntype: fact\nlevel: observation\nstatus: active\ntags: [grommet]\n"
                     f"date: '2099-01-01'\n---\n{body}\n")
        os.utime(p, (old, old))
    return root


def plan(root, tmp, *args):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("KHMS_", "NIGHTLY_", "TRIAGE_"))}
    env.update(KHMS_ROOT=str(root), KHMS_STATE=str(tmp / "agent"), KHMS_READ_ONLY="1", KHMS_NO_SNAPSHOT="1",
               PYTHONDONTWRITEBYTECODE="1")
    r = subprocess.run([sys.executable, str(TOOLS / "khms_contradict.py"), "--pairs-only", "--limit", "50",
                        *args], env=env, text=True, capture_output=True, timeout=120)
    assert r.returncode == 0, r.stderr
    return {tuple(sorted((p["a"], p["b"]))) for p in json.loads(r.stdout)}, r.stderr


def test_empty_since_window_plans_no_pair(tmp_path):
    root = make_base(tmp_path)
    full, err = plan(root, tmp_path)
    assert full, ("positive control: the synthetic base must pair without a window", err)
    assert "mode: full" in err, err
    empty, err = plan(root, tmp_path, "--since", "2999-01-01")
    assert empty == set(), (len(empty), err)
    assert "window holds no card" in err, err


def test_pairs_judged_in_recent_nights_are_not_planned_again(tmp_path):
    root = make_base(tmp_path)
    st = root / "memory/inbox/.staging"
    full, _ = plan(root, tmp_path)
    assert len(full) >= 3, full
    judged, unjudged, changed = sorted(full)[:3]
    started = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - 3 * 86400))
    rows = [{"a": judged[0], "b": judged[1], "verdict": {"outcome": "OK"}},
            {"a": unjudged[0], "b": unjudged[1], "verdict": {"outcome": "DEADLINE"}},
            {"a": changed[0], "b": changed[1], "verdict": {"outcome": "OK"}}]
    (st / "triage-2099-01-09-sweep.json").write_text(json.dumps(
        {"meta": {"progress": {"started": started}}, "results": rows}))
    (st / "triage-2099-01-10-sweep.json").write_text(json.dumps(     # today: never read
        {"meta": {"progress": {"started": started}}, "results": [
            {"a": p[0], "b": p[1], "verdict": {"outcome": "OK"}} for p in full]}))
    os.utime(root / "memory/know" / f"{changed[1]}.md")             # edited after that night
    skip = ("--skip-judged-dir", str(st), "--skip-nights", "7", "--today", "2099-01-10")
    got, err = plan(root, tmp_path, *skip)
    assert judged not in got and unjudged in got and changed in got, (judged, unjudged, changed, err)
    assert got == full - {judged}, err
    assert "skipped 1 candidate pairs" in err, err
    off, _ = plan(root, tmp_path, "--skip-judged-dir", str(st), "--skip-nights", "0", "--today", "2099-01-10")
    assert off == full                                              # positive control: 0 nights = no skip
