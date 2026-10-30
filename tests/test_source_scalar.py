"""#267: a free-text meta value is ONE string, whatever punctuation it carries, and a card whose `source` is not a
string is refused at the door.

The night writes `source: journal/D.md:12 — "a tool, D: 5 of 22 rows"` on its own meta line. That line is not a valid
YAML block (a second `: ` on a plain scalar), so the converter fell back to `yaml.safe_load` of the bare value, and
YAML read `X: Y` as a one-entry MAPPING. The .approve.md then carried `source: {'journal/D.md:12 — "a tool, D': ...}`,
which is valid YAML, so the converter's self-check, `approve_inbox.py --dry-run` and `build_views.py` all stayed green.
Fixed at the source: the converter takes a free-text value as the raw line text (a value written as ONE YAML quoted
scalar is unquoted, nothing else is re-parsed), and the one per-card gate (`build_views.card_problems`, run by the
converter's self-check, approve_inbox and build_views) refuses a non-string `source`, naming the card and the field.

Synthetic, fictional fixtures only. Each acceptance has a sibling in the dimension the defect lives in: a value with no
colon, a value already written as a quoted YAML scalar, and the compound annotation line that was already safe.
"""
import os
import shutil
import subprocess
import sys
import tempfile

import pytest
import yaml

from test_convert_inbox2 import TAGS, load

TOOLS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools")

COLON = 'journal/2099-01-01.md:12 — "probe sweep, 2099-01-01: 5 of 22 rows"'
QUOTED = 'journal/2099-01-01.md:17 — "K-90001 vs K-90002: K-90001 (\\"vents stay shut\\") is overtaken"'
PLAIN = "journal 2099-01-01"

UNIT_CARD = """### {cid}: the probe swap note {cid}
type: fact
level: observation
tags: [khms, gotcha]
evidence: observed
scope: universal
source: {source}
date: 2099-01-01
WHAT: the note about the probe swap {cid} never reached the base.
RELATION: new — no card covers the probe swap {cid}.
**QUOTES:**
- src=journal :: the probe swap note {cid} never reached the base
"""


def _convert_cli(tmp_path, monkeypatch, sources):
    _ci, base, pat = load(tmp_path, monkeypatch)
    src = tmp_path / "2099-01-01.md"
    src.write_text("## Cards\n\n" + "\n".join(UNIT_CARD.format(cid=f"C{i + 1}", source=s)
                                             for i, s in enumerate(sources)), encoding="utf-8")
    env = dict(os.environ, KHMS_ROOT=str(base), KHMS_PATTERNS=str(pat))
    r = subprocess.run([sys.executable, os.path.join(TOOLS, "convert_inbox2.py"), str(src), "--quiet"],
                       capture_output=True, text=True, env=env, timeout=60)
    out = (tmp_path / "2099-01-01.approve.md").read_text(encoding="utf-8")
    return r, out


def _front_sources(out):
    """`source` of every card of an .approve.md, read back with YAML as approve_inbox reads the front matter."""
    blocks = out.split("---\n")[1::2]
    return [yaml.safe_load(b)["source"] for b in blocks]


# --- (1) converter: the value with its own `: ` (and one with quotes and an em dash) round-trips exact -----------
def test_a_source_with_its_own_colon_round_trips_as_one_string(tmp_path, monkeypatch):
    r, out = _convert_cli(tmp_path, monkeypatch, [COLON, QUOTED, PLAIN])
    assert r.returncode == 0, r.stdout + r.stderr
    assert _front_sources(out) == [COLON, QUOTED, PLAIN], out


def test_a_source_written_as_one_quoted_yaml_scalar_is_unquoted_not_kept_verbatim(tmp_path, monkeypatch):
    ci, _b, _p = load(tmp_path, monkeypatch)
    text = "## Cards\n\n" + UNIT_CARD.format(cid="C1", source="'journal/x.md line 3; \"probe: wet\"'")
    (meta, _t, _body), = ci.convert(text, "2099-01-01", ci.load_tags())[0]
    assert meta["source"] == 'journal/x.md line 3; "probe: wet"'
    # sibling: a value that only STARTS with a quote is not one scalar, so it is the raw text
    text = "## Cards\n\n" + UNIT_CARD.format(cid="C1", source="\"probe sweep\": journal/x.md:4")
    (meta, _t, _body), = ci.convert(text, "2099-01-01", ci.load_tags())[0]
    assert meta["source"] == "\"probe sweep\": journal/x.md:4"


def test_every_free_text_key_is_read_as_text_and_structured_keys_stay_yaml(tmp_path, monkeypatch):
    ci, _b, _p = load(tmp_path, monkeypatch)
    text = "## Cards\n\n" + UNIT_CARD.format(cid="C1", source=COLON).replace(
        "scope: universal", "scope: device: probe-7")
    (meta, _t, _body), = ci.convert(text, "2099-01-01", ci.load_tags())[0]
    assert (meta["source"], meta["scope"]) == (COLON, "device: probe-7")
    assert meta["tags"] == ["khms", "gotcha"]          # a flow list is still a list


def test_a_wrapped_source_keeps_its_continuation_line(tmp_path, monkeypatch):
    ci, _b, _p = load(tmp_path, monkeypatch)
    text = "## Cards\n\n" + UNIT_CARD.format(cid="C1", source=COLON + "\n  and the probe log: line 9 (helper #12)")
    (meta, _t, _body), = ci.convert(text, "2099-01-01", ci.load_tags())[0]
    assert meta["source"] == COLON + " and the probe log: line 9 (helper #12)"
    assert meta["date"] is not None and meta["tags"] == ["khms", "gotcha"]


def test_a_fenced_yaml_block_with_a_colon_source_is_read_as_text(tmp_path, monkeypatch):
    ci, _b, _p = load(tmp_path, monkeypatch)
    text = ("## Cards\n\n### C1: the probe swap note\n```yaml\ntype: fact\nlevel: observation\n"
            f"tags: [khms]\nsource: {COLON}\n```\n"
            "WHAT: the probe swap note never reached the base.\n"
            "**QUOTES:**\n- src=journal :: the probe swap note never reached the base\n")
    (meta, _t, _body), = ci.convert(text, "2099-01-01", ci.load_tags())[0]
    assert meta["source"] == COLON


def test_the_compound_annotation_line_was_already_safe_and_stays_so(tmp_path, monkeypatch):
    ci, _b, _p = load(tmp_path, monkeypatch)
    text = ("## Cards\n\n### C1: the probe swap note\n"
            f"type: fact · level: observation · tags: [khms] · source: {COLON}\n"
            "WHAT: the probe swap note never reached the base.\n"
            "**QUOTES:**\n- src=journal :: the probe swap note never reached the base\n")
    (meta, _t, _body), = ci.convert(text, "2099-01-01", ci.load_tags())[0]
    assert meta["source"] == COLON


# --- (2) the door: a card whose `source` is not a string is refused, naming the card and the field --------------
META = {"id": "K-90003", "type": "fact", "level": "observation", "status": "active", "tags": ["testing"],
        "scope": "universal", "evidence": "measured", "source": "journal/2099-01-01.md:3", "date": "2099-01-01",
        "links": {"supports": [], "supersedes": None}}


def test_card_problems_refuses_a_mapping_source(monkeypatch):
    monkeypatch.syspath_prepend(TOOLS)
    import build_views
    assert not build_views.card_problems(dict(META))[0]
    bad = dict(META, source={'journal/2099-01-01.md:12 — "probe sweep, 2099-01-01': '5 of 22 rows"'})
    problems = build_views.card_problems(bad)[0]
    assert any("'source'" in p and "not a string" in p for p in problems), problems
    # a mapping in an enum field must be refused too, not crash the set lookup
    problems = build_views.card_problems(dict(META, level={"observation": "x"}))[0]
    assert any("'level'" in p and "not a string" in p for p in problems), problems


APPROVE_FILE = """## Cards

---
id: C1
type: fact
level: observation
status: active
tags: [testing]
scope: universal
evidence: measured
source: {source}
date: 2099-01-01
links:
  derived_from: []
  supports: []
  contradicts: []
  supersedes: null
  refuted_by: []
---
the probe swap note never reached the base

the note about the probe swap never reached the base.
"""


@pytest.fixture
def root():
    r = tempfile.mkdtemp(prefix="srcscalar-")
    os.makedirs(os.path.join(r, "memory", "know"))
    os.makedirs(os.path.join(r, "memory", "views"))
    with open(os.path.join(r, "memory", "views", "tags.md"), "w", encoding="utf-8") as f:
        f.write(TAGS)
    os.makedirs(os.path.join(r, "state", "shared"))
    with open(os.path.join(r, "state", "shared", "next_id"), "w") as f:
        f.write("90500\n")
    yield r
    shutil.rmtree(r, ignore_errors=True)


def _approve(root, source, *flags):
    inbox = os.path.join(root, "2099-01-01.approve.md")
    with open(inbox, "w", encoding="utf-8") as f:
        f.write(APPROVE_FILE.format(source=source))
    return subprocess.run([sys.executable, os.path.join(TOOLS, "approve_inbox.py"), inbox, *flags],
                          capture_output=True, text=True, env=dict(os.environ, KHMS_ROOT=root), timeout=120)


def test_approve_dry_run_refuses_a_mapping_source(root):
    r = _approve(root, "{'journal/2099-01-01.md:12 — \"probe sweep, 2099-01-01': 5 of 22 rows\"}", "--dry-run")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "C1: 'source'" in r.stderr and "not a string" in r.stderr, r.stdout + r.stderr
    assert not os.listdir(os.path.join(root, "memory", "know"))
    # sibling: the same value as one quoted string passes the dry run
    r = _approve(root, yaml.safe_dump(COLON, allow_unicode=True).splitlines()[0], "--dry-run")
    assert r.returncode == 0, r.stdout + r.stderr


def test_build_views_refuses_a_stored_card_with_a_mapping_source(root):
    card = APPROVE_FILE.split("---\n", 1)[1].replace("id: C1", "id: K-90001")
    path = os.path.join(root, "memory", "know", "K-90001.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write("---\n" + card.format(source="{'journal/2099-01-01.md:12 — x': y}"))
    r = subprocess.run([sys.executable, os.path.join(TOOLS, "build_views.py")], capture_output=True, text=True,
                       env=dict(os.environ, KHMS_ROOT=root), timeout=120)
    assert r.returncode != 0 and "K-90001: 'source'" in (r.stdout + r.stderr), r.stdout + r.stderr
    with open(path, "w", encoding="utf-8") as f:
        f.write("---\n" + card.format(source="'journal/2099-01-01.md:12 — x: y'"))
    r = subprocess.run([sys.executable, os.path.join(TOOLS, "build_views.py")], capture_output=True, text=True,
                       env=dict(os.environ, KHMS_ROOT=root), timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
