"""khms_archive.py (#219): a card that is no longer worth keeping in the base is MOVED to
memory/archive/know/ — no tombstone — and every link a live card holds to it is taken back through
apply_triage's `unlink` path.

What is pinned here, all on a throwaway base (fictional K-9xxxx cards, no model, no network):
  * the move, and the inbound links on the live side (supports / derived_from / contradicts with its
    mirror half); a `supersedes` edge INTO the archive stays as history (it is bound to the successor's
    SUPERSEDES body line) and build_views stays green on it;
  * a card that is the ONLY live successor of a superseded card is refused unless --allow-chain and the
    predecessor is named in the same run; a refusal stops only that card;
  * an unlink the shared writer refuses (the last derived_from of a derived card) refuses the card;
  * --all-superseded archives every superseded card whose successor chain ends in a live card and
    reports the rest (no successor edge, chain ending in a refuted card);
  * dry run writes nothing; a re-run is a no-op; one JSONL line per archived card;
  * recall's candidate set (khms_search.load_cards, which the dense index also reads) no longer holds an
    archived card, and the mark ledger keeps a mark CARDED when its covering card is archived.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

yaml = pytest.importorskip("yaml")

TOOLS = Path(__file__).resolve().parents[1] / "tools"

# id: (status, level, links block, extra frontmatter, body)
CARDS = {
    # the card to archive: a state that no longer exists
    "K-91001": ("active", "observation", "links:\n  derived_from: []\n  supports: []\n  contradicts: [K-91004]\n"
                "  refuted_by: []\n  supersedes: null", "marks: [M-0000aaaa]\n",
                "The flux capacitor relay sits on bench three. Zebraquartz wiring note."),
    # live cards that link to it, in both frontmatter styles
    "K-91002": ("active", "observation", "links:\n  derived_from: [K-91001]\n  supports: [K-91001, K-91005]\n"
                "  contradicts: []\n  refuted_by: []\n  supersedes: null", "", "A live card that leans on the relay card."),
    "K-91003": ("active", "observation", "links: {derived_from: [], supports: [K-91001], contradicts: [], "
                "refuted_by: [], supersedes: null}", "", "An inline-links card."),
    "K-91004": ("active", "observation", "links:\n  derived_from: []\n  supports: []\n  contradicts: [K-91001]\n"
                "  refuted_by: []\n  supersedes: null", "", "The other half of a contradicts edge."),
    "K-91005": ("active", "observation", "links:\n  derived_from: []\n  supports: []\n  contradicts: []\n"
                "  refuted_by: []\n  supersedes: null", "", "An unrelated live card."),
    # a supersede pair: K-91007 is the ONLY successor of K-91006
    "K-91006": ("superseded", "observation", "links:\n  derived_from: []\n  supports: []\n  contradicts: []\n"
                "  refuted_by: []\n  supersedes: null\n  superseded_by: K-91007", "",
                "The old belief about the gearbox."),
    "K-91007": ("active", "observation", "links:\n  derived_from: []\n  supports: []\n  contradicts: []\n"
                "  refuted_by: []\n  supersedes: K-91006", "",
                "The new belief about the gearbox.\nSUPERSEDES K-91006 BECAUSE: measured again."),
    # a derived card whose only derived_from is K-91009: unlinking it would leave the card invalid
    "K-91008": ("active", "derived", "links:\n  derived_from: [K-91009]\n  supports: []\n  contradicts: []\n"
                "  refuted_by: []\n  supersedes: null", "", "A principle derived from one card."),
    "K-91009": ("active", "observation", "links:\n  derived_from: []\n  supports: []\n  contradicts: []\n"
                "  refuted_by: []\n  supersedes: null", "", "The one card a principle rests on."),
    # chains for --all-superseded: K-91010 -> K-91011 (superseded) -> K-91012 (live)
    "K-91010": ("superseded", "observation", "links:\n  derived_from: []\n  supports: []\n  contradicts: []\n"
                "  refuted_by: []\n  supersedes: null\n  superseded_by: K-91011", "", "Version one."),
    "K-91011": ("superseded", "observation", "links:\n  derived_from: []\n  supports: []\n  contradicts: []\n"
                "  refuted_by: []\n  supersedes: K-91010\n  superseded_by: K-91012", "",
                "Version two.\nSUPERSEDES K-91010 BECAUSE: v2."),
    "K-91012": ("active", "observation", "links:\n  derived_from: []\n  supports: [K-91011]\n  contradicts: []\n"
                "  refuted_by: []\n  supersedes: K-91011", "", "Version three.\nSUPERSEDES K-91011 BECAUSE: v3."),
    # a superseded card with no successor edge at all: reported, never archived
    "K-91013": ("superseded", "observation", "links:\n  derived_from: []\n  supports: []\n  contradicts: []\n"
                "  refuted_by: []\n  supersedes: null", "", "An orphan superseded card."),
    # a superseded card whose successor is refuted: the chain ends dead, reported
    "K-91014": ("superseded", "observation", "links:\n  derived_from: []\n  supports: []\n  contradicts: []\n"
                "  refuted_by: []\n  supersedes: null\n  superseded_by: K-91015", "", "Superseded by a dead end."),
    "K-91015": ("refuted", "observation", "links:\n  derived_from: []\n  supports: []\n  contradicts: [K-91016]\n"
                "  refuted_by: [K-91016]\n  supersedes: K-91014", "",
                "A refuted successor.\nSUPERSEDES K-91014 BECAUSE: x."),
    "K-91016": ("active", "observation", "links:\n  derived_from: []\n  supports: []\n  contradicts: [K-91015]\n"
                "  refuted_by: []\n  supersedes: null", "", "The refuter."),
    # a superseded card whose only successor pointer is its own superseded_by (the target declares nothing)
    "K-91017": ("superseded", "observation", "links:\n  derived_from: []\n  supports: []\n  contradicts: []\n"
                "  refuted_by: []\n  supersedes: null\n  superseded_by: K-91016", "", "A one-sided pointer."),
}


def card_text(cid, status, level, links, extra, body):
    typ = "principle" if level == "derived" else "fact"
    return (f"---\nid: {cid}\ntype: {typ}\nlevel: {level}\nstatus: {status}\ntags: [testing]\n"
            f"scope: universal\nevidence: measured\nsource: synthetic\ndate: '2099-01-01'\n{links}\n{extra}---\n"
            f"{body}\n\nCZ: syntetická karta {cid}.\n")


@pytest.fixture
def base(tmp_path):
    home, root = tmp_path / "home", tmp_path / "base"
    home.mkdir()
    env = {"PATH": os.environ["PATH"], "HOME": str(home), "KHMS_NO_SNAPSHOT": "1",
           "PYTHONDONTWRITEBYTECODE": "1"}
    r = subprocess.run([str(TOOLS / "khms_init.sh"), str(root)], env=env, text=True, capture_output=True)
    assert r.returncode == 0, r.stderr
    for cid, spec in CARDS.items():
        (root / "memory/know" / f"{cid}.md").write_text(card_text(cid, *spec), encoding="utf-8")
    # one journal MARK covered by K-91001's `marks:` frontmatter
    (root / "journal").mkdir(exist_ok=True)
    env["KHMS_ROOT"] = str(root)
    return root, env


def run(env, *args):
    return subprocess.run([sys.executable, str(TOOLS / "khms_archive.py"), *args], env=env, text=True,
                          capture_output=True, timeout=120)


def snapshot(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted((root / "memory").rglob("*")) if p.is_file() and ".embed" not in p.parts}


def links(root, cid, where="know"):
    text = (root / "memory" / where / f"{cid}.md").read_text(encoding="utf-8")
    return yaml.safe_load(text.split("---\n")[1])["links"]


def body(root, cid, where="know"):
    return (root / "memory" / where / f"{cid}.md").read_text(encoding="utf-8").split("\n---\n", 1)[1]


def ledger(root):
    p = root / "memory/ledger/archive.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()] if p.exists() else []


def test_dry_run_lists_the_inbound_links_and_writes_nothing(base):
    root, env = base
    before = snapshot(root)
    r = run(env, "--card", "K-91001", "--reason", "state-only", "--by", "coordinator")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "DRY RUN" in r.stdout
    for line in ("K-91002 links.derived_from", "K-91002 links.supports", "K-91003 links.supports",
                 "K-91004 links.contradicts"):
        assert line in r.stdout, r.stdout
    assert snapshot(root) == before


def test_apply_moves_the_card_and_unlinks_every_live_link(base):
    root, env = base
    bodies = {cid: body(root, cid) for cid in ("K-91002", "K-91003", "K-91004")}
    r = run(env, "--card", "K-91001", "--reason", "state-only", "--by", "coordinator", "--apply")
    assert r.returncode == 0, r.stdout + r.stderr
    assert not (root / "memory/know/K-91001.md").exists()
    assert (root / "memory/archive/know/K-91001.md").exists()
    assert links(root, "K-91002")["derived_from"] == []
    assert links(root, "K-91002")["supports"] == ["K-91005"]
    assert links(root, "K-91003")["supports"] == []
    assert links(root, "K-91004")["contradicts"] == []
    for cid, b in bodies.items():                     # bodies byte-for-byte
        assert body(root, cid) == b
    live = "".join(p.read_text() for p in (root / "memory/know").glob("K-*.md"))
    assert "K-91001" not in live
    views = "".join(p.read_text() for p in (root / "memory/views").rglob("*.md"))
    assert "K-91001" not in views
    rec = ledger(root)
    assert [x["id"] for x in rec] == ["K-91001"]
    assert rec[0]["by"] == "coordinator" and rec[0]["reason"] == "state-only"
    assert {(u["card"], u["field"]) for u in rec[0]["unlinked"]} == {
        ("K-91002", "derived_from"), ("K-91002", "supports"), ("K-91003", "supports"),
        ("K-91004", "contradicts")}


def test_a_rerun_is_a_no_op(base):
    root, env = base
    assert run(env, "--card", "K-91001", "--reason", "state-only", "--by", "owner", "--apply").returncode == 0
    before = snapshot(root)
    r = run(env, "--card", "K-91001", "--reason", "state-only", "--by", "owner", "--apply")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "already archived" in r.stdout
    assert snapshot(root) == before
    assert len(ledger(root)) == 1


def test_the_only_successor_of_a_superseded_card_is_refused_without_allow_chain(base):
    root, env = base
    before = snapshot(root)
    r = run(env, "--card", "K-91007", "K-91005", "--reason", "state-only", "--by", "coordinator", "--apply")
    assert r.returncode == 2, r.stdout + r.stderr
    assert "REFUSED K-91007" in r.stdout and "K-91006" in r.stdout
    assert (root / "memory/know/K-91007.md").exists()
    assert not (root / "memory/know/K-91005.md").exists()        # the other card still went
    # --allow-chain without the predecessor in the run: both stay
    r = run(env, "--card", "K-91007", "--reason", "state-only", "--by", "coordinator", "--apply",
            "--allow-chain")
    assert r.returncode == 2 and (root / "memory/know/K-91007.md").exists()
    # --allow-chain with the predecessor named: both archived
    r = run(env, "--card", "K-91007", "K-91006", "--reason", "state-only", "--by", "coordinator", "--apply",
            "--allow-chain")
    assert r.returncode == 0, r.stdout + r.stderr
    assert not (root / "memory/know/K-91007.md").exists() and not (root / "memory/know/K-91006.md").exists()
    assert before  # the first run changed only what it named


def test_a_superseded_card_goes_and_its_successors_supersedes_edge_stays_as_history(base):
    root, env = base
    r = run(env, "--card", "K-91006", "--reason", "superseded", "--by", "coordinator", "--apply")
    assert r.returncode == 0, r.stdout + r.stderr
    assert links(root, "K-91007")["supersedes"] == "K-91006"
    assert "SUPERSEDES K-91006 BECAUSE" in body(root, "K-91007")
    bv = subprocess.run([sys.executable, str(TOOLS / "build_views.py")], env=env, text=True, capture_output=True)
    assert bv.returncode == 0, bv.stdout + bv.stderr
    lint = subprocess.run([sys.executable, str(TOOLS / "khms_lint.py"), str(root / "memory/know")], env=env,
                          text=True, capture_output=True)
    assert lint.returncode == 0, lint.stdout + lint.stderr


def test_an_unlink_the_writer_refuses_refuses_the_card(base):
    root, env = base
    before = snapshot(root)
    r = run(env, "--card", "K-91009", "--reason", "state-only", "--by", "coordinator", "--apply")
    assert r.returncode == 2, r.stdout + r.stderr
    assert "REFUSED K-91009" in r.stdout and "derived_from" in r.stdout
    assert snapshot(root) == before


def test_all_superseded_follows_chains_and_reports_dead_ends(base):
    root, env = base
    r = run(env, "--all-superseded", "--reason", "superseded", "--by", "coordinator", "--apply")
    assert r.returncode == 0, r.stdout + r.stderr
    for cid in ("K-91006", "K-91010", "K-91011"):
        assert (root / "memory/archive/know" / f"{cid}.md").exists(), cid
    for cid in ("K-91013", "K-91014"):
        assert (root / "memory/know" / f"{cid}.md").exists(), cid
    assert "K-91013" in r.stdout and "no successor" in r.stdout
    assert "K-91014" in r.stdout and "refuted" in r.stdout
    assert (root / "memory/know/K-91017.md").exists()
    assert "REPORTED K-91017: no successor edge (only superseded_by K-91016" in r.stdout
    assert links(root, "K-91012")["supports"] == []                   # inbound link taken back
    rec = {x["id"]: x for x in ledger(root)}
    assert "superseded-by K-91011" in rec["K-91010"]["reason"] and "K-91012" in rec["K-91010"]["reason"]


def test_recall_candidates_and_the_mark_ledger_after_archive(base):
    root, env = base
    assert run(env, "--card", "K-91001", "--reason", "state-only", "--by", "coordinator", "--apply").returncode == 0
    probe = ("import sys; sys.path.insert(0, %r); import khms_search as ks; "
             "print(sorted(c['id'] for c in ks.load_cards()))" % str(TOOLS))
    out = subprocess.run([sys.executable, "-c", probe], env=env, text=True, capture_output=True)
    assert out.returncode == 0, out.stderr
    assert "K-91001" not in out.stdout and "K-91002" in out.stdout
    # a card already sitting in the archive from before is not a recall candidate either
    shutil.copy(root / "memory/know/K-91005.md", root / "memory/archive/know/K-91099.md")
    out = subprocess.run([sys.executable, "-c", probe], env=env, text=True, capture_output=True)
    assert "K-91099" not in out.stdout
    r = subprocess.run([str(TOOLS / "recall.sh"), "Zebraquartz", "flux", "capacitor"], env=env, text=True,
                       capture_output=True)
    assert "K-91001" not in r.stdout


def test_a_mark_covered_by_an_archived_card_stays_carded(base):
    root, env = base
    sys.path.insert(0, str(TOOLS))
    import mark_ledger as ml
    paths = ml.Paths(str(root))
    (root / "journal" / "2099-01-01.md").write_text(
        "- 10:00 MARK[nalez] the flux capacitor relay sits on bench three (test)\n", encoding="utf-8")
    marks = ml.collect_marks(paths, "1970-01-01")
    assert len(marks) == 1
    mid = next(iter(marks))
    text = (root / "memory/know/K-91001.md").read_text().replace("M-0000aaaa", mid)
    (root / "memory/know/K-91001.md").write_text(text)
    assert ml.derive(paths, "1970-01-01")[mid]["state"] == "CARDED"
    assert run(env, "--card", "K-91001", "--reason", "state-only", "--by", "coordinator", "--apply").returncode == 0
    mk = ml.derive(paths, "1970-01-01")[mid]
    assert mk["state"] == "CARDED" and mk["card"] == "K-91001"
    assert "archived" in mk["reason"] and "state-only" in mk["reason"]


def test_keep_links_leaves_every_live_link_as_history_and_the_base_stays_buildable(base):
    root, env = base
    r = run(env, "--card", "K-91011", "K-91010", "--allow-chain", "--keep-links", "--reason", "superseded",
            "--by", "coordinator", "--apply")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "keep   K-91012 links.supports K-91011" in r.stdout
    assert links(root, "K-91012")["supports"] == ["K-91011"] and links(root, "K-91012")["supersedes"] == "K-91011"
    assert (root / "memory/archive/know/K-91011.md").exists()
    rec = {x["id"]: x for x in ledger(root)}
    assert rec["K-91011"]["unlinked"] == [] and {"card": "K-91012", "field": "supports"} in rec["K-91011"]["kept"]
    for cmd in ([sys.executable, str(TOOLS / "build_views.py")],
                [sys.executable, str(TOOLS / "khms_lint.py"), str(root / "memory/know")]):
        p = subprocess.run(cmd, env=env, text=True, capture_output=True)
        assert p.returncode == 0, p.stdout + p.stderr
