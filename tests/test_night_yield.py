"""night_yield.py (#237): $ per proposal and per finally accepted card, per stage, from what the night wrote.

A synthetic night: a log segment with served-by lines of both the old and the new pipeline, the units the night
published, the reviewer's approve files and receipts. The fixture varies what the numbers depend on: a second
night in the same log, a call without `$`, a unit approved, a unit closed with nothing approved, a unit still
pending, a candidate attributed by its MARKS ids against its unit name, and a card the same approval took from a
unit the night did not publish.
"""
import json
import os
import subprocess
import sys

TOOLS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools")

LOG = """2099-02-01T03:30:00+01:00 nightly start (night key 2099-02-01)
2099-02-01T03:31:00+01:00 sweep-1 served-by: claude-x (attempt 1/3, rc=0, $0.5000, 8 turns)
2099-02-01T03:32:00+01:00 sweep-2 served-by: UNKNOWN (attempt 1/3, rc=1, no JSON result)
2099-02-01T03:40:00+01:00 permark-M-0000000a served-by: claude-x (attempt 1/2, rc=0, $0.0300, 1 turns)
2099-02-01T03:41:00+01:00 permark-M-0000000b served-by: claude-x (attempt 1/2, rc=0, $0.0500, 1 turns)
2099-02-01T03:42:00+01:00 permark-G-0000000c served-by: claude-x (attempt 1/2, rc=0, $0.0400, 1 turns)
2099-02-01T03:50:00+01:00 marks-1 served-by: claude-x (attempt 1/2, rc=0, $1.0000, 60 turns)
2099-02-01T04:00:00+01:00 consolidate-2099-01-31-02 served-by: claude-x (attempt 1/2, rc=0, $0.8000, 40 turns)
2099-02-01T04:10:00+01:00 consolidate-2099-02-01-m01-01 served-by: claude-x (attempt 1/2, rc=0, $0.2000, 9 turns)
2099-02-01T04:20:00+01:00 nightly done
2099-02-02T03:30:00+01:00 nightly start (night key 2099-02-02)
2099-02-02T03:31:00+01:00 sweep-1 served-by: claude-x (attempt 1/3, rc=0, $9.0000, 8 turns)
"""


def cand(label, ids):
    return f"### {label}: a widget fact\nWHAT: x\nRELATION: new\n" + (f"MARKS: {ids}\n" if ids else "") + \
           "**QUOTES:**\n- src=journal :: x\n\n"


def approved(label, ids):
    return f"---\nid: {label}\ntype: fact\nmarks: [{ids}]\n---\nbody\n"


def fixture(tmp_path):
    root = tmp_path / "base"
    inbox = root / "memory/inbox"
    inbox.mkdir(parents=True)
    (root / "memory/ledger").mkdir(parents=True)
    pub = [("2099-02-01T04:11:00+01:00", "2099-02-01-m01-b01.md"),
           ("2099-02-01T04:11:00+01:00", "2099-02-01-m01-b00.md"),
           ("2099-02-01T04:12:00+01:00", "2099-01-31-b02.md"),
           ("2099-02-01T04:13:00+01:00", "2099-02-01-m01-b02.md"),
           ("2099-02-02T04:00:00+01:00", "2099-02-02-b01.md")]                     # the NEXT night's
    (root / "memory/ledger/published.jsonl").write_text(
        "".join(json.dumps({"ts": t, "batch": "x", "unit": u, "sha256": "0"}) + "\n" for t, u in pub))
    # unit 1 (evaluator): 2 journal-mark candidates + 1 generated-line candidate; 1 + 1 approved
    (inbox / "2099-02-01-m01-b01.md").write_text(
        "## Cards\n\n" + cand("M1001", "M-0000000a") + cand("M1002", "M-0000000b M-0000000d")
        + cand("M1003", "G-0000000c") + "## DROPPED (no valid RELATION)\n" + cand("M1004", "M-0000000e"))
    (inbox / "2099-02-01-m01-b01.approve.md").write_text(approved("M1001", "M-0000000a")
                                                        + approved("M1003", "G-0000000c"))
    (inbox / "2099-02-01-m01-b00.md").write_text("## MARKS NOT CARDED\n- M-0000000f :: not know-how: x\n")
    # unit 2 (old sweep, no MARKS lines): 3 candidates, review closed with nothing approved
    (inbox / "2099-01-31-b02.md").write_text("## Cards\n\n" + cand("C1", "") + cand("C2", "") + cand("C3", ""))
    # unit 3: pending
    (inbox / "2099-02-01-m01-b02.md").write_text("## Cards\n\n" + cand("M2001", "M-00000010"))
    (inbox / "2099-02-02-b01.md").write_text("## Cards\n\n" + cand("C1", ""))
    # a hand-made unit the same approval took a card from
    (inbox / "2099-02-01-x.md").write_text("## Cards\n\n" + cand("C9", ""))
    (inbox / "2099-02-01-x.approve.md").write_text(approved("C9", ""))
    appr = tmp_path / "approvals"
    appr.mkdir()
    (appr / "r1.json").write_text(json.dumps({"first": 1, "complete": True, "units": [
        str(inbox / "2099-02-01-m01-b01.md"), str(inbox / "2099-02-01-x.md")]}))
    (appr / "r2.json").write_text(json.dumps({"first": 9, "complete": False, "units": [
        str(inbox / "2099-02-01-m01-b02.md")]}))
    markers = tmp_path / "reviewed"
    markers.mkdir()
    (markers / ".reviewed-2099-01-31-b02-cards").write_text("sha256=0\n")
    log = tmp_path / "nightly.log"
    log.write_text(LOG)
    return root, log, appr, markers


def run(tmp_path, *extra):
    root, log, appr, markers = fixture(tmp_path)
    e = {k: v for k, v in os.environ.items() if not k.startswith("KHMS_")}
    return subprocess.run([sys.executable, os.path.join(TOOLS, "night_yield.py"), "2099-02-01", "--log", str(log),
                           "--root", str(root), "--approvals", str(appr), "--markers", str(markers), *extra],
                          env=e, capture_output=True, text=True, timeout=60)


def test_cost_proposals_and_accepted_cards_per_stage(tmp_path):
    r = run(tmp_path, "--json")
    assert r.returncode == 0, r.stderr
    got = json.loads(r.stdout)
    m, s, t = (got["stages"][k] for k in ("mark", "sweep", "total"))
    # $: the night's segment only (the next night's $9 is not in it)
    assert m["calls"] == 4 and round(m["usd"], 4) == 1.28            # permark M x2, marks-1, consolidate -m01-
    assert s["calls"] == 4 and round(s["usd"], 4) == 1.34 and s["nocost"] == 1
    # proposals: the trailer's DROPPED candidate is not one; the pending unit's proposal counts for $ per
    # proposal but not for the accepted/reviewed ratio, and its stage's $ per accepted card is not final
    assert (m["proposals"], m["reviewed"], m["accepted"], m["pending"]) == (3, 2, 1, 1)
    assert (s["proposals"], s["reviewed"], s["accepted"], s["pending"]) == (4, 4, 1, 0)   # 1 generated + 3 old
    assert (t["proposals"], t["reviewed"], t["accepted"]) == (7, 6, 2)
    assert round(t["usd_per_accepted"], 4) == round((1.28 + 1.34) / 2, 4) and not t["final"] and s["final"]
    assert round(m["usd_per_proposal"], 4) == round(1.28 / 3, 4) and round(s["ratio"], 4) == 0.25
    assert round(m["ratio"], 4) == 0.5
    assert got["accepted_outside_the_night"] == 1
    states = {u["unit"]: u["state"] for u in got["units"]}
    assert states == {"2099-02-01-m01-b01.md": "approved", "2099-01-31-b02.md": "closed",
                      "2099-02-01-m01-b02.md": "pending"}


def test_the_table_names_every_stage_and_the_total(tmp_path):
    r = run(tmp_path)
    assert r.returncode == 0, r.stderr
    rows = {ln.split()[0]: ln.split() for ln in r.stdout.splitlines()[2:5]}
    assert set(rows) == {"mark", "sweep", "total"}
    assert rows["total"][1:6] == ["8", "$2.62", "7", "2", "0.33"]
    assert rows["total"][7] == "$1.310*" and rows["sweep"][7] == "$1.340"
    assert "not counted: 1 card(s)" in r.stdout and "calls without $" in r.stdout


def test_an_unknown_night_is_said(tmp_path):
    root, log, appr, markers = fixture(tmp_path)
    r = subprocess.run([sys.executable, os.path.join(TOOLS, "night_yield.py"), "2099-03-01", "--log", str(log),
                        "--root", str(root), "--approvals", str(appr), "--markers", str(markers)],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode != 0 and "no `nightly start (night key 2099-03-01)`" in r.stderr
