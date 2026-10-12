"""convert_inbox2.py counts every candidate it did not convert and says so (#232, K-02720's seventh instance).

2026-10-11 and 2026-10-12: a unit held candidates the converter did not turn into cards -- one under a mixed-case
`## Dropped` section the night's relation gate had filled (`<!-- dropped: malformed RELATION ... -->`), six behind
the same section with labels the strict form does not know (`C6A`) -- and both reports said `dropped: 0`, rc 0.
The reviewers found them only by counting headings by hand.

The fixtures vary the dimensions the defect lives in: the SECTION a candidate stands in (`## Cards`, `## Dropped`,
`## DROPPED (no valid RELATION)`, `## DEFERRED`), the LABEL shape (`C1`, `C6A`, `M52031+M53017`), and the clean
control (prose-only trailers, a plumbing heading that is not a label) that must stay rc 0 with nothing flagged.
"""
import os
import re
import subprocess
import sys

TOOLS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools")

TAGS = """# Tag registry

| tag | group | description | aliases | cards |
|---|---|---|---|---|
| khms | meta | the knowledge base | — | 2 |
| testing | process | tests | — | 5 |
| dev-process | process | how work is done (the converter's fallback tag) | — | 1 |
"""


def card(label, title, rel="RELATION: new — nearest K-00011 unrelated because it is about widgets"):
    return (f"### {label}: {title}\ntype: fact\nlevel: observation\ntags: [testing]\n\n"
            f"WHAT: {title}, measured once.\n{rel}\n**QUOTES:**\n- src=journal :: {title}\n\n")


CLEAN = ("## Cards\n\n" + card("C1", "first widget fact") + card("C2", "second widget fact")
         + "## Flagged\n- C2 is close to C1, kept apart.\n\n### not a card, plumbing prose\nnotes\n\n"
         + "## Dropped\n(none)\n\n## MARKS NOT CARDED\n(none)\n")

# 2026-10-12 m07-b01: the gate's drop sits under the consolidate's own mixed-case `## Dropped`
GATE_UNDER_DROPPED = ("## Cards\n\n" + card("C2", "kept widget fact")
                      + "## Flagged\n- nothing\n\n## Dropped\n(none)\n"
                      + "<!-- dropped: malformed RELATION: \"new — nearest K-00011 covers\" — expected ... -->\n"
                      + card("C1", "gate dropped widget fact", "RELATION: new — nearest K-00011 covers it")
                      + "## MARKS NOT CARDED\n(none)\n")

# 2026-10-11-2 b01: labels the strict form does not know, inside the cards region
ODD_LABELS = ("## Cards\n\n" + card("C5", "fifth widget fact") + card("C6A", "suffixed widget fact")
              + card("C7", "seventh widget fact") + card("M52031+M53017", "merged widget fact")
              + "## Dropped\n(none)\n")

# the night's own gate sections, upper case, and a DEFERRED one
GATE_SECTIONS = ("## Cards\n\n" + card("C1", "kept widget fact")
                 + "## DEFERRED\n\n<!-- deferred: over the cap of 1 -->\n" + card("C3", "deferred widget fact")
                 + "## DROPPED (no valid RELATION)\n\n<!-- dropped: no RELATION -->\n"
                 + card("C4", "relationless widget fact", "") + card("C5", "second relationless fact", ""))

ONLY_DROPPED = ("## Cards\n\n## Dropped\n<!-- dropped: malformed RELATION -->\n"
                + card("C1", "only candidate, dropped") + "## MARKS NOT CARDED\n(none)\n")


def convert(tmp_path, text, name="2099-01-02-b01.md"):
    base = tmp_path / "base"
    (base / "memory" / "views").mkdir(parents=True, exist_ok=True)
    (base / "memory" / "views" / "tags.md").write_text(TAGS, encoding="utf-8")
    pat = tmp_path / "patterns.env"
    pat.write_text("CONVERT_SCOPE_DEFAULT=project:widgets\n", encoding="utf-8")
    src = tmp_path / name
    src.write_text(text, encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith("KHMS_")}
    env.update(KHMS_ROOT=str(base), KHMS_PATTERNS=str(pat))
    r = subprocess.run([sys.executable, os.path.join(TOOLS, "convert_inbox2.py"), str(src), "--quiet"],
                       capture_output=True, text=True, env=env, timeout=60)
    rep = tmp_path / name.replace(".md", ".approve.report.txt")
    return r, (rep.read_text(encoding="utf-8") if rep.exists() else "")


def flags(text):
    return [ln for ln in text.splitlines() if "FLAG not converted" in ln]


def test_a_clean_unit_converts_everything_and_flags_nothing(tmp_path):
    r, rep = convert(tmp_path, CLEAN)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "2 cards" in rep and "candidate headings: 2, converted: 2, not converted: 0" in rep, rep
    assert not flags(rep) and not flags(r.stderr)


def test_a_candidate_under_a_mixed_case_dropped_section_is_counted_and_flagged(tmp_path):
    r, rep = convert(tmp_path, GATE_UNDER_DROPPED)
    assert "candidate headings: 2, converted: 1, not converted: 1" in rep, rep
    assert re.search(r"dropped: 1\b", rep), rep                      # was `dropped: 0`
    f = flags(rep)
    assert len(f) == 1 and "C1" in f[0] and "## Dropped" in f[0], f
    assert flags(r.stderr), r.stderr                                  # said on the terminal too
    assert r.returncode == 4, (r.returncode, r.stdout, r.stderr)     # output fine, a decision is owed


def test_labels_the_strict_form_does_not_know_are_flagged_as_absorbed_and_fail(tmp_path):
    r, rep = convert(tmp_path, ODD_LABELS)
    assert "candidate headings: 4, converted: 2, not converted: 2" in rep, rep
    f = flags(rep)
    assert len(f) == 2, f
    assert any("C6A" in ln and "absorbed into C5" in ln for ln in f), f
    assert any("M52031+M53017" in ln and "absorbed into C7" in ln for ln in f), f
    assert r.returncode == 1 and "NOT approve-ready" in r.stderr, (r.returncode, r.stderr)


def test_the_gates_own_sections_are_counted_in_every_spelling(tmp_path):
    r, rep = convert(tmp_path, GATE_SECTIONS)
    assert "deferred: 1, dropped: 2" in rep, rep
    assert "candidate headings: 4, converted: 1, not converted: 3" in rep, rep
    assert len(flags(rep)) == 3 and r.returncode == 4, (rep, r.returncode)


def test_a_unit_whose_only_candidate_was_dropped_says_so_instead_of_no_cards(tmp_path):
    r, _rep = convert(tmp_path, ONLY_DROPPED)
    assert r.returncode == 1 and "no cards found" in r.stderr, r.stderr
    f = flags(r.stderr)
    assert len(f) == 1 and "C1" in f[0] and "## Dropped" in f[0], r.stderr


def test_a_unit_with_no_candidate_at_all_stays_a_plain_no_cards(tmp_path):
    r, _rep = convert(tmp_path, "## Cards\n\n## MARKS NOT CARDED\n- M-0000abcd :: not know-how: status\n")
    assert r.returncode == 1 and "no cards found" in r.stderr and not flags(r.stderr), r.stderr
