"""The ways a tracked MARK leaves OPEN, end to end on a temporary base.

These are the ledger repairs of the coverage review (#209 in the reference deployment): every exit from
OPEN is a card or a recorded reason, never a model's unproven say-so.

  * FLOOR: memory/ledger/FLOOR names the first tracked journal day; --floor and $MARK_LEDGER_FLOOR win.
  * A dismissal without a reason is refused (it would be a silent loss).
  * "already carded K-x" is coverage only with a verbatim quote of K-x; no quote, a quote that is not in
    the card or a card that does not exist leaves the mark OPEN.
  * "duplicate of M-x" follows M-x: OPEN while M-x is OPEN, CARDED with M-x's card, judged on its own
    when M-x is dismissed.
  * Closing a unit's review records its NOT CARDED verdicts (the marks do not go back to the queue), the
    same way `dismiss --from-inbox` does, and a second pass adds nothing.
  * A verdict never turns a mark a card already holds into a dismissal.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))
import mark_ledger as ml  # noqa: E402

DAY = "2026-10-09"
FINDING = "the camera loses frames when the memory clock is pinned low, measured 3 of 5 runs"
DECISION = "we keep the ledger floor at 21 September because older marks were swept already"
DUPLICATE = "duplicate wording of the frame loss finding with the memory clock pinned low"
STATUS = "PKG1 finished and pushed"
MISTAKE = "a dismissal without a reason would be a silent loss of a mark"
CARD_SENTENCE = "The camera loses frames when the memory clock is pinned low (3 of 5 runs)."


def write_card(root, cid, body, marks=None):
    know = root / "memory" / "know"
    know.mkdir(parents=True, exist_ok=True)
    front = "---\nid: %s\nstatus: active\n" % cid
    if marks:
        front += "marks: [%s]\n" % ", ".join(marks)
    (know / (cid + ".md")).write_text(front + "---\n" + body + "\n", encoding="utf-8")


@pytest.fixture
def base(tmp_path):
    root = tmp_path / "base"
    (root / "journal").mkdir(parents=True)
    (root / "memory" / "inbox").mkdir(parents=True)
    (root / "journal" / "2026-07-01.md").write_text(
        "- 10:00 MARK[finding] an old July finding before any floor\n", encoding="utf-8")
    (root / "journal" / ("%s.md" % DAY)).write_text(
        "".join("- 09:%02d MARK[%s] %s\n" % (i * 10, k, t) for i, (k, t) in enumerate(
            [("finding", FINDING), ("decision", DECISION), ("finding", DUPLICATE),
             ("agent-done", STATUS), ("mistake", MISTAKE)])), encoding="utf-8")
    write_card(root, "K-00001", CARD_SENTENCE)
    return root


def paths_of(root):
    return ml.Paths(str(root), reviewed=str(root / "state" / "reviewed"))


def ids(root, floor=DAY):
    marks = ml.derive(paths_of(root), floor)
    by_text = {mk["text"]: mid for mid, mk in marks.items()}
    return {name: by_text[text] for name, text in (
        ("finding", FINDING), ("decision", DECISION), ("dup", DUPLICATE), ("status", STATUS),
        ("mistake", MISTAKE))}


def state(root, mid, floor=DAY):
    return ml.derive(paths_of(root), floor)[mid]


def cli(root, *args):
    env = dict(os.environ, KHMS_ROOT=str(root), PYTHONDONTWRITEBYTECODE="1")
    env.pop("MARK_LEDGER_FLOOR", None)
    return subprocess.run([sys.executable, str(TOOLS / "mark_ledger.py"), "--root", str(root), *args],
                          capture_output=True, text=True, env=env, timeout=60)


def unit(root, lines, name="2026-10-10-b00.md"):
    p = root / "memory" / "inbox" / name
    p.write_text("# night 2026-10-10 b00\n\n%s\n%s\n" % (ml.NOT_CARDED_HEAD, "\n".join(lines)),
                 encoding="utf-8")
    return p


def events(root):
    p = root / "memory" / "ledger" / "events.jsonl"
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []


# --- the floor ----------------------------------------------------------------------------------------
def test_the_floor_file_sets_the_first_tracked_day(base, monkeypatch):
    monkeypatch.delenv("MARK_LEDGER_FLOOR", raising=False)
    p = paths_of(base)
    assert ml.floor_of(p, None) == ml.DEFAULT_FLOOR
    assert len(ml.derive(p, ml.floor_of(p, None))) == 6          # July mark included without a floor
    os.makedirs(p.ledger, exist_ok=True)
    Path(p.floor_file).write_text("2026-09-21\n", encoding="utf-8")
    assert ml.floor_of(p, None) == "2026-09-21"
    texts = {mk["text"] for mk in ml.derive(p, ml.floor_of(p, None)).values()}
    assert "an old July finding before any floor" not in texts and FINDING in texts
    assert cli(base, "floor").stdout.strip() == "2026-09-21"
    monkeypatch.setenv("MARK_LEDGER_FLOOR", "2026-10-01")
    assert ml.floor_of(p, None) == "2026-10-01"                  # the environment outranks the file
    assert ml.floor_of(p, "2026-10-05") == "2026-10-05"          # and --floor outranks both


def test_a_malformed_floor_file_falls_back_to_the_default(base, monkeypatch):
    monkeypatch.delenv("MARK_LEDGER_FLOOR", raising=False)
    p = paths_of(base)
    os.makedirs(p.ledger, exist_ok=True)
    Path(p.floor_file).write_text("21. 9. 2026\n", encoding="utf-8")
    assert ml.floor_of(p, None) == ml.DEFAULT_FLOOR


# --- dismissal needs a reason ---------------------------------------------------------------------------
def test_a_dismissal_without_a_reason_is_refused_and_records_nothing(base):
    mid = ids(base)["mistake"]
    r = cli(base, "--floor", DAY, "dismiss", mid)
    assert r.returncode == 2, r.stdout + r.stderr
    assert "reason" in r.stdout
    assert events(base) == []
    assert state(base, mid)["state"] == "OPEN"
    r = cli(base, "--floor", DAY, "dismiss", mid, "--reason", "covered by the review rule")
    assert r.returncode == 0, r.stdout + r.stderr
    mk = state(base, mid)
    assert (mk["state"], mk["reason"]) == ("DISMISSED", "covered by the review rule")


# --- "already carded K-x" needs a quote of K-x -----------------------------------------------------------
def test_quote_in_card_needs_a_long_enough_verbatim_quote():
    assert ml.quote_in_card('"loses frames when the memory clock is pinned low"', CARD_SENTENCE)
    assert ml.quote_in_card("loses frames ... memory clock is pinned low", CARD_SENTENCE)
    assert not ml.quote_in_card("", CARD_SENTENCE)
    assert not ml.quote_in_card('"loses frames"', CARD_SENTENCE)               # under MIN_CARD_QUOTE
    assert not ml.quote_in_card('"loses frames when the network clock drifts"', CARD_SENTENCE)


@pytest.mark.parametrize("verdict,why", [
    ("already carded K-00001", "no quote"),
    ('already carded K-00001: "the camera never drops a frame at any memory clock"', "quote not in the card"),
    ('already carded K-00042: "loses frames when the memory clock is pinned low"', "no such card"),
])
def test_an_unproven_already_carded_verdict_leaves_the_mark_open(base, verdict, why):
    mid = ids(base)["finding"]
    r = cli(base, "--floor", DAY, "dismiss", "--from-inbox", str(unit(base, ["- %s :: %s" % (mid, verdict)])))
    assert r.returncode == 0, r.stdout + r.stderr
    assert why in r.stdout
    assert state(base, mid)["state"] == "OPEN"
    assert events(base) == []


def test_a_quoted_already_carded_verdict_cards_the_mark(base):
    mid = ids(base)["finding"]
    verdict = 'already carded K-00001: "loses frames when the memory clock is pinned low"'
    r = cli(base, "--floor", DAY, "dismiss", "--from-inbox", str(unit(base, ["- %s :: %s" % (mid, verdict)])))
    assert r.returncode == 0, r.stdout + r.stderr
    mk = state(base, mid)
    assert (mk["state"], mk["card"]) == ("CARDED", "K-00001")
    assert events(base)[0]["quote_verified"] is True


# --- "duplicate of M-x" follows M-x ---------------------------------------------------------------------
def test_a_duplicate_follows_its_target(base):
    m = ids(base)
    cli(base, "--floor", DAY, "dismiss", "--from-inbox",
        str(unit(base, ["- %s :: duplicate of %s" % (m["dup"], m["finding"])])))
    mk = state(base, m["dup"])
    assert mk["state"] == "OPEN" and mk["dup_of"] == m["finding"]          # the target is still OPEN
    write_card(base, "K-00002", "A card written later.", marks=[m["finding"]])
    mk = state(base, m["dup"])
    assert (mk["state"], mk["card"], mk["how"]) == ("CARDED", "K-00002", "dup")
    (base / "memory" / "know" / "K-00002.md").unlink()
    cli(base, "--floor", DAY, "dismiss", m["finding"], "--reason", "superseded by a later finding")
    mk = state(base, m["dup"])
    assert mk["state"] == "OPEN" and "judged on its own" in mk["reason"]   # never folded into a dismissal


# --- closing a unit records its verdicts -----------------------------------------------------------------
def test_closing_a_unit_review_records_its_not_carded_verdicts(base):
    m = ids(base)
    u = unit(base, ["- %s :: not know-how (status)" % m["status"],
                    "- %s :: duplicate of %s" % (m["dup"], m["finding"])])
    marker, recorded = ml.close_unit_review(paths_of(base), str(u), "reviewer", {"reason": "probe"},
                                            floor=DAY)
    assert os.path.exists(marker)
    assert {e["id"] for e in recorded} == {m["status"], m["dup"]}
    mk = state(base, m["status"])
    assert mk["state"] == "DISMISSED" and "not know-how (status)" in mk["reason"]
    assert state(base, m["dup"])["dup_of"] == m["finding"]
    n = len(events(base))
    r = cli(base, "--floor", DAY, "dismiss", "--from-inbox", str(u))          # the same verdicts again
    assert r.returncode == 0, r.stdout + r.stderr
    assert len(events(base)) == n                                             # add nothing


def test_mark_reviewed_cli_records_the_verdicts_before_the_marker(base):
    m = ids(base)
    u = unit(base, ["- %s :: not know-how (status)" % m["status"]])
    r = cli(base, "--floor", DAY, "mark-reviewed", str(u), "--reason", "nothing approved")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "recorded 1 NOT CARDED verdict" in r.stdout
    assert state(base, m["status"])["state"] == "DISMISSED"


def test_a_verdict_never_uncards_a_mark_a_card_holds(base):
    m = ids(base)
    write_card(base, "K-00003", "The decision card.", marks=[m["decision"]])
    u = unit(base, ["- %s :: not know-how (status)" % m["decision"]])
    _marker, recorded = ml.close_unit_review(paths_of(base), str(u), "reviewer", {}, floor=DAY)
    assert recorded == []
    mk = state(base, m["decision"])
    assert (mk["state"], mk["card"]) == ("CARDED", "K-00003")
