"""source_repair.py (#244): put back the `source:` value the old converter cut at " #".

Until #239 `convert_inbox2.py` read a proposal's plain `source: journal/D.md:5 (agent #901)` line as YAML, and a
plain YAML scalar ends where ` #` starts a comment: the approved card got `source: journal/D.md:5 (agent`. The
proposal in the inbox still holds the whole value. The repair tool finds it, rewrites ONLY the card's
`source:` line (quoted the way the converter quotes it now) and leaves every other byte alone.

Isolated: a throwaway base passed with --brain, an empty selected base, no model, no network.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

import pytest

yaml = pytest.importorskip("yaml")

TOOLS = Path(__file__).resolve().parents[1] / "tools"

FULL = "journal/2099-01-01.md:5 (agent programmer #901)"
QUOTED_FULL = "journal/2099-01-01.md:7 (agent's run #906 / sess x1)"

# card id -> (title, the `source:` line as the card holds it, date)
CARDS = {
    # cut at " #": the proposal under the same title holds the whole value
    "K-90001": ("Synthetic cut card one", "source: journal/2099-01-01.md:5 (agent programmer", "2099-01-01"),
    # two proposals under the same title give two different originals: which one is unknowable
    "K-90002": ("Synthetic ambiguous card", "source: journal/2099-01-01.md:6 (agent runner", "2099-01-01"),
    # cut-looking, but no proposal anywhere carries an original
    "K-90003": ("Synthetic orphan card", "source: journal/2099-01-01.md:9 (agent investigator", "2099-01-01"),
    # a whole source, and the proposal holds the same value: nothing to do
    "K-90004": ("Synthetic intact card", "source: journal/2099-01-01.md:8 (agent librarian)", "2099-01-01"),
    # the reviewer edited the title; the original is found by source and date alone
    "K-90005": ("Synthetic retitled card", "source: journal/2099-01-01.md:11 (diblin", "2099-01-01"),
    # an apostrophe in the original: the quoting must still round-trip
    "K-90006": ("Synthetic quoting card", "source: journal/2099-01-01.md:7 (agent's run", "2099-01-01"),
    # already quoted (a repaired or hand-fixed card): never touched again
    "K-90007": ("Synthetic quoted card", "source: 'journal/2099-01-01.md:12 (agent tester #907)'", "2099-01-01"),
}

UNIT = """# ready output of batch 1 of run 2099-01-02 -- synthetic

## Cards

### M1: Synthetic cut card one
type: fact
level: observation
tags: [testing]
evidence: observed
source: journal/2099-01-01.md:5 (agent programmer #901)
date: 2099-01-01
WHAT: synthetic one.

### M2: Synthetic ambiguous card
type: fact
level: observation
tags: [testing]
evidence: observed
source: journal/2099-01-01.md:6 (agent runner #902)
date: 2099-01-01
WHAT: synthetic two.

### M4: Synthetic intact card
type: fact
level: observation
tags: [testing]
evidence: observed
source: journal/2099-01-01.md:8 (agent librarian)
date: 2099-01-01
WHAT: synthetic four.

### M5: Synthetic owner/reviewer retitled card
type: fact
level: observation
tags: [testing]
evidence: observed
source: journal/2099-01-01.md:11 (diblin #905)
date: 2099-01-01
WHAT: synthetic five.

### M6: Synthetic quoting card
type: fact
level: observation
tags: [testing]
evidence: observed
source: journal/2099-01-01.md:7 (agent's run #906 / sess x1)
date: 2099-01-01
WHAT: synthetic six.
"""

UNIT2 = """# ready output of batch 2 of run 2099-01-02 -- synthetic

## Cards

### M3: Synthetic ambiguous card
type: fact
level: observation
tags: [testing]
evidence: observed
source: journal/2099-01-01.md:6 (agent runner #903)
date: 2099-01-01
WHAT: synthetic three.
"""


def card_text(cid, title, source_line, date):
    return (f"---\nid: {cid}\ntype: fact\nlevel: observation\nstatus: active\ntags: [testing]\n"
            f"scope: project:x\nevidence: observed\n{source_line}\ndate: {date}\nlinks:\n  supports: []\n"
            f"  derived_from: []\n  contradicts: []\n  refuted_by: []\n  supersedes: null\n"
            f"marks: [M-0000000{cid[-1]}]\n---\n{title}\n\nWHAT: synthetic body of {cid}.\n")


def make(tmp):
    selected = tmp / "selected"
    for d in ("memory/know", "memory/inbox", "state/shared/reviewed"):
        (selected / d).mkdir(parents=True)
    base = tmp / "base"
    for d in ("memory/know", "memory/inbox", "memory/ledger", "state/shared/reviewed"):
        (base / d).mkdir(parents=True)
    for cid, (title, src, date) in CARDS.items():
        (base / "memory/know" / f"{cid}.md").write_text(card_text(cid, title, src, date), encoding="utf-8")
    (base / "memory/inbox" / "2099-01-02-b01.md").write_text(UNIT, encoding="utf-8")
    (base / "memory/inbox" / "2099-01-02-b02.md").write_text(UNIT2, encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith("KHMS_")}
    env.update(KHMS_ROOT=str(selected), KHMS_STATE=str(tmp / "agent"), KHMS_NO_SNAPSHOT="1",
               KHMS_READ_ONLY="0", PYTHONDONTWRITEBYTECODE="1")
    return base, env


def run(base, env, *extra):
    return subprocess.run([sys.executable, str(TOOLS / "source_repair.py"), "--brain", str(base), *extra],
                          env=env, text=True, capture_output=True, timeout=120)


def snapshot(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


def card(base, cid):
    return (base / "memory/know" / f"{cid}.md").read_text(encoding="utf-8")


def front(text):
    return yaml.safe_load(re.match(r"^---\n(.*?)\n---\n", text, re.S).group(1))


def test_dry_run_reports_and_writes_nothing(tmp_path):
    base, env = make(tmp_path)
    before = snapshot(base)
    r = run(base, env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert snapshot(base) == before
    out = r.stdout
    assert re.search(r"^WOULD-REPAIR K-90001 memory/inbox/2099-01-02-b01\.md:10 ", out, re.M), out
    assert re.search(r"^WOULD-REPAIR K-90005 .*by source\+date", out, re.M), out
    assert re.search(r"^WOULD-REPAIR K-90006 ", out, re.M), out
    assert re.search(r"^WOULD-REPAIR 3$", out, re.M), out
    assert re.search(r"^SKIPPED K-90002 .*2 different originals", out, re.M), out
    assert re.search(r"^SKIPPED K-90003 .*no original", out, re.M), out
    assert re.search(r"^SKIPPED 2$", out, re.M), out
    for cid in ("K-90004", "K-90007"):
        assert cid not in out, out


def test_apply_repairs_only_the_source_line(tmp_path):
    base, env = make(tmp_path)
    before = {cid: card(base, cid) for cid in CARDS}
    os.chmod(base / "memory/know" / "K-90001.md", 0o644)
    r = run(base, env, "--apply")
    assert r.returncode == 0, r.stdout + r.stderr
    assert re.search(r"^REPAIRED 3$", r.stdout, re.M), r.stdout
    for cid, full in (("K-90001", FULL), ("K-90005", "journal/2099-01-01.md:11 (diblin #905)"),
                      ("K-90006", QUOTED_FULL)):
        after = card(base, cid)
        assert front(after)["source"] == full                       # YAML reads the whole original back
        old_lines, new_lines = before[cid].split("\n"), after.split("\n")
        assert len(old_lines) == len(new_lines)
        diff = [i for i, (a, b) in enumerate(zip(old_lines, new_lines)) if a != b]
        assert len(diff) == 1 and new_lines[diff[0]].startswith("source: "), (cid, diff)
        want = dict(front(before[cid]), source=full)
        assert front(after) == want                                 # every other field equal
    for cid in ("K-90002", "K-90003", "K-90004", "K-90007"):        # skipped / intact / quoted: byte for byte
        assert card(base, cid) == before[cid], cid
    assert (base / "memory/know" / "K-90001.md").stat().st_mode & 0o777 == 0o644    # mode kept, too
    log = [json.loads(ln) for ln in (base / "memory/ledger/repair-244.jsonl").read_text().splitlines()]
    assert sorted(x["card"] for x in log) == ["K-90001", "K-90005", "K-90006"]
    row = next(x for x in log if x["card"] == "K-90001")
    assert row["unit"] == "memory/inbox/2099-01-02-b01.md" and row["line"] == 10
    assert row["new_source"] == FULL
    assert row["before_sha256"] == hashlib.sha256(before["K-90001"].encode()).hexdigest()
    assert row["after_sha256"] == hashlib.sha256(card(base, "K-90001").encode()).hexdigest()


def test_second_apply_is_a_no_op(tmp_path):
    base, env = make(tmp_path)
    assert run(base, env, "--apply").returncode == 0
    mid = snapshot(base)
    r = run(base, env, "--apply")
    assert r.returncode == 0, r.stdout + r.stderr
    assert re.search(r"^REPAIRED 0$", r.stdout, re.M), r.stdout
    assert snapshot(base) == mid


def test_inbox_files_that_are_not_proposals_are_not_read(tmp_path):
    """A converted `.approve.md` already holds the CUT value; a triage report quotes sources. Neither is an
    original -- only a raw proposal unit is."""
    base, env = make(tmp_path)
    (base / "memory/inbox" / "2099-01-02-b01.approve.md").write_text(
        "### M9: Synthetic orphan card\nsource: journal/2099-01-01.md:9 (agent investigator #909)\n",
        encoding="utf-8")
    r = run(base, env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert re.search(r"^SKIPPED K-90003 .*no original", r.stdout, re.M), r.stdout
