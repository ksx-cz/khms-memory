#!/usr/bin/env python3
"""Acceptance tests for the v3 know-how rule (#219, owner 2026-10-21): the base keeps know-how only.

Guarded properties:
  (1) V3 PROMPT: step 8 asks "mechanism or state" -- the keep kinds (when X then Y, a measurement with its
      conditions, a decision with its reason, a mistake with its measure, a rule, a refuted dead end) and the
      out kinds (where something is, a value, what is deployed, what a file does, progress), with examples of
      both; the v2 example reason ("it names a value a future agent would otherwise get wrong") is gone;
      PROMPT_VERSION is v3.
  (2) KNOW-HOW-ONLY MODE (`card_validity.py knowhow`): re-asks only the v3 question, with no tools, for cards
      judged under v1/v2 at their current hash and not yet under v3; each answer is one ledger row with
      prompt_version v3-knowhow and verdict not-judged (validity is not re-asked, no evidence); the earlier
      rows stay as they are; a "no" becomes a retire-candidate proposal, a "yes" none.
  (3) FULL RUN: `--n all` draws every eligible card; `--ids` judges named cards.
  (4) ARCHIVE CANDIDATES (`khms_archive_candidates.py`): a v3 "no" card cited by id from a git tree at a ref,
      from a configured file, from a live card's body or from a live card's links is "keep (cited)" with the
      citing place; the only live successor of a superseded card is kept; an uncited one is "archive"; a
      citation from another archive candidate does not count; v2 rows and rows at a stale hash are ignored.
Everything is built in a temporary directory; no model and no real base are touched.
"""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

yaml = pytest.importorskip("yaml")

TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))

import card_validity as CV  # noqa: E402


def card_text(cid, body, status="active", links="", typ="fact"):
    links = links or ("links:\n  derived_from: []\n  supports: []\n  contradicts: []\n  refuted_by: []\n"
                      "  supersedes: null")
    return (f"---\nid: {cid}\ntype: {typ}\nlevel: observation\nstatus: {status}\ntags: [testing]\n"
            f"scope: universal\nevidence: measured\nsource: synthetic\ndate: '2099-01-01'\n{links}\n---\n"
            f"{body}\n")


# ------------------------------------------------------------------------------------- (1) prompt

def test_v3_prompt_carries_the_rule_and_examples_of_both_kinds(tmp_path):
    assert CV.PROMPT_VERSION == "v3"
    (tmp_path / "proj").mkdir()
    card = CV.parse_card(card_text("K-00001", "The nav parameters live in nav.yaml."), "K-00001.md")
    full = CV.build_prompt(card, [], str(tmp_path))
    only = CV.build_knowhow_prompt(card)
    for p in (full, only):
        for needle in ("WHEN X THEN Y", "a measurement WITH its conditions", "a decision WITH its reason",
                       "a mistake WITH the measure", "a rule or a policy", "a refuted idea or a dead end",
                       "only a STATE", "where something is", "what a value or a setting is",
                       "what is deployed, installed", "what a file, a script or a tool does",
                       "progress status", 'Examples. "yes":', '"no": "The\n   nav parameters live'):
            assert needle in p, needle
        assert "it names a value a future agent would otherwise get wrong" not in p
    assert CV.ANSWER_EXAMPLE["knowhow_reason"] != "it names a value a future agent would otherwise get wrong"
    # the know-how-only prompt asks nothing about validity
    for needle in ("STEPS", "subclaims", "false_sentence", "cat, grep, head"):
        assert needle not in only, needle


# --------------------------------------------------------------------------- (2) know-how-only mode

FAKE_CLAUDE = r'''#!/usr/bin/env python3
import json, os, re, sys
prompt = sys.stdin.read()
log = os.environ["FAKE_CLAUDE_LOG"]
with open(log, "a") as fh:
    fh.write(json.dumps({"argv": sys.argv[1:], "prompt": prompt}) + "\n")
cid = re.search(r"THE CARD (K-\d+)", prompt).group(1)
kh = "yes" if "BECAUSE" in prompt else "no"
ans = {"card_id": cid, "knowhow": kh, "knowhow_reason": "fake reason for " + cid}
print(json.dumps({"result": json.dumps(ans), "total_cost_usd": 0.002, "is_error": False, "num_turns": 1}))
'''


@pytest.fixture
def know_and_ledger(tmp_path):
    know = tmp_path / "memory" / "know"
    know.mkdir(parents=True)
    for i, body in ((1, "The relay sits on bench three."), (2, "We chose X BECAUSE Y."),
                    (3, "The topic is /x."), (4, "Never judged."), (5, "Judged under v3 already.")):
        (know / f"K-0000{i}.md").write_text(card_text(f"K-0000{i}", body), encoding="utf-8")
    cards = {c["id"]: c for c in CV.load_cards(str(know))}
    ledger = tmp_path / "memory" / "ledger" / "validity.jsonl"
    v2 = lambda c, sha=None: {"id": c["id"], "sha256": sha or c["sha256"], "date": "2026-10-20", "judge": "sonnet",
                              "prompt_version": "v2", "verdict": "holds", "knowhow": "yes", "reason": "r",
                              "evidence": [{"command": "ls a", "exit_code": 0, "output_quote": "a"}]}
    rows = [v2(cards["K-00001"]), v2(cards["K-00002"]), v2(cards["K-00003"], sha="0" * 64),
            v2(cards["K-00005"]), dict(v2(cards["K-00005"]), prompt_version="v3", knowhow="no")]
    CV.append_ledger(str(ledger), rows)
    return know, ledger, cards


def test_knowhow_targets_are_the_v1_v2_cards_not_yet_asked_under_v3(know_and_ledger):
    know, ledger, cards = know_and_ledger
    got = [c["id"] for c in CV.knowhow_targets(list(cards.values()), CV.read_ledger(str(ledger)))]
    # K-00003's v2 row is at an old hash (eligible for a full draw instead), K-00004 was never judged,
    # K-00005 has a v3 row already
    assert got == ["K-00001", "K-00002"]


def test_knowhow_mode_writes_its_own_ledger_rows_and_does_not_rejudge_validity(know_and_ledger, tmp_path):
    know, ledger, cards = know_and_ledger
    fake = tmp_path / "fake_claude.py"
    fake.write_text(FAKE_CLAUDE, encoding="utf-8")
    fake.chmod(0o755)
    log = tmp_path / "calls.jsonl"
    os.environ["FAKE_CLAUDE_LOG"] = str(log)
    before = ledger.read_text(encoding="utf-8")
    out = tmp_path / "out"
    rc = CV.main(["knowhow", "--know", str(know), "--ledger", str(ledger), "--out", str(out), "--seed", "7",
                  "--n", "all", "--workers", "2", "--claude-bin", str(fake)])
    assert rc == 0
    text = ledger.read_text(encoding="utf-8")
    assert text.startswith(before)                                  # append-only: earlier rows untouched
    new = [json.loads(x) for x in text[len(before):].splitlines()]
    assert sorted(r["id"] for r in new) == ["K-00001", "K-00002"]
    by = {r["id"]: r for r in new}
    for r in new:
        assert r["prompt_version"] == CV.KNOWHOW_VERSION == "v3-knowhow"
        assert r["verdict"] == CV.NOT_JUDGED == "not-judged"        # validity not re-asked
        assert r["evidence"] == [] and r["false_sentence"] is None and r["suggested_path"] is None
        assert r["sha256"] == cards[r["id"]]["sha256"]
    assert (by["K-00001"]["knowhow"], by["K-00002"]["knowhow"]) == ("no", "yes")
    calls = [json.loads(x) for x in log.read_text().splitlines()]
    assert len(calls) == 2
    for c in calls:
        i = c["argv"].index("--tools")
        assert c["argv"][i + 1] == ""                               # no tools: the card is in the prompt
        assert "STEPS" not in c["prompt"] and "subclaims" not in c["prompt"]
    # the full draw still skips them (their current hash has rows), and a second know-how pass finds nothing
    assert "K-00001" not in {c["id"] for c in CV.eligible_cards(list(cards.values()), CV.read_ledger(str(ledger)))}
    assert CV.knowhow_targets(list(cards.values()), CV.read_ledger(str(ledger))) == []
    # proposals see it: a "no" is a retire candidate, a "yes" is no row
    res = CV.read_jsonl(str(out / "knowhow.jsonl"))
    row = CV.proposal_row("K-00001", res["K-00001"])
    assert row["route"] == "retire-candidate" and "not re-judged" in row["command"]
    assert CV.proposal_row("K-00002", res["K-00002"]) is None
    assert "not-judged=2" in CV.verdict_line(list(res.values()))
    assert CV.knowhow_line(list(res.values())).startswith("KNOWHOW judged=2 yes=1 no=1 unverified=0")


def test_a_malformed_knowhow_answer_goes_back_once_then_is_unverified(know_and_ledger):
    _know, _ledger, cards = know_and_ledger
    prompts = []

    def model(p):
        prompts.append(p)
        return '{"card_id": "K-00001", "knowhow": "maybe", "knowhow_reason": "x"}', {"cost_usd": 0.001}
    r = CV.judge_knowhow(cards["K-00001"], model, max_retries=1)
    assert r["verdict"] == "unverified" and r["attempts"] == 2
    assert "REJECTED" in prompts[1] and "knowhow must be" in prompts[1]


# ------------------------------------------------------------------------------------- (3) full run

def test_n_all_draws_every_eligible_card_and_ids_names_cards(tmp_path):
    assert CV.n_arg("all") is None and CV.n_arg("12") == 12
    pool = [{"id": f"K-{i:05d}"} for i in range(1, 40)]
    assert sorted(c["id"] for c in CV.select_uniform(pool, None, 3)) == [c["id"] for c in pool]
    f = tmp_path / "ids.txt"
    f.write_text("K-00002\nK-00001, K-00002\n")
    assert CV.ids_arg(str(f)) == ["K-00002", "K-00001"]
    assert CV.ids_arg("K-00003,K-00004") == ["K-00003", "K-00004"]


# ---------------------------------------------------------------------------- (4) archive candidates

def _git(repo, *a):
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@x"}
    return subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True, text=True, env=env)


def test_archive_candidates_keep_the_cited_and_list_the_uncited(tmp_path):
    home, root = tmp_path / "home", tmp_path / "base"
    home.mkdir()
    env = {"PATH": os.environ["PATH"], "HOME": str(home), "KHMS_NO_SNAPSHOT": "1", "PYTHONDONTWRITEBYTECODE": "1"}
    r = subprocess.run([str(TOOLS / "khms_init.sh"), str(root)], env=env, text=True, capture_output=True)
    assert r.returncode == 0, r.stderr
    L = lambda **kw: ("links:\n" + "".join(f"  {k}: {v}\n" for k, v in {
        "derived_from": "[]", "supports": "[]", "contradicts": "[]", "refuted_by": "[]", "supersedes": "null",
        **kw}.items())).rstrip("\n")
    cards = {
        "K-92001": card_text("K-92001", "The relay is on bench one."),                 # cited from the repo
        "K-92002": card_text("K-92002", "The relay is on bench two."),                 # cited in a live body
        "K-92003": card_text("K-92003", "The relay is on bench three."),               # linked from a live card
        "K-92004": card_text("K-92004", "The relay is on bench four. See K-92007."),   # uncited -> archive
        "K-92005": card_text("K-92005", "New gearbox state.\nSUPERSEDES K-92012 BECAUSE: x.",
                             links=L(supersedes="K-92012")),                           # only live successor
        "K-92006": card_text("K-92006", "The relay is on bench six."),                 # cited from a path
        "K-92007": card_text("K-92007", "The relay is on bench seven."),               # cited only by K-92004
        "K-92008": card_text("K-92008", "A v2-only card."),                            # v2 row: not a candidate
        "K-92009": card_text("K-92009", "A card changed since its answer."),           # stale hash
        "K-92010": card_text("K-92010", "When the relay sticks, see K-92002."),
        "K-92011": card_text("K-92011", "A lesson.", links=L(supports="[K-92003]")),
        "K-92012": card_text("K-92012", "Old gearbox state.", status="superseded",
                             links=L(superseded_by="K-92005")),
    }
    for cid, text in cards.items():
        (root / "memory/know" / f"{cid}.md").write_text(text, encoding="utf-8")
    import card_validity as CVm
    live = {c["id"]: c for c in CVm.load_cards(str(root / "memory/know"))}
    row = lambda cid, pv="v3-knowhow", kh="no", sha=None: {
        "id": cid, "sha256": sha or live[cid]["sha256"], "date": "2026-10-21", "prompt_version": pv,
        "verdict": "not-judged", "knowhow": kh, "knowhow_reason": f"only a state ({cid})"}
    ledger = root / "memory/ledger/validity.jsonl"
    CVm.append_ledger(str(ledger), [row(c) for c in ("K-92001", "K-92002", "K-92003", "K-92004", "K-92005",
                                                      "K-92006", "K-92007")]
                      + [row("K-92008", pv="v2"), row("K-92009", sha="f" * 64), row("K-92010", pv="v3", kh="yes")])
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "master")
    (repo / "docs").mkdir()
    (repo / "docs/design.md").write_text("intro\nthe bench rule (K-92001) holds\n")
    _git(repo, "add", "docs/design.md")
    _git(repo, "commit", "-qm", "doc")
    loader = tmp_path / "CLAUDE.md"
    loader.write_text("rule [[K-92006]]\n")
    out = tmp_path / "cand.md"
    env["KHMS_ROOT"] = str(root)
    r = subprocess.run([sys.executable, str(TOOLS / "khms_archive_candidates.py"), "--brain", str(root),
                        "--out", str(out), "--git", f"{repo}@master", "--path", str(loader)],
                       env=env, text=True, capture_output=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "CANDIDATES 7 keep=5 archive=2 stale-rows-skipped=1" in r.stdout, r.stdout
    table = {ln.split(" | ")[0].strip("| "): ln for ln in out.read_text().splitlines() if ln.startswith("| K-")}
    assert set(table) == {"K-92001", "K-92002", "K-92003", "K-92004", "K-92005", "K-92006", "K-92007"}
    assert "keep (cited)" in table["K-92001"] and "repo@master:docs/design.md:2" in table["K-92001"]
    assert "keep (cited)" in table["K-92002"] and "K-92010 body" in table["K-92002"]
    assert "keep (cited)" in table["K-92003"] and "K-92011 links.supports" in table["K-92003"]
    assert "keep (cited)" in table["K-92005"] and "only live successor" in table["K-92005"]
    assert "keep (cited)" in table["K-92006"] and f"{loader}:1" in table["K-92006"]
    assert "| archive |" in table["K-92004"]
    assert "| archive |" in table["K-92007"] and "cited only by archive candidates: K-92004" in table["K-92007"]
    assert "khms_archive.py --card K-92004 K-92007" in out.read_text()
    assert (root / "memory/know/K-92004.md").exists()                # nothing moved
