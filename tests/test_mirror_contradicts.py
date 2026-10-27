"""`contradicts` is symmetric (spec §4.10): a one-sided edge fails the build, and one tool closes it.

A card written with `contradicts: [K-old]` used to leave K-old silent: nothing but apply_triage ever wrote
the back half, and build_views symmetrised the edge for belief only, so a reader of the OLD card never saw
its correction and the build stayed green. In the reference deployment a base-wide census found 153 such
edges (and 66 refute edges whose back half is `refuted_by`, which is closed by design: apply_triage's
`refute` writes winner.contradicts += loser, loser.refuted_by += winner).

  * build_views FAILS on a one-sided edge, naming both cards and the fix command;
  * `apply_triage.py --mirror-contradicts` adds exactly the missing halves -- dry run by default, every
    edge printed, idempotent, only the one link-list line of the target card moves;
  * approve_inbox closes the halves of the cards it approves, so a morning approval stays green.

Synthetic cards only (K-93xxx band), a throwaway base made by khms_init.sh, no model, no network.
"""
import difflib
import hashlib
import os
from pathlib import Path
import re
import subprocess
import sys

import pytest

yaml = pytest.importorskip("yaml")

TOOLS = Path(__file__).resolve().parents[1] / "tools"

BLOCK = "links:\n  derived_from: []\n  supports: []\n  contradicts: {c}\n  refuted_by: {r}\n  supersedes: null"
INLINE = "links: {{derived_from: [], supports: [], contradicts: {c}, supersedes: null, refuted_by: []}}"

CARDS = {
    # one-sided, block-style target with an empty list
    "K-93001": ("active", BLOCK.format(c="[K-93002, K-93003]", r="[]")),
    "K-93002": ("active", BLOCK.format(c="[]", r="[]")),
    # already mirrored (inline style): nothing to do
    "K-93003": ("active", INLINE.format(c="[K-93001]")),
    # one-sided, inline-style target without a contradicts key at all
    "K-93004": ("active", INLINE.format(c="[K-93005]")),
    "K-93005": ("active", "links: {derived_from: [], supports: [], supersedes: null, refuted_by: []}"),
    # a refute edge: the back half is refuted_by -- closed, never touched
    "K-93006": ("refuted", BLOCK.format(c="[]", r="[K-93007]")),
    "K-93007": ("active", BLOCK.format(c="[K-93006]", r="[]")),
    # the target lives in the archive (moved out of the base): not a know/ edge
    "K-93008": ("active", BLOCK.format(c="[K-93009]", r="[]")),
    # one-sided, block-style target without a contradicts key, and a target that already has others
    "K-93010": ("active", BLOCK.format(c="[K-93011, K-93012]", r="[]")),
    "K-93011": ("active", "links:\n  derived_from: []\n  supports: []\n  refuted_by: []\n  supersedes: null"),
    "K-93012": ("challenged", BLOCK.format(c="[K-93003]", r="[]")),
}
ARCHIVED = {"K-93009": ("superseded", BLOCK.format(c="[]", r="[]"))}
# (target, source) of every missing half in CARDS
MISSING = {("K-93002", "K-93001"), ("K-93005", "K-93004"), ("K-93011", "K-93010"),
           ("K-93012", "K-93010"), ("K-93003", "K-93012")}


def card_text(cid, status, links):
    return (f"---\nid: {cid}\ntype: fact\nlevel: observation\nstatus: {status}\ntags: [testing]\n"
            f"scope: universal\nevidence: measured\nsource: synthetic\ndate: 2099-01-01\n{links}\n---\n"
            f"{cid} is a synthetic card about a fictional weather vane.\n\nSecond paragraph of {cid}.\n")


def make(tmp, cards, archived=None):
    home, root = tmp / "home", tmp / "base"
    home.mkdir()
    env = {"PATH": os.environ["PATH"], "HOME": str(home), "KHMS_NO_SNAPSHOT": "1",
           "PYTHONDONTWRITEBYTECODE": "1"}
    r = subprocess.run([str(TOOLS / "khms_init.sh"), str(root)], env=env, text=True, capture_output=True)
    assert r.returncode == 0, r.stderr
    for cid, (status, links) in cards.items():
        (root / "memory/know" / f"{cid}.md").write_text(card_text(cid, status, links), encoding="utf-8")
    for cid, (status, links) in (archived or {}).items():
        (root / "memory/archive/know").mkdir(parents=True, exist_ok=True)
        (root / "memory/archive/know" / f"{cid}.md").write_text(card_text(cid, status, links),
                                                                encoding="utf-8")
    env["KHMS_ROOT"] = str(root)
    return root, env


def build(env):
    return subprocess.run([sys.executable, str(TOOLS / "build_views.py")], env=env, text=True,
                          capture_output=True, timeout=120)


def mirror(env, *extra):
    return subprocess.run([sys.executable, str(TOOLS / "apply_triage.py"), "--mirror-contradicts", *extra],
                          env=env, text=True, capture_output=True, timeout=120)


def know_bytes(root):
    return {p.name: p.read_bytes() for p in sorted((root / "memory/know").glob("K-*.md"))}


def split(raw):
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", raw.decode(), re.S)
    return yaml.safe_load(m.group(1)), m.group(2)


# --- the check ------------------------------------------------------------------------------------
def test_a_one_sided_contradicts_edge_fails_the_build_naming_both_cards_and_the_fix(tmp_path):
    root, env = make(tmp_path, {k: CARDS[k] for k in ("K-93001", "K-93002", "K-93003")})
    r = build(env)
    assert r.returncode != 0, r.stdout + r.stderr
    assert re.search(r"K-93001 contradicts K-93002\b.*K-93002", r.stderr), r.stderr
    assert "apply_triage.py --mirror-contradicts --apply" in r.stderr
    assert "K-93003" not in r.stderr                       # the mirrored edge is not reported


def test_mirrored_refuted_by_and_archived_halves_keep_the_build_green(tmp_path):
    keep = ("K-93003", "K-93006", "K-93007", "K-93008")
    cards = {k: CARDS[k] for k in keep}
    cards["K-93001"] = ("active", BLOCK.format(c="[K-93003]", r="[]"))
    root, env = make(tmp_path, cards, ARCHIVED)
    r = build(env)
    assert r.returncode == 0, r.stdout + r.stderr


# --- the tool -------------------------------------------------------------------------------------
def test_the_dry_run_prints_every_missing_edge_and_writes_nothing(tmp_path):
    root, env = make(tmp_path, CARDS, ARCHIVED)
    before = know_bytes(root)
    r = mirror(env)
    assert r.returncode == 0, r.stdout + r.stderr
    printed = set(re.findall(r"^MIRROR (K-\d+) links\.contradicts \+= (K-\d+)", r.stdout, re.M))
    assert printed == MISSING, r.stdout
    assert re.search(r"^mirror-contradicts: 5 missing half\(s\) on 5 card\(s\) .*DRY RUN", r.stdout, re.M), r.stdout
    assert know_bytes(root) == before


def test_apply_adds_exactly_the_missing_halves_and_no_other_byte(tmp_path):
    root, env = make(tmp_path, CARDS, ARCHIVED)
    before = know_bytes(root)
    r = mirror(env, "--apply")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "OK:" in r.stdout                                # the selected base is rebuilt, green
    after = know_bytes(root)
    want = {}
    for tgt, src in MISSING:
        want.setdefault(tgt, []).append(src)
    for name, raw in before.items():
        cid = name[:-3]
        if cid not in want:
            assert after[name] == raw, cid                  # every other card: byte for byte
            continue
        m0, b0 = split(raw)
        m1, b1 = split(after[name])
        assert b1 == b0, cid                                # the body never moves
        expect = yaml.safe_load(yaml.safe_dump(m0))
        expect["links"]["contradicts"] = list(m0["links"].get("contradicts") or []) + sorted(want[cid])
        assert m1 == expect, cid                            # only links.contradicts grew
        changed = [l for l in difflib.ndiff(raw.decode().splitlines(), after[name].decode().splitlines())
                   if l[:2] in ("- ", "+ ")]
        plus = [l for l in changed if l.startswith("+ ")]
        assert len(plus) == 1 and "contradicts: [" in plus[0], (cid, changed)   # one line written
        assert len(changed) <= 2, (cid, changed)                # ... replacing at most one line
    assert hashlib.sha256(after["K-93007.md"]).digest() == hashlib.sha256(before["K-93007.md"]).digest()
    # idempotent: the second run finds nothing and writes nothing
    again = mirror(env, "--apply")
    assert again.returncode == 0, again.stdout + again.stderr
    assert re.search(r"^mirror-contradicts: 0 missing half\(s\)", again.stdout, re.M), again.stdout
    assert know_bytes(root) == after
    assert build(env).returncode == 0


def test_a_multi_line_list_is_refused_and_nothing_is_written(tmp_path):
    cards = {"K-93001": ("active", BLOCK.format(c="[K-93002]", r="[]")),
             "K-93002": ("active", "links:\n  derived_from: []\n  supports: []\n  contradicts:\n"
                                   "    - K-93003\n    - K-93004\n  refuted_by: []\n  supersedes: null"),
             "K-93003": ("active", BLOCK.format(c="[K-93002]", r="[]")),
             "K-93004": ("active", BLOCK.format(c="[K-93005, K-93002]", r="[]")),
             "K-93005": ("active", BLOCK.format(c="[]", r="[]"))}
    root, env = make(tmp_path, cards)
    before = know_bytes(root)
    r = mirror(env, "--apply")
    assert r.returncode == 2, r.stdout + r.stderr
    assert "K-93002" in r.stderr and "nothing was written" in r.stderr
    assert know_bytes(root) == before                       # not even the editable K-93005


def test_the_mode_takes_no_triage_file(tmp_path):
    root, env = make(tmp_path, {})
    r = subprocess.run([sys.executable, str(TOOLS / "apply_triage.py"), str(root / "x-triage.md"),
                        "--mirror-contradicts"], env=env, text=True, capture_output=True, timeout=60)
    assert r.returncode == 2, r.stdout + r.stderr


# --- approve closes the halves of what it approves -----------------------------------------------
APPROVE_CARD = """\
### C1: the vane points north at noon

```yaml
id: C1
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
  supports: []
  contradicts: [K-93002]
  supersedes: null
  refuted_by: []
---
the vane points north at noon.

**QUOTES:**
- src=journal :: the vane points north at noon.
```
"""


def test_approve_writes_the_back_half_of_a_new_cards_contradicts(tmp_path):
    root, env = make(tmp_path, {"K-93002": CARDS["K-93002"]})
    (root / "state/shared/next_id").write_text("93500\n")
    inbox = root / "memory/inbox" / "2099-01-01.md"
    inbox.write_text("## Cards\n\n" + APPROVE_CARD, encoding="utf-8")
    r = subprocess.run([sys.executable, str(TOOLS / "approve_inbox.py"), str(inbox)], env=env, text=True,
                       capture_output=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    old, _ = split((root / "memory/know/K-93002.md").read_bytes())
    new, _ = split((root / "memory/know/K-93500.md").read_bytes())
    assert new["links"]["contradicts"] == ["K-93002"]
    assert old["links"]["contradicts"] == ["K-93500"]
    assert old["status"] == "active"


# --- a list key written twice: YAML reads the last line, the first one holds the edges -------------
DUP = ("links:\n  supports: []\n  contradicts: {first}\n  derived_from: []\n  contradicts: []\n"
       "  refuted_by: []\n  supersedes: null")


def test_a_duplicated_list_key_is_collapsed_onto_one_line_when_no_id_is_lost(tmp_path):
    cards = {"K-93020": ("active", DUP.format(first="[K-93021, K-93022]")),
             "K-93021": ("active", BLOCK.format(c="[K-93020]", r="[]")),
             "K-93022": ("active", BLOCK.format(c="[K-93020]", r="[]"))}
    root, env = make(tmp_path, cards)
    assert build(env).returncode != 0                       # the parser sees `contradicts: []` there
    before = know_bytes(root)
    r = mirror(env, "--apply")
    assert r.returncode == 0, r.stdout + r.stderr
    after = know_bytes(root)
    assert {k for k in before if before[k] != after[k]} == {"K-93020.md"}
    lines0, lines1 = before["K-93020.md"].decode().splitlines(), after["K-93020.md"].decode().splitlines()
    assert [l for l in lines1 if "contradicts" in l] == ["  contradicts: [K-93021, K-93022]"]
    assert [l for l in lines0 if "contradicts" not in l] == [l for l in lines1 if "contradicts" not in l]
    assert build(env).returncode == 0


def test_a_duplicated_list_key_whose_first_line_would_lose_an_id_is_refused(tmp_path):
    cards = {"K-93020": ("active", DUP.format(first="[K-93021, K-93023]")),
             "K-93021": ("active", BLOCK.format(c="[K-93020]", r="[]")),
             "K-93023": ("active", BLOCK.format(c="[]", r="[]"))}
    root, env = make(tmp_path, cards)
    before = know_bytes(root)
    r = mirror(env, "--apply")
    assert r.returncode == 2, r.stdout + r.stderr
    assert "K-93023" in r.stderr and "nothing was written" in r.stderr
    assert know_bytes(root) == before
