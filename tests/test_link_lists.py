"""A link list is a list of ids: one element is ONE card id or ONE temp label (#241).

A list written without commas -- `supports: [K-90001 K-90002]` -- used to be read as ONE id,
"K-90001 K-90002", by the converter and by approve_inbox, and `approve_inbox.py --dry-run` passed it:
the card reached the base with an edge that points at nothing and the two real edges missing. Fixed at
the source: whitespace between ids is a separator wherever a link list is read (the converter's
`**LINKS:**` line and meta lines, approve_inbox's LINKS line and front matter), and an element that is
still not an id after the split is refused by the one per-card gate (build_views.card_problems) that
the converter's self-check, approve_inbox and build_views all run, naming the card and the field.

Synthetic fixtures only. Every acceptance has its sibling: the comma list is unchanged, and a garbage
element is refused rather than split into something that merely looks like an id.
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile

import pytest
import yaml

from test_convert_inbox2 import TAGS, load

TOOLS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools")
APPROVE = os.path.join(TOOLS, "approve_inbox.py")

APPROVE_CARD = """\
### {cid}: {title}

```yaml
id: {cid}
type: fact
level: observation
status: active
tags: [testing]
scope: universal
evidence: measured
source: 'journal/2099-01-01.md line 1'
date: 2099-01-01
links:
  derived_from: []
  supports: {supports}
  contradicts: []
  supersedes: null
  refuted_by: []
---
{claim}

**QUOTES:**
- src=journal :: {claim}
```
"""


def approve_root():
    root = tempfile.mkdtemp(prefix="linklists-")
    os.makedirs(os.path.join(root, "memory", "know"))
    os.makedirs(os.path.join(root, "memory", "views"))
    with open(os.path.join(root, "memory", "views", "tags.md"), "w", encoding="utf-8") as f:
        f.write(TAGS)
    os.makedirs(os.path.join(root, "state", "shared"))
    with open(os.path.join(root, "state", "shared", "next_id"), "w") as f:
        f.write("90500\n")
    return root


@pytest.fixture
def root():
    r = approve_root()
    yield r
    shutil.rmtree(r, ignore_errors=True)


def run_approve(root, cards, *flags):
    inbox = os.path.join(root, "2099-01-01.approve.md")
    with open(inbox, "w", encoding="utf-8") as f:
        f.write("## Cards\n\n" + "\n".join(APPROVE_CARD.format(**c) for c in cards))
    env = dict(os.environ, KHMS_ROOT=root)
    return subprocess.run([sys.executable, APPROVE, inbox, *flags],
                          capture_output=True, text=True, env=env, timeout=120)


def written_links(root):
    out = {}
    know = os.path.join(root, "memory", "know")
    for name in sorted(os.listdir(know)):
        text = open(os.path.join(know, name), encoding="utf-8").read()
        meta = yaml.safe_load(re.match(r"^---\n(.*?)\n---\n", text, re.S).group(1))
        out[meta["id"]] = meta["links"]
    return out


C1 = dict(cid="C1", title="the vent sticks", claim="the vent sticks at RH 90.")


# --- (1) a whitespace-separated list is two links after approve -------------------------------------
def test_a_space_separated_list_becomes_two_links_after_approve(root):
    r = run_approve(root, [dict(C1, supports="[K-90001 K-90002]")])
    assert r.returncode == 0, r.stdout + r.stderr
    (links,) = written_links(root).values()
    assert links["supports"] == ["K-90001", "K-90002"]


def test_a_space_separated_temp_label_is_remapped_too(root):
    r = run_approve(root, [dict(C1, supports="[C2 K-90001]"),
                           dict(cid="C2", title="the cap", claim="a vented cap stops condensation.",
                                supports="[]")])
    assert r.returncode == 0, r.stdout + r.stderr
    links = written_links(root)
    assert links["K-90500"]["supports"] == ["K-90501", "K-90001"]


def test_the_converter_splits_a_space_separated_links_line(tmp_path, monkeypatch):
    ci, _b, _p = load(tmp_path, monkeypatch)
    text = ("## Cards\n\n### C1: the vent sticks\nSYMPTOM: the vent sticks at RH 90.\n"
            "**LINKS:** supports=[K-90001 K-90002] derived_from=[C7]\n"
            "**QUOTES:**\n- src=journal :: the vent sticks at RH 90\n")
    (meta, _t, _b2), = ci.convert(text, "2099-01-01", ci.load_tags())[0]
    assert meta["links"]["supports"] == ["K-90001", "K-90002"]
    assert meta["links"]["derived_from"] == ["C7"]
    # the converter's own edge audit reads the same split, so it reports nothing lost
    problems, n_edges = ci.edge_audit(text, [(meta, "t", "b")])
    assert not problems and n_edges == 3, problems


def test_the_converter_splits_a_space_separated_meta_line(tmp_path, monkeypatch):
    ci, _b, _p = load(tmp_path, monkeypatch)
    text = ("## Cards\n\n### C1: the vent sticks\ntype: fact\nsupports: [K-90001 K-90002]\n"
            "SYMPTOM: the vent sticks at RH 90.\n**QUOTES:**\n- src=journal :: the vent sticks at RH 90\n")
    (meta, _t, _b2), = ci.convert(text, "2099-01-01", ci.load_tags())[0]
    assert meta["links"]["supports"] == ["K-90001", "K-90002"]


# --- (2) a garbage element is refused, naming the card and the field --------------------------------
def test_a_garbage_element_makes_approve_dry_run_refuse(root):
    r = run_approve(root, [dict(C1, supports="[K-90001 foo]")], "--dry-run")
    out = r.stdout + r.stderr
    assert r.returncode != 0, out
    assert re.search(r"C1: links\.supports: 'foo' is not a card id", out), out
    assert os.listdir(os.path.join(root, "memory", "know")) == []


def test_a_garbage_element_makes_the_real_approve_refuse_before_any_id(root):
    r = run_approve(root, [dict(C1, supports="[K-90001, see-above]")])
    assert r.returncode != 0
    assert "C1: links.supports: 'see-above'" in r.stdout + r.stderr
    assert os.listdir(os.path.join(root, "memory", "know")) == []
    assert open(os.path.join(root, "state", "shared", "next_id")).read().strip() == "90500"


def test_the_converter_flags_a_garbage_element(tmp_path, monkeypatch):
    _ci, base, pat = load(tmp_path, monkeypatch)
    src = tmp_path / "2099-01-01.md"
    src.write_text("## Cards\n\n### C1: the vent sticks\nSYMPTOM: the vent sticks at RH 90.\n"
                   "RELATION: supports K-90001\n**LINKS:** supports=[K-90001 foo]\n"
                   "**QUOTES:**\n- src=journal :: the vent sticks at RH 90\n", encoding="utf-8")
    env = dict(os.environ, KHMS_ROOT=str(base), KHMS_PATTERNS=str(pat))
    r = subprocess.run([sys.executable, os.path.join(TOOLS, "convert_inbox2.py"), str(src), "--quiet"],
                       capture_output=True, text=True, env=env, timeout=60)
    report = (tmp_path / "2099-01-01.approve.report.txt").read_text(encoding="utf-8")
    assert r.returncode == 1 and "schema problem" in r.stderr, r.stdout + r.stderr
    assert "C1: links.supports: 'foo' is not a card id" in report
    row = next(ln for ln in report.splitlines() if ln.startswith("C1 "))
    assert "I" in row.split()[3], row


def test_build_views_refuses_a_space_joined_id_in_a_stored_card(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(TOOLS)
    import build_views
    meta = {"id": "K-90003", "type": "fact", "level": "observation", "status": "active",
            "tags": ["testing"], "scope": "universal", "evidence": "measured", "source": "s",
            "date": "2099-01-01", "links": {"supports": ["K-90001 K-90002"], "supersedes": None}}
    problems = build_views.card_problems(meta)[0]
    assert any("links.supports: 'K-90001 K-90002' is not a card id" in p for p in problems), problems
    meta["links"]["supports"] = ["K-90001", "K-90002"]
    assert not build_views.card_problems(meta)[0]


# --- (3) the normal comma list is unchanged ---------------------------------------------------------
def test_a_comma_list_is_unchanged(root):
    r = run_approve(root, [dict(C1, supports="[K-90001, K-90002]")])
    assert r.returncode == 0, r.stdout + r.stderr
    (links,) = written_links(root).values()
    assert links["supports"] == ["K-90001", "K-90002"]


def test_the_converter_keeps_a_comma_links_line(tmp_path, monkeypatch):
    ci, _b, _p = load(tmp_path, monkeypatch)
    text = ("## Cards\n\n### C1: the vent sticks\nSYMPTOM: the vent sticks at RH 90.\n"
            "**LINKS:** supports=[K-90001, K-90002]\n"
            "**QUOTES:**\n- src=journal :: the vent sticks at RH 90\n")
    cards, rows = ci.convert(text, "2099-01-01", ci.load_tags())
    assert cards[0][0]["links"]["supports"] == ["K-90001", "K-90002"]
    assert "I" not in rows[0][6]
