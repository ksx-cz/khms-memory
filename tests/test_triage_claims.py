"""#268: a card claim the triage judges, matches and tombstones is a whole sentence of the LIVE body.

The nightly contradiction triage maps the judge's quote of the losing card to one of the card's claims
(`khms_triage.split_claims` + `khms_triage_report.match_claim`); that claim is what the morning file lists as
"judged false" and what the proposed tombstone records. Two defects made it offer text that is not a claim of
the card at all:

  * the ERRATUM block that `khms_erratum.py apply` appends was split like any other text, so its retracted
    wording and its "It now reads: ..." line became candidate claims -- and, being short, they out-scored the
    live sentence they quote;
  * the body was split per PHYSICAL line, and cards are hard-wrapped, so a claim was the piece of a sentence
    that happened to sit on one line ("since abc1234), so a ..." -- starting mid-sentence).

The judge is shown the live text too: an ERRATUM block or an inline `[ERRATUM TS-...]` marker is not the card
asserting anything, and a quote that only the block holds is not in the card.

Synthetic cards only (K-9xxxx, TS-9xxxx, a fictional pump); no model, no network.
"""
import os
import sys

import pytest

TOOLS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools")

# A card after an applied dated-pointer erratum: the corrected sentence in the body carries the inline marker,
# the block at the end quotes the retracted wording and the corrected one, both hard-wrapped.
LIVE_FIRST = ("The pump controller stops the pump when the inlet pressure stays below the threshold for "
              "2.0 s (the override is pump.ros__parameters.inlet_timeout in config/pump.yaml, on main since "
              "abc1234), so a sensor dropout of about 1.5 s or more trips the pump as low_inlet and opens the "
              "bypass valve")
BODY = """The pump controller stops the pump when the inlet pressure stays below the threshold for
2.0 s (the override is pump.ros__parameters.inlet_timeout in config/pump.yaml, on main since
abc1234), so a sensor dropout of about 1.5 s or more trips the pump as low_inlet and opens the
bypass valve [ERRATUM TS-90001]

WHAT: The bypass valve opens only after the trip has been latched. The latch clears on a manual
reset, never on its own.

CZ: (the mirror line, in the operator's language) the controller stops the pump after 2.0 s, so
a dropout of about 1.5 s trips it as low_inlet.

ERRATUM TS-90001 (2026-01-02, coordinator, dated pointer): the sentence "On the test branches the
pump controller waits 5.0 s, so a sensor dropout of about 1.5 s never trips the pump as
low_inlet" was true when it was written; the value has since moved in the repo and lives in
pump.ros__parameters.inlet_timeout in config/pump.yaml, so the number was replaced by a pointer
to it. Record: git show main:config/pump.yaml -> 2.0. It now reads: "The pump controller stops
the pump when the inlet pressure stays below the threshold for 2.0 s (the override is
pump.ros__parameters.inlet_timeout in config/pump.yaml, on main since abc1234), so a sensor
dropout of about 1.5 s or more trips the pump as low_inlet and opens the bypass valve".
"""
BLOCK_START = BODY.index("ERRATUM TS-90001 (")


def _import(monkeypatch, tmp_path):
    for k in [k for k in os.environ if k.startswith("KHMS_")]:
        monkeypatch.delenv(k)
    base = tmp_path / "base"
    for d in ("memory/know", "memory/tombstones", "state/shared"):
        (base / d).mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("KHMS_ROOT", str(base))
    monkeypatch.syspath_prepend(TOOLS)
    for m in ("khms_contradict", "khms_paths", "khms_patterns", "khms_messages", "khms_search", "khms_lint",
              "khms_triage_gate", "khms_triage_report", "khms_triage", "khms_alloc"):
        sys.modules.pop(m, None)
    import khms_triage as T
    import khms_triage_report as R
    import khms_contradict as kc
    return T, R, kc


def _card(body=BODY):
    return {"id": "K-90440", "type": "fact", "level": "observation", "status": "active",
            "evidence": "measured", "date": "2026-01-01", "body": body.strip(), "first": body.splitlines()[0]}


def _flat(s):
    return " ".join(s.split())


# ---------------------------------------------------------------- (a) nothing comes from an ERRATUM block

def test_no_candidate_claim_comes_from_the_erratum_block(monkeypatch, tmp_path):
    T, _, _ = _import(monkeypatch, tmp_path)
    claims = T.split_claims(BODY)
    assert claims, "the live body has claims"
    for c in claims:
        assert c.span[0] < BLOCK_START, f"claim taken from the ERRATUM block: {c.text!r}"
        assert "It now reads" not in c.text
        assert "ERRATUM" not in c.text, f"an erratum tag or block reached a claim: {c.text!r}"
        assert "never trips" not in c.text, f"the retracted wording reached a claim: {c.text!r}"


def test_a_quote_of_the_corrected_wording_maps_to_the_live_sentence(monkeypatch, tmp_path):
    """The first 2026-10-30 shape: the judge quotes the live, corrected sentence; the shortest text sharing
    its words is the block's "It now reads" line, and that line used to win the match."""
    T, R, _ = _import(monkeypatch, tmp_path)
    card = _card()
    claims = T.split_claims(card["body"])
    hit = R.match_claim(claims, "a sensor dropout of about 1.5 s or more trips the pump as low_inlet")
    assert hit is not None, "the quote is in the live body; it must map to a claim"
    assert _flat(hit.text) == LIVE_FIRST
    res = T.triage_card(card, false_claim_idx=[hit.idx], refuted_by_evidence=True)
    assert [_flat(c.text) for c in res.false_claims] == [LIVE_FIRST]
    line, is_rec, _ts = T.propose_tombstone(card, claim=res.false_claims[0].text[:200], wrong="w",
                                            truth="K-90478", keys=["low_inlet"], today="2026-01-03",
                                            ledger=[], counter=str(tmp_path / ".next_ts_id"))
    claim_field = line.split('claim="', 1)[1].split('" | wrong=', 1)[0]
    assert "It now reads" not in claim_field and "ERRATUM" not in claim_field
    assert claim_field.startswith("The pump controller stops the pump")


RETRACTED = ("On the test branches the pump controller waits 5.0 s, so a sensor dropout of about 1.5 s never "
             "trips the pump")


def test_a_verdict_quoting_the_retracted_wording_is_not_in_the_card(monkeypatch, tmp_path):
    """The retracted sentence stands only inside the block: the card does not state it, so a verdict that
    quotes it is dropped by the quote guard before it can become a row, and no claim carries it."""
    T, _, kc = _import(monkeypatch, tmp_path)
    loser, winner = _card(), dict(_card("The pump never trips on a sensor dropout shorter than 3 s.\n"),
                                  id="K-90478")
    results = [{"a": loser["id"], "b": winner["id"],
                "verdict": {"contradiction": "yes", "quote_a": RETRACTED,
                            "quote_b": "The pump never trips on a sensor dropout shorter than 3 s"}}]
    counts = kc.integrity_filter(results, {c["id"]: c for c in (loser, winner)})
    assert counts["quote-not-in-card"] == 1 and results[0]["verdict"]["contradiction"] == "dropped"
    assert not any("never trips" in c.text or "test branches" in c.text for c in T.split_claims(BODY))


def test_the_judge_and_the_quote_guard_see_the_live_text_only(monkeypatch, tmp_path):
    _, _, kc = _import(monkeypatch, tmp_path)
    card = _card()
    blob = kc.card_blob(card, maxlen=100000)
    assert "It now reads" not in blob and "ERRATUM" not in blob
    assert "trips the pump as low_inlet" in blob
    text = kc.card_text(card)
    assert kc.quote_coverage(RETRACTED, text) < kc.QUOTE_COV_MIN
    assert kc.quote_coverage("so a sensor dropout of about 1.5 s or more trips the pump", text) == 1.0


# ---------------------------------------------------------------- (b) a claim is a whole sentence

def test_the_claim_is_the_whole_wrapped_sentence_not_its_line(monkeypatch, tmp_path):
    """The second 2026-10-30 shape: the claim began mid-sentence at a line break after "(... since <sha>)"."""
    T, R, _ = _import(monkeypatch, tmp_path)
    claims = T.split_claims(BODY)
    texts = [_flat(c.text) for c in claims if c.kind == "claim"]
    assert LIVE_FIRST in texts
    assert not any(t.startswith("abc1234)") or t.startswith("since abc1234") for t in texts)
    hit = R.match_claim(claims, "on main since abc1234), so a sensor dropout of about 1.5 s or more trips")
    assert hit is not None and _flat(hit.text) == LIVE_FIRST


def test_sentences_of_a_wrapped_field_and_the_cz_mirror(monkeypatch, tmp_path):
    T, _, _ = _import(monkeypatch, tmp_path)
    claims = T.split_claims(BODY)
    texts = [_flat(c.text) for c in claims if c.kind == "claim"]
    assert "WHAT: The bypass valve opens only after the trip has been latched." in texts
    assert "The latch clears on a manual reset, never on its own." in texts
    cz = [c for c in claims if c.kind == "cz"]
    assert len(cz) == 1 and _flat(cz[0].text).endswith("trips it as low_inlet.")
    assert not any(t.startswith("a dropout of about") for t in texts), "a wrapped CZ line became a claim"


def test_spans_point_at_the_claim_in_the_body(monkeypatch, tmp_path):
    T, _, _ = _import(monkeypatch, tmp_path)
    for c in T.split_claims(BODY):
        assert _flat(BODY[c.span[0]:c.span[1]].replace("[ERRATUM TS-90001]", "")) == _flat(c.text)


def test_a_spec_list_still_splits_on_its_commas(monkeypatch, tmp_path):
    """The motor-card rule stays: one wrong item of a vendor spec list is its own claim."""
    T, _, _ = _import(monkeypatch, tmp_path)
    body = ("Vendor-page specs: torque 100 N.m, supply 48 V, power 600 W, gear ratio 5, tyre 13x5-6 tubeless\n"
            "fat tire, connector 8-pin aviation.\n")
    texts = [_flat(c.text) for c in T.split_claims(body)]
    assert "tyre 13x5-6 tubeless fat tire" in texts
    assert "supply 48 V" in texts


QUOTED = """The relief valve opens at 3.2 bar on the bench rig and the gauge reads it within 0.1 bar.

**QUOTES:**
- src=journal :: the relief valve opens at 3.2 bar on the bench rig
- src=digest :: The gauge reads the relief valve within 0.1 bar.
"""


def test_the_quotes_tail_is_evidence_not_a_claim(monkeypatch, tmp_path):
    """A `**QUOTES:**` line repeats the body's words in a source's voice and is shorter than the body sentence;
    it must neither be a claim nor win the match over the sentence it backs (the reading of khms_lint)."""
    T, R, _ = _import(monkeypatch, tmp_path)
    claims = T.split_claims(QUOTED)
    assert [c.text for c in claims] == [
        "The relief valve opens at 3.2 bar on the bench rig and the gauge reads it within 0.1 bar."]
    hit = R.match_claim(claims, "the relief valve opens at 3.2 bar on the bench rig")
    assert hit is not None and hit.idx == 0


def test_a_prose_sentence_with_a_colon_and_commas_stays_whole(monkeypatch, tmp_path):
    """A field label or a long-clause sentence is not a spec list: cut on its commas it gave the reviewer a
    head like "Leaks do NOT all stop at once:" as the claim judged false."""
    T, R, _ = _import(monkeypatch, tmp_path)
    body = ("Leaks do NOT all stop at once: a seal with a scratched lip keeps weeping after the clamp is\n"
            "tightened, the drain plug seeps until its washer is replaced, and a hairline crack in the housing\n"
            "only shows at full pressure, so a dry floor after ten minutes proves nothing.\n\n"
            "FIX: tighten the clamp, replace the washer, pressure-test the housing, then wait an hour.\n")
    texts = [c.text for c in T.split_claims(body)]
    assert texts[0].startswith("Leaks do NOT all stop at once: a seal") and texts[0].endswith("proves nothing.")
    assert texts[1] == "FIX: tighten the clamp, replace the washer, pressure-test the housing, then wait an hour."
    hit = R.match_claim(T.split_claims(body), "Leaks do NOT all stop at once")
    assert hit is not None and hit.text == texts[0]


def test_an_inline_erratum_marker_ends_the_corrected_sentence(monkeypatch, tmp_path):
    """The corrected wording need not end with a full stop; the marker after it is the sentence boundary."""
    T, _, _ = _import(monkeypatch, tmp_path)
    body = ("The pump trips after 2.0 s on main [ERRATUM TS-90002] The bypass valve opens after the trip.\n\n"
            "ERRATUM TS-90002 (2026-01-02, coordinator): the sentence \"The pump trips after 5.0 s\" was wrong at\n"
            "the time of writing. It now reads: \"The pump trips after 2.0 s on main\".\n")
    assert [c.text for c in T.split_claims(body)] == ["The pump trips after 2.0 s on main",
                                                      "The bypass valve opens after the trip."]
