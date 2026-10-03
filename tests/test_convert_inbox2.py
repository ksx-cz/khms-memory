"""convert_inbox2.py: a nightly/weekly inbox unit becomes approve-ready cards, and the conversion is loud.

Synthetic fixtures only (the deployment's own regression tests run against its real inbox units privately).
Every acceptance has a sibling that differs in the dimension the defect lives in and must not pass:
  * headings: both candidate-heading shapes the night writes (`### C1:` and `### C2 (from C8):`) are cards,
    plumbing headings are not (a silent zero is how 20 candidates once aged out unnoticed);
  * a compound annotation line (`type: X · level: Y · scope: S · tags: [...]`) is READ, not guessed;
  * a RELATION entry wrapped over several lines converts whole, whatever terminates it, and the
    converter's own audit fails the run when a field label it cannot see is swallowed;
  * MARK ids go to frontmatter (`marks:`), never into the body;
  * the base's tag vocabulary and scope names come from its patterns file, not from the code.
"""
import os
import subprocess
import sys

import pytest

TOOLS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools")

TAGS = """# Tag registry

| tag | group | description | aliases | cards |
|---|---|---|---|---|
| greenhouse | subsystem | climate control | — | 3 |
| sensor | device | any probe | probe | 4 |
| khms | meta | the knowledge base | — | 2 |
| testing | process | tests | — | 5 |
| gotcha | meta | traps | — | 1 |
| dev-process | process | how work is done (the converter's fallback tag) | — | 1 |
"""

PATTERNS = ("CONVERT_TAG_HINT_1=greenhouse::glass ?house\nCONVERT_TAG_HINT_2=sensor::(?-i)\\bRH\\b\n"
            "CONVERT_SCOPE_DEFAULT=project:greenhouse\nCONVERT_SCOPE_AGENT=agent:helper\n"
            "CONVERT_AGENT_TAGS=khms\n")


def load(tmp_path, monkeypatch, patterns=PATTERNS):
    """The converter imported fresh against a temporary base and patterns file."""
    base = tmp_path / "base"
    (base / "memory" / "views").mkdir(parents=True)
    (base / "memory" / "views" / "tags.md").write_text(TAGS, encoding="utf-8")
    pat = tmp_path / "patterns.env"
    pat.write_text(patterns, encoding="utf-8")
    monkeypatch.setenv("KHMS_ROOT", str(base))
    monkeypatch.setenv("KHMS_PATTERNS", str(pat))
    monkeypatch.syspath_prepend(TOOLS)
    for m in ("convert_inbox2", "khms_patterns", "khms_paths", "approve_inbox"):
        sys.modules.pop(m, None)
    import convert_inbox2 as ci
    return ci, base, pat


HEADINGS = """# nightly sweep — consolidated

## Cards

### C1: plain label
type: fact
body one

### C2 (from C8): one merged source
type: fact
body two

### C10 (from C46+C67+C89): several merged sources
type: fact
body three

### F1: the weekly's own label
type: fact
body four

### C25b: the review's split-card suffix
type: fact
body five

## Dropped

### not a card, plumbing prose
"""


def test_both_heading_shapes_are_cards_and_plumbing_is_not(tmp_path, monkeypatch):
    ci, _b, _p = load(tmp_path, monkeypatch)
    text = ci.split_trailers(HEADINGS)
    found = [(m.group(1), m.group(2)) for m in ci.CARD_RE.finditer(text)]
    assert [i for i, _ in found] == ["C1", "C2", "C10", "F1", "C25b"]
    titles = dict(found)
    assert titles["C2"] == "one merged source" and titles["C10"] == "several merged sources"
    assert all("###" not in m.group(3) for m in ci.CARD_RE.finditer(text))
    assert not ci.CARD_RE.search("### not a card, plumbing prose\nbody\n")   # it CAN reject


def test_a_compound_annotation_line_is_read_not_guessed(tmp_path, monkeypatch):
    ci, _b, _p = load(tmp_path, monkeypatch)
    text = ("## Cards\n\n### C1: humidity probe reads high after rain\n"
            "type: problem→solution · level: observation · evidence: measured · scope: device:rh-probe · "
            "tags: [sensor, testing] · date: 2099-01-01 · source: journal 2099-01-01\n"
            "SYMPTOM: the probe reads 100 % for an hour after rain.\nCAUSE: condensation on the filter.\n"
            "FIX: a vented cap.\n**QUOTES:**\n- src=journal :: the probe reads 100 % for an hour after rain\n")
    cards, rows = ci.convert(text, "2099-01-01", ci.load_tags())
    (meta, _t, body), = cards
    assert (meta["type"], meta["level"], meta["scope"]) == ("problem→solution", "observation", "device:rh-probe")
    assert meta["tags"] == ["sensor", "testing"]
    assert "T" not in rows[0][6]
    assert "type:" not in body and "level:" not in body
    # control: the same card without the line is guessed, and the guess is flagged for review
    plain = text.replace(text.split("\n")[3] + "\n", "")
    (pmeta, _t, _b), = ci.convert(plain, "2099-01-01", ci.load_tags())[0]
    assert pmeta["scope"] != "device:rh-probe"


WRAPPED = """## Cards

### C1: the cap fixes the reading
HOLDS: a vented cap stops condensation.
RELATION: supports K-90001 BECAUSE the earlier card measured the same
  hour-long saturation after rain, and this one names its
  mechanism.
CZ: a gloss line in the base's second language.
**QUOTES:**
- src=journal :: a vented cap stops condensation

### C2: the probe placement rule is replaced
DECIDED: probes hang under the roof ridge.
RELATION: supersedes K-90002 BECAUSE the ridge position was measured
  drier than the wall position, which turns an
  inferred claim into a verified one.

**QUOTES:**
- src=journal :: probes hang under the roof ridge
"""


def test_a_wrapped_relation_entry_converts_whole(tmp_path, monkeypatch):
    ci, _b, _p = load(tmp_path, monkeypatch)
    cards, _rows = ci.convert(WRAPPED, "2099-01-01", ci.load_tags())
    by = {m["id"]: (m, b) for m, _t, b in cards}
    m1, b1 = by["C1"]
    assert m1["links"].get("supports") == ["K-90001"]
    assert "mechanism." not in b1 and "hour-long saturation" not in b1     # no orphan continuation
    assert "a gloss line" in b1                                            # the terminator line is kept
    m2, b2 = by["C2"]
    assert m2["links"].get("supersedes") == "K-90002"
    assert "inferred claim into a verified one." in b2                     # the sentence is whole
    problems, n = ci.relation_audit(WRAPPED, cards)
    assert not problems and n == 2


def test_a_swallowed_field_label_fails_the_audit(tmp_path, monkeypatch):
    ci, _b, _p = load(tmp_path, monkeypatch)
    probe = WRAPPED.replace("  mechanism.\n", "  mechanism.\nDONE-CRITERIA: two dry weeks.\n")
    cards, _rows = ci.convert(probe, "2099-01-01", ci.load_tags())
    problems, _n = ci.relation_audit(probe, cards)
    assert any("SWALLOWED" in p or "LOST" in p for p in problems), problems


def test_marks_go_to_frontmatter_not_the_body(tmp_path, monkeypatch):
    ci, _b, _p = load(tmp_path, monkeypatch)
    text = ("## Cards\n\n### C1: a fact\nWHAT: the encoder drifts\nWHY: wear\nHOW TO APPLY: check\n"
            "MARKS: M-0000000a M-0000000b\nRELATION: supports K-90011\nCZ: x\n**QUOTES:**\n"
            "- src=journal :: the encoder drifts\n\n## MARKS NOT CARDED\n- M-0000000c :: dup\n")
    cards, _rows = ci.convert(text, "2099-01-01", ci.load_tags())
    assert len(cards) == 1
    meta, _title, body = cards[0]
    assert meta["marks"] == ["M-0000000a", "M-0000000b"]
    assert "MARKS:" not in body and "M-0000000c" not in body


CARD_GH = ("## Cards\n\n### C1: the glasshouse vent sticks\nSYMPTOM: the glass house vent sticks at RH 90.\n"
           "**QUOTES:**\n- src=journal :: the glass house vent sticks at RH 90\n")
CARD_AGENT = ("## Cards\n\n### C1: the knowledge base nightly skipped a unit\n"
              "SYMPTOM: the khms nightly skipped one inbox unit.\n"
              "**QUOTES:**\n- src=journal :: the khms nightly skipped one inbox unit\n")


def test_tag_vocabulary_and_scopes_come_from_the_patterns_file(tmp_path, monkeypatch):
    ci, _b, _p = load(tmp_path, monkeypatch)
    (m, _t, _b2), = ci.convert(CARD_GH, "2099-01-01", ci.load_tags())[0]
    assert {"greenhouse", "sensor"} <= set(m["tags"]) and m["scope"] == "project:greenhouse"
    (a, _t, _b3), = ci.convert(CARD_AGENT, "2099-01-01", ci.load_tags())[0]
    assert "khms" in a["tags"] and a["scope"] == "agent:helper"


def test_without_a_patterns_file_only_the_registry_speaks(tmp_path, monkeypatch):
    ci, _b, _p = load(tmp_path, monkeypatch, patterns="")
    (m, _t, _b2), = ci.convert(CARD_GH, "2099-01-01", ci.load_tags())[0]
    assert "greenhouse" not in m["tags"] and "sensor" not in m["tags"]     # the hints were data
    assert m["scope"] == "universal"


def test_the_cli_writes_approve_file_and_report_through_approve_inboxs_parser(tmp_path, monkeypatch):
    _ci, base, pat = load(tmp_path, monkeypatch)
    src = tmp_path / "2099-01-01.md"
    src.write_text(WRAPPED, encoding="utf-8")
    env = dict(os.environ, KHMS_ROOT=str(base), KHMS_PATTERNS=str(pat))
    r = subprocess.run([sys.executable, os.path.join(TOOLS, "convert_inbox2.py"), str(src), "--quiet"],
                       capture_output=True, text=True, env=env, timeout=60)
    out = tmp_path / "2099-01-01.approve.md"
    assert out.exists() and (tmp_path / "2099-01-01.approve.report.txt").exists(), r.stdout + r.stderr
    assert out.read_text(encoding="utf-8").count("\nid: C") == 2
    assert r.returncode == 0, r.stdout + r.stderr
    report = (tmp_path / "2099-01-01.approve.report.txt").read_text(encoding="utf-8")
    assert "parsed 2/2 cards" in report and "converted whole" in report
    # control: a fallback tag the registry does not hold is caught by approve_inbox's own parser
    (base / "memory" / "views" / "tags.md").write_text(TAGS.replace("| dev-process |", "| other |"), encoding="utf-8")
    r = subprocess.run([sys.executable, os.path.join(TOOLS, "convert_inbox2.py"), str(src), "--quiet"],
                       capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 1 and "schema problem" in r.stderr
    assert "not in the registry" in (tmp_path / "2099-01-01.approve.report.txt").read_text(encoding="utf-8")
