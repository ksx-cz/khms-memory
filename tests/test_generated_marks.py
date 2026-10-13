"""#237 step 2: the generator's lines are committed by a 0-token gate and join the journal MARKs in ONE queue.

Generated lines are not journal MARKs: they live in memory/ledger/generated/, carry their own `G-` id space and
`origin: generated`, and are quoted with their own source label (`src=digest` / `src=gitlog`). Synthetic
fixtures only.
"""
import json
import os
import re
import subprocess
import sys

TOOLS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools")
sys.path.insert(0, TOOLS)

DIGEST = ("===== SESSION 0badcafe-1111 =====\n"
          "[2099-05-06T08:00] USER: the widget relay chatters below 11.5 V supply\n"
          "[2099-05-06T08:03] ASSISTANT: measured: relay drops out at 11.4 V, holds at 11.6 V\n")
GITLOG = "commit 1234567890abcdef1234567890abcdef12345678\nDate: 2099-05-06\n\n    widget relay: debounce 20 ms\n"


def lines():
    return [
        '- 2099-05-06T08:03Z MARK[measurement] relay drops out at 11.4 V (src=digest sess 0badcafe @08:03) '
        ':: "relay drops out at 11.4 V, holds at 11.6 V"',
        '- 2099-05-06T07:10Z MARK[decision] relay debounce set to 20 ms (src=gitlog 1234567890ab) '
        ':: "widget relay: debounce 20 ms"',
        '- 2099-05-06T08:00Z MARK[finding] relay chatters at 9 V (src=digest sess 0badcafe @08:00) '
        ':: "the relay chatters at 9 V"',                                                # not in the source
        '- 2099-05-06T08:00Z MARK[done] package finished (src=digest sess 0badcafe @08:00) '
        ':: "the widget relay chatters below 11.5 V supply"',                            # a status kind
        '- 2099-05-06T08:00Z MARK[finding] relay chatters, no span given',               # no label, no span
        '- 2099-05-06T08:03Z MARK[measurement] relay drops out at 11.4 V (src=digest sess 0badcafe @08:03) '
        ':: "relay drops out at 11.4 V, holds at 11.6 V"',                               # a repeat
    ]


def build(tmp_path, out):
    part = tmp_path / "part.md"
    part.write_text("\n".join(lines()) + "\n")
    (tmp_path / "digest.txt").write_text(DIGEST)
    (tmp_path / "git.txt").write_text(GITLOG)
    return subprocess.run([sys.executable, os.path.join(TOOLS, "generated_marks.py"), "build", "--out", str(out),
                           "--window", "2099-05-06T00:00:00+00:00 -> 2099-05-07T03:30:00+00:00",
                           "--source", f"digest={tmp_path / 'digest.txt'}", "--source", f"gitlog={tmp_path / 'git.txt'}",
                           "--rejects", str(tmp_path / "rejects.txt"), str(part)],
                          capture_output=True, text=True, timeout=60)


def test_the_gate_keeps_grounded_know_how_lines_once_and_says_why_it_rejects_the_rest(tmp_path):
    out = tmp_path / "base/memory/ledger/generated/2099-05-07.md"
    r = build(tmp_path, out)
    assert r.returncode == 0, r.stderr
    text = out.read_text()
    kept = re.findall(r"(?m)^- \S+Z MARK\[", text)
    assert len(kept) == 2, text
    assert text.index("debounce") < text.index("drops out"), "sorted by time"
    assert re.search(r"(?m)^WINDOW: 2099-05-07T03:30:00\+00:00$", text)
    rej = (tmp_path / "rejects.txt").read_text()
    for why in ("ungrounded", "status kind", "no source label and span", "duplicate"):
        assert why in rej, (why, rej)
    assert "2 line(s) committed" in r.stdout and "4 rejected" in r.stdout
    # a night key is never reused: the committed file is not rebuilt
    assert build(tmp_path, out).returncode == 3
    w = subprocess.run([sys.executable, os.path.join(TOOLS, "generated_marks.py"), "window", str(out.parent)],
                       capture_output=True, text=True)
    assert w.stdout.strip() == "2099-05-07T03:30:00+00:00"


def ledger(root, *args, tz="UTC"):
    e = {k: v for k, v in os.environ.items() if not k.startswith("KHMS_")}
    e.update(KHMS_ROOT=str(root), TZ=tz)
    return subprocess.run([sys.executable, os.path.join(TOOLS, "mark_ledger.py"), "--root", str(root), *args],
                          env=e, capture_output=True, text=True, timeout=60)


def base_with_both(tmp_path):
    root = tmp_path / "base"
    (root / "journal").mkdir(parents=True)
    (root / "memory/know").mkdir(parents=True)
    (root / "journal/2099-05-06.md").write_text(
        "- 07:30 MARK[finding] the widget relay coil is rated 12 V\n"
        "- 08:01 MARK[decision] keep the widget supply at 12 V or more\n"
        "- 09:00 MARK[done] status line, not tracked\n")
    assert build(tmp_path, root / "memory/ledger/generated/2099-05-07.md").returncode == 0
    return root


def test_generated_lines_enter_the_same_queue_time_sorted_in_their_own_id_space(tmp_path):
    root = base_with_both(tmp_path)
    r = ledger(root, "queue")
    assert r.returncode == 0, r.stderr
    q = [ln for ln in r.stdout.splitlines() if ln.startswith("- ")]
    order = [re.search(r"\| (journal|generated)/", ln).group(1) + ":" + ln.split(" | ")[1] for ln in q]
    # one time axis (TZ=UTC here, so the journal's local time equals the generated UTC time)
    assert order == ["generated:2099-05-06 07:10", "journal:2099-05-06 07:30", "journal:2099-05-06 08:01",
                     "generated:2099-05-06 08:03"], order
    assert all(ln.startswith("- G-") == ("| generated/" in ln) for ln in q)
    # in another zone the generated lines move with the clock, the journal does not
    r2 = ledger(root, "queue", tz="Europe/Prague")
    times = [ln.split(" | ")[1] for ln in r2.stdout.splitlines() if ln.startswith("- G-")]
    assert times == ["2099-05-06 09:10", "2099-05-06 10:03"], times
    s = ledger(root, "sync")
    assert s.returncode == 0, s.stderr
    summary = json.loads((root / "memory/ledger/summary.json").read_text())
    assert summary["generated_lines"] == {"total": 2, "open": 2} and summary["total"] == 4
    marks = [json.loads(ln) for ln in (root / "memory/ledger/marks.jsonl").read_text().splitlines()]
    gen = [m for m in marks if m["id"].startswith("G-")]
    assert len(gen) == 2 and all(m["origin"] == "generated" and m["utc"].endswith("Z") for m in gen)
    assert not re.search(r"G-[0-9a-f]{8}", (root / "journal/2099-05-06.md").read_text())


def test_a_generated_line_is_carded_and_dismissed_like_a_mark_but_under_its_own_id(tmp_path):
    root = base_with_both(tmp_path)
    q = ledger(root, "queue").stdout
    g1, g2 = re.findall(r"(?m)^- (G-[0-9a-f]{8}) ", q)
    card = "K-%05d" % 77
    (root / "memory/know" / f"{card}.md").write_text(
        f"---\nid: {card}\ntype: fact\nlevel: observation\nstatus: active\ntags: [power]\nmarks: [{g1}]\n---\n"
        "the relay drops out at 11.4 V\n")
    (root / "memory/inbox").mkdir(parents=True, exist_ok=True)
    unit = root / "memory/inbox/2099-05-07-m01-b00.md"
    unit.write_text(f"## MARKS NOT CARDED\n- {g2} :: not know-how: a tunable that lives in its file\n")
    d = ledger(root, "dismiss", "--from-inbox", str(unit))
    assert d.returncode == 0, d.stdout + d.stderr
    assert ledger(root, "sync").returncode == 0
    st = {m["id"]: m for m in map(json.loads, (root / "memory/ledger/marks.jsonl").read_text().splitlines())}
    assert st[g1]["state"] == "CARDED" and st[g1]["card"] == card
    assert st[g2]["state"] == "DISMISSED"
    assert not [ln for ln in ledger(root, "queue").stdout.splitlines() if ln.startswith("- G-")]
