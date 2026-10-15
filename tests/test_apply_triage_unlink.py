"""apply_triage.py `unlink` verdict class (#242): the one sanctioned way to REMOVE a link from a card.

Until this class the tool could only add links and raise status, so a wrong link written by hand (a
`refuted_by` on a `challenged` card, against the spec's "refuted_by: filled when status→refuted") could never
be taken back by a tool. The verdict line names the field and the target:

    ROW n: APPLY unlink <field> <K-target> — <reason>

and removes exactly that link from the row's card — plus the mirror half of a symmetric `contradicts` edge —
and nothing else: status, the other links and the body stay byte-for-byte. A link that is not there is a
refusal of the whole run (non-zero, nothing written), never a silent no-op.

Isolated: a throwaway base passed with --brain, an empty selected base, no model, no network.
"""
import hashlib
import os
from pathlib import Path
import re
import subprocess
import sys

import pytest

yaml = pytest.importorskip("yaml")

TOOLS = Path(__file__).resolve().parents[1] / "tools"

CARDS = {
    # a challenged card carrying a refuted_by (the shape #242 cleans up), block-style links
    "K-90001": ("challenged", "links:\n  derived_from: []\n  supports: [K-90005]\n  contradicts: []\n"
                "  refuted_by: [K-90002]\n  supersedes: null"),
    "K-90002": ("active", "links:\n  derived_from: []\n  supports: []\n  contradicts: [K-90001]\n"
                "  refuted_by: []\n  supersedes: null"),
    # a symmetric contradicts edge across the two frontmatter styles
    "K-90003": ("active", "links: {derived_from: [], supports: [], contradicts: [K-90006, K-90004], "
                "supersedes: null, refuted_by: []}"),
    "K-90004": ("challenged", "links:\n  derived_from: []\n  supports: []\n  contradicts: [K-90003, K-90006]\n"
                "  refuted_by: []\n  supersedes: null"),
    # a refuted card whose one refuted_by may not be emptied by an unlink
    "K-90005": ("refuted", "links:\n  derived_from: []\n  supports: []\n  contradicts: []\n"
                "  refuted_by: [K-90006]\n  supersedes: null"),
    "K-90006": ("active", "links:\n  derived_from: []\n  supports: []\n  contradicts: [K-90003, K-90004]\n"
                "  refuted_by: []\n  supersedes: K-90007"),
    # a temp label that never became a card, in a whitespace-separated list (#241's splitter reads it)
    "K-90008": ("active", "links:\n  derived_from: []\n  supports: [K-90002 C12]\n  contradicts: [C41]\n"
                "  refuted_by: []\n  supersedes: null"),
}


def card_text(cid, status, links):
    return (f"---\nid: {cid}\ntype: fact\nlevel: observation\nstatus: {status}\ntags: [testing]\n"
            f"scope: universal\nevidence: measured\nsource: synthetic\ndate: 2099-01-01\n{links}\n---\n"
            f"{cid} synthetic body line one.\n\nSecond paragraph of {cid}, kept byte-for-byte.\n")


def make(tmp):
    selected = tmp / "selected"
    for d in ("memory/know", "memory/inbox", "state/shared/reviewed"):
        (selected / d).mkdir(parents=True)
    base = tmp / "base"
    for d in ("memory/know", "memory/inbox", "state/shared/reviewed"):
        (base / d).mkdir(parents=True)
    for cid, (status, links) in CARDS.items():
        (base / "memory/know" / f"{cid}.md").write_text(card_text(cid, status, links), encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith("KHMS_")}
    env.update(KHMS_ROOT=str(selected), KHMS_STATE=str(tmp / "agent"), KHMS_NO_SNAPSHOT="1",
               KHMS_READ_ONLY="0", PYTHONDONTWRITEBYTECODE="1")
    return base, env


def unit(base, rows, verdicts):
    p = base / "memory/inbox" / "2099-01-01-unlink-triage.md"
    p.write_text("# unlink review 2099-01-01\n\n" + "\n\n".join(rows)
                 + "\n\n## REVIEW VERDICT\n\n" + "\n".join(verdicts) + "\n", encoding="utf-8")
    return p


def run(triage, base, env, *extra):
    return subprocess.run([sys.executable, str(TOOLS / "apply_triage.py"), str(triage), "--brain", str(base),
                           *extra], env=env, text=True, capture_output=True, timeout=60)


def snapshot(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


def parts(base, cid):
    text = (base / "memory/know" / f"{cid}.md").read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
    return yaml.safe_load(m.group(1)), m.group(2)


def without(meta, field, target):
    out = yaml.safe_load(yaml.safe_dump(meta))
    out["links"][field] = [x for x in out["links"][field] if x != target]
    return out


def test_unlink_refuted_by_on_a_challenged_card_empties_it_and_keeps_status_and_body(tmp_path):
    base, env = make(tmp_path)
    before = {cid: parts(base, cid) for cid in CARDS}
    tri = unit(base, ["### K-90001 — action **UNLINK** (refuted_by K-90002)"],
               ["ROW 1: APPLY unlink refuted_by K-90002 — challenged is not refuted, spec 4.10"])
    r = run(tri, base, env, "--apply")
    assert r.returncode == 0, r.stdout + r.stderr
    meta, body = parts(base, "K-90001")
    assert meta["links"]["refuted_by"] == []
    assert meta["status"] == "challenged"
    assert meta == without(before["K-90001"][0], "refuted_by", "K-90002")      # nothing else moved
    assert body == before["K-90001"][1]
    for cid in CARDS:                    # refuted_by has no inverse field: every other card untouched
        if cid != "K-90001":
            assert parts(base, cid) == before[cid], cid
    report = tri.with_name(tri.stem + ".applied.md").read_text(encoding="utf-8")
    assert "challenged is not refuted, spec 4.10" in report        # the reason is in the history
    assert re.search(r"ROW 1: APPLY K-90001 .*unlink refuted_by K-90002", report), report
    assert "links.refuted_by -= K-90002" in report


def test_unlink_contradicts_removes_both_halves_and_nothing_else(tmp_path):
    base, env = make(tmp_path)
    before = {cid: parts(base, cid) for cid in CARDS}
    tri = unit(base, ["### K-90003 — action **UNLINK** (contradicts K-90004)"],
               ["ROW 1: APPLY — the two cards speak of different subjects"])     # class from the row
    r = run(tri, base, env, "--apply")
    assert r.returncode == 0, r.stdout + r.stderr
    m3, b3 = parts(base, "K-90003")
    m4, b4 = parts(base, "K-90004")
    assert m3 == without(before["K-90003"][0], "contradicts", "K-90004")
    assert m4 == without(before["K-90004"][0], "contradicts", "K-90003")
    assert m3["links"]["contradicts"] == ["K-90006"] and m4["links"]["contradicts"] == ["K-90006"]
    assert m4["status"] == "challenged"
    assert (b3, b4) == (before["K-90003"][1], before["K-90004"][1])
    for cid in ("K-90001", "K-90002", "K-90005", "K-90006"):
        assert parts(base, cid) == before[cid], cid


def test_dry_run_writes_no_card(tmp_path):
    base, env = make(tmp_path)
    know = snapshot(base / "memory/know")
    tri = unit(base, ["### K-90001 — action **UNLINK** (refuted_by K-90002)"],
               ["ROW 1: APPLY unlink refuted_by K-90002 — preview"])
    r = run(tri, base, env, "--dry-run")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "applied: 1" in r.stdout and "links.refuted_by -= K-90002" in r.stdout
    assert snapshot(base / "memory/know") == know


@pytest.mark.parametrize("row,verdict,why", [
    ("### K-90002 — action **UNLINK** (refuted_by K-90009)", "ROW 1: APPLY unlink refuted_by K-90009 — gone",
     "does not exist"),
    ("### K-90001 — action **UNLINK** (refuted_by K-90002)", "ROW 1: APPLY unlink contradicts K-90002 — x",
     "does not match"),
    ("### K-90006 — action **UNLINK** (supersedes K-90007)", "ROW 1: APPLY unlink supersedes K-90007 — x",
     "supersedes"),
    ("### K-90005 — action **UNLINK** (refuted_by K-90006)", "ROW 1: APPLY unlink refuted_by K-90006 — x",
     "refuted"),
])
def test_an_unlink_that_cannot_be_applied_is_refused_and_writes_nothing(tmp_path, row, verdict, why):
    base, env = make(tmp_path)
    tri = unit(base, [row], [verdict])
    before = snapshot(base)
    for mode in ("--apply", "--dry-run"):
        r = run(tri, base, env, mode)
        assert r.returncode != 0, (mode, r.stdout + r.stderr)
        assert "REFUSED" in r.stderr and why in r.stderr, r.stderr
        assert snapshot(base) == before                    # no card, no report, no review marker


def test_a_refusal_in_one_row_stops_the_valid_rows_too(tmp_path):
    base, env = make(tmp_path)
    tri = unit(base, ["### K-90001 — action **UNLINK** (refuted_by K-90002)",
                      "### K-90002 — action **UNLINK** (refuted_by K-90009)"],
               ["ROW 1: APPLY unlink refuted_by K-90002 — ok", "ROW 2: APPLY unlink refuted_by K-90009 — gone"])
    before = snapshot(base)
    r = run(tri, base, env, "--apply")
    assert r.returncode != 0 and "ROW 2" in r.stderr, r.stdout + r.stderr
    assert snapshot(base) == before


def test_unlink_takes_back_a_temp_label_that_never_became_a_card(tmp_path):
    base, env = make(tmp_path)
    before = {cid: parts(base, cid) for cid in CARDS}
    tri = unit(base, ["### K-90008 — action **UNLINK** (supports C12)",
                      "### K-90008 — action **UNLINK** (contradicts C41)"],
               ["ROW 1: APPLY unlink supports C12 — temp label never allocated",
                "ROW 2: APPLY unlink contradicts C41 — temp label never allocated"])
    r = run(tri, base, env, "--apply")
    assert r.returncode == 0, r.stdout + r.stderr
    meta, body = parts(base, "K-90008")
    assert meta["links"]["supports"] == ["K-90002"] and meta["links"]["contradicts"] == []
    assert body == before["K-90008"][1] and meta["status"] == "active"
    assert "mirror half: no card file for C41" in r.stdout
    for cid in CARDS:
        if cid != "K-90008":
            assert parts(base, cid) == before[cid], cid
