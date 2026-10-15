"""#240: a candidate pair the base has ALREADY decided never reaches the judge, and the morning file counts it.

The nightly contradiction triage sent the model pairs a review could only reject: one card of the pair was
already superseded / refuted / challenged, or the two cards were already joined by the edge the triage would
propose (`contradicts`, `refuted_by`). On three reviewed nights that was about a third of the rows. The
pre-model pair gate now refuses such a pair whichever card the model would call wrong, and the morning file
says how many pairs it filtered and why -- never silently.

The siblings (K-93309, an illustrative stand-in id: dropping EVERY linked pair loses real contradictions) stay with the judge: a pair joined
only by `derived_from`, and a pair where one card `supports` the other. The `supports` rule exists, measured
and OFF: in the reviewed history a winner that already `supports` its loser was APPLIED as a real refutation
(a follow-up card that corrects the one it builds on), so it is a row-level rule a deployment switches on with
KHMS_TRIAGE_GATE, never a default.

Synthetic cards only (K-9xxxx); no model, no network.
"""
import json
import os
import re
import subprocess
import sys

import pytest

TOOLS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools")


def _import(monkeypatch, tmp_path, **env):
    for k in [k for k in os.environ if k.startswith("KHMS_")]:
        monkeypatch.delenv(k)
    monkeypatch.setenv("KHMS_ROOT", str(tmp_path / "base"))
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.syspath_prepend(TOOLS)
    for m in ("khms_contradict", "khms_paths", "khms_patterns", "khms_messages", "khms_search",
              "khms_triage_gate", "khms_triage_report", "khms_triage"):
        sys.modules.pop(m, None)
    import khms_contradict as kc
    import khms_triage_gate as G
    return kc, G


def _card(cid, status="active", **links):
    lk = {"derived_from": [], "supports": [], "contradicts": [], "refuted_by": [], "supersedes": None}
    lk.update(links)
    return {"id": cid, "type": "fact", "level": "observation", "status": status, "evidence": "measured",
            "date": "2099-01-01", "tags": ["widget"], "links": lk,
            "first": f"{cid} the widget runs at some rate", "body": f"{cid}: the widget runs at some rate."}


# superseded loser · contradicts both ways · winner supports loser · derived_from only · live (unlinked)
CARDS = [
    _card("K-90001"), _card("K-90002", status="superseded"),
    _card("K-90011", contradicts=["K-90012"]), _card("K-90012", contradicts=["K-90011"]),
    _card("K-90021", supports=["K-90022"]), _card("K-90022"),
    _card("K-90041", derived_from=["K-90042"]), _card("K-90042"),
    _card("K-90031"), _card("K-90032"),
]
PAIRS = [("K-90001", "K-90002"), ("K-90011", "K-90012"), ("K-90021", "K-90022"),
         ("K-90041", "K-90042"), ("K-90031", "K-90032")]


def _sweep(kc, monkeypatch):
    """The sweep's pre-model path (gate, then the judge) with a judge stub that records what it was sent."""
    by_id = {c["id"]: c for c in CARDS}
    pairs = [{"a": a, "b": b, "score": 1.0, "shapes": [], "keys": []} for a, b in PAIRS]
    kept, acc = kc.gate_before_model(pairs, by_id)
    sent = []

    def judge(prompt, timeout=None):
        sent.append(prompt)
        raise OSError("judge stub: no model in this test")
    monkeypatch.setattr(kc, "_post", judge)
    monkeypatch.setattr(kc, "LLM_ATTEMPTS", 1)
    kc.adjudicate(kept, by_id, batch=1, verbose=False)
    judged = {tuple(sorted(set(re.findall(r"K-9\d{4}", p)))) for p in sent}
    return judged, acc


def test_only_undecided_pairs_reach_the_judge(tmp_path, monkeypatch):
    kc, _G = _import(monkeypatch, tmp_path)
    judged, acc = _sweep(kc, monkeypatch)
    assert ("K-90001", "K-90002") not in judged, "a pair with a superseded card reached the judge"
    assert ("K-90011", "K-90012") not in judged, "a pair already linked by contradicts reached the judge"
    # siblings: an undecided pair, a derived_from link and a one-way supports link still go to the judge
    assert {("K-90031", "K-90032"), ("K-90041", "K-90042"), ("K-90021", "K-90022")} <= judged, judged
    refused = {(a, b): set(rules) for a, b, rules in acc["refused_pairs"]}
    assert refused == {("K-90001", "K-90002"): {"loser-not-active", "winner-not-active"},
                       ("K-90011", "K-90012"): {"edge-exists"}}, refused


@pytest.mark.parametrize("status", ["superseded", "refuted", "challenged"])
def test_every_settled_status_of_either_card_refuses_the_pair(tmp_path, monkeypatch, status):
    kc, G = _import(monkeypatch, tmp_path)
    a, b = _card("K-90051"), _card("K-90052", status=status)
    assert G.pair_gate(a, b) and G.pair_gate(b, a), status
    assert not G.pair_gate(a, _card("K-90052")), "control: two active unlinked cards are judged"


@pytest.mark.parametrize("edge", ["contradicts-loser-side", "contradicts-winner-side", "refuted_by"])
def test_an_existing_edge_of_the_kind_the_triage_proposes_refuses_the_pair(tmp_path, monkeypatch, edge):
    kc, G = _import(monkeypatch, tmp_path)
    if edge == "contradicts-loser-side":
        a, b = _card("K-90061", contradicts=["K-90062"]), _card("K-90062")
    elif edge == "contradicts-winner-side":
        a, b = _card("K-90061"), _card("K-90062", contradicts=["K-90061"])
    else:  # refuted_by without the status (an inconsistent card) is still a decided pair
        a, b = _card("K-90061", refuted_by=["K-90062"]), _card("K-90062")
    hits = G.pair_gate(a, b)
    assert hits and {n for n, _ in hits} == {"edge-exists"}, hits


def test_winner_supports_loser_is_a_measured_rule_that_ships_off(tmp_path, monkeypatch):
    kc, G = _import(monkeypatch, tmp_path)
    winner, loser = _card("K-90071", supports=["K-90072"]), _card("K-90072")
    assert "winner-supports-loser" in G.RULES and "winner-supports-loser" not in G.enabled_rules()
    assert G.row_gate(loser, winner) == []
    on = G.row_gate(loser, winner, rules=["winner-supports-loser"])
    assert [n for n, _ in on] == ["winner-supports-loser"], on
    assert G.row_gate(winner, loser, rules=["winner-supports-loser"]) == [], "the rule is directional"


def test_card_readers_carry_the_links_the_rules_read(tmp_path, monkeypatch):
    kc, G = _import(monkeypatch, tmp_path)
    text = ("---\nid: K-90081\ntype: fact\nstatus: active\ndate: 2099-01-01\ntags: [widget]\nlinks:\n"
            "  derived_from: []\n  supports: [K-90082]\n  contradicts: [K-90083]\n  refuted_by: []\n"
            "  supersedes: null\n---\nbody\n")
    c = G.parse_card_text("K-90081", text)
    assert c["links"]["supports"] == ["K-90082"] and c["links"]["contradicts"] == ["K-90083"], c


def _base(tmp):
    root = tmp / "base"
    for d in ("memory/know", "memory/inbox", "memory/views", "state/shared", "journal"):
        (root / d).mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("KHMS_", "NIGHTLY_", "TRIAGE_"))}
    env.update(KHMS_ROOT=str(root), KHMS_STATE=str(tmp / "agent"), KHMS_READ_ONLY="1",
               KHMS_NO_SNAPSHOT="1", PYTHONDONTWRITEBYTECODE="1")
    return root, env


def test_the_morning_file_counts_what_the_gate_filtered_and_why(tmp_path, monkeypatch):
    kc, _G = _import(monkeypatch, tmp_path)
    by_id = {c["id"]: c for c in CARDS}
    pairs = [{"a": a, "b": b, "score": 1.0, "shapes": [], "keys": []} for a, b in PAIRS]
    _kept, acc = kc.gate_before_model(pairs, by_id)
    _root, env = _base(tmp_path)
    sweep = tmp_path / "sweep.json"
    sweep.write_text(json.dumps({"results": [], "meta": {"selection": {
        "candidate_pairs": 3, "adjudicated": 3, "not_adjudicated": 0, "gated_before_model": acc}}}))
    out = tmp_path / "report.md"
    r = subprocess.run([sys.executable, os.path.join(TOOLS, "khms_triage_report.py"), str(sweep),
                        "--no-subject-model", "--out", str(out)], env=env, text=True,
                       capture_output=True, timeout=60)
    assert r.returncode == 0, r.stderr
    text = out.read_text()
    line = [ln for ln in text.splitlines() if "filtered before the model" in ln]
    assert line, text
    assert "**2**" in line[0] and "loser not active: 1" in line[0] and "edge already exists: 1" in line[0], line
