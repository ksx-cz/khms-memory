#!/usr/bin/env python3
"""Judge-context experiment for the nightly contradiction triage .

Prepares a frozen 40-case development set exported as immutable pre-review snapshots, blind
two-axis labelling sheets for two independent labellers, and a harness that judges every case
under three INPUT arms with the same frozen standard and the same model:

    A  the nightly cut: card metadata line + the first 1 600 body characters (card_blob)
    B  both FULL bodies, the same metadata line
    C  B + full frontmatter, the edge state around both cards (out- and in-edges), and the
       directly linked correction/successor cards (no recursion)

Every arm also gets the same task: the sweep's quoted claim, the winning quote and the proposed
operation, exactly as the night's triage file stated them before the review.

    judge_exp.py build    [--out DIR]                 # one-time; refuses to overwrite
    judge_exp.py verify   [--out DIR]                 # integrity of the frozen set (rc 0/1)
    judge_exp.py run      --arm A|B|C [--dry] [--cases JX-01,..] [--max-usd X]
    judge_exp.py report   [--unblind] [--final-labels JSONL [--labels-manifest JSON]]
                                                      # paired per-case table + cost; with the
                                                      # final labels: accuracy per arm/axis,
                                                      # paired changes, stable-miss causes
    judge_exp.py verdict                              # JUDGEEXP-VERDICT line

Nothing here writes to memory/. Historical anchors are read from Nextcloud versions.
No local snapshots are made or read; missing history is explicitly unverifiable.
Prices: the CLI's own total_cost_usd; the projection scales it by measured tokens and
tools/tok_account.py's output price \u2014 no price is typed into this file.
"""
from __future__ import annotations

# Public helpers live beside the experiment package.
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import argparse
import datetime as dt
import glob
import hashlib
import json
import os
import random
import re
import subprocess
import sys
import tempfile
import time
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import apply_triage as A  # noqa: E402  (ROW numbering + verdict parsing, one definition)
import eval_triage_judge as J  # noqa: E402  (consistent_mask, sonnet answer caches)
import eval_triage_selector as E  # noqa: E402
import khms_contradict as kc  # noqa: E402  (claude_cmd, card_blob, JUDGE_CARD_CHARS)

try:
    import yaml
except ImportError:  # pragma: no cover - apply_triage already needs it
    yaml = None

BRAIN = E.BRAIN
from khms_versions import Versions, VersionError
INBOX = os.path.join(BRAIN, "memory", "inbox")
CONFIG_FILE = E.P.get("KHMS_JUDGE_EXPERIMENT_CONFIG")
CONFIG = json.loads(Path(CONFIG_FILE).read_text()) if CONFIG_FILE else {}
def configured_path(value):
    return value if os.path.isabs(value) else os.path.join(BRAIN, value)
OUT = configured_path(CONFIG.get("out", "reports/triage-judge-experiment"))
SERIALIZER = "jx-ser-1"          # bump when an arm's serialisation changes
# POST-FREEZE RECORD, keyed by the sha256 of the manifest it is about .
SEED = int(CONFIG.get("seed", 0))
MODEL = "sonnet"
ARMS = ("A", "B", "C")
LABELLERS = tuple(CONFIG.get("labellers", ["reviewer-a", "reviewer-b"]))
QUOTAS = {"stable_miss_apply": 9, "other_apply": 11, "reject": 10, "changing": 10}
SOURCE_MAX = 4                   # directly linked cards shown in arm C, per case
SOURCE_BUDGET = 24000            # characters of linked-card text in arm C, per case
CORRECTION_KEYS = ("contradicts", "refuted_by", "supersedes", "superseded_by",
                   "challenged_by", "refutes", "challenges")
IN_SOURCE_KEYS = ("supersedes", "contradicts", "refutes", "challenges")
RUNNER = subprocess.run          # replaced by a stub in tests

# Sonnet answer caches of the earlier rounds (TRIAGESONNET old prompt, JUDGEDEF tuning
# variants p1/p2/p2-whole/p4 on the 102-row tune sample, JUDGEDEF final prompt). They decide
# which APPLY rows are "stably missed"; they are read, never written.
CITED_DOCS = tuple(configured_path(p) for p in CONFIG.get("cited_docs", []))
PRIOR_CACHES = {k: configured_path(v) for k, v in CONFIG.get("prior_caches", {}).items()}


def sha(b):
    if isinstance(b, str):
        b = b.encode("utf-8")
    return hashlib.sha256(b).hexdigest()


def fsha(path):
    return sha(open(path, "rb").read())


# ----------------------------------------------------------------------------- snapshots

def snap_stamp(name):
    """'2026-09-10T045838-pre-triage-1' -> datetime(2026-09-10 04:58:38)."""
    m = re.match(r"^(\d{4}-\d{2}-\d{2})T(\d{4})(\d{2})?-", name)
    if not m:
        return None
    t = m.group(2) + (m.group(3) or "00")
    return dt.datetime.strptime(m.group(1) + t, "%Y-%m-%d%H%M%S")


def applied_info(triage_file):
    """(APPLIED datetime, {card id: [(minus lines), (plus lines)]}) from <file>.applied.md."""
    p = os.path.join(INBOX, triage_file.replace(".md", ".applied.md"))
    if not os.path.exists(p):
        return None, {}
    text = open(p, encoding="utf-8").read()
    m = re.search(r"\(APPLIED (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})\)", text)
    when = dt.datetime.fromisoformat(m.group(1)) if m else None
    diffs = defaultdict(lambda: ([], []))
    for dm in re.finditer(r"^--- (K-\d+) \(frontmatter, before\)\n\+\+\+ \1 \(frontmatter, after\)\n"
                          r"(.*?)^```", text, re.M | re.S):
        cid = dm.group(1)
        minus, plus = diffs[cid]
        for line in dm.group(2).splitlines():
            if line.startswith("-") and not line.startswith("---"):
                # the ledger's last before-line has no newline when the frontmatter ended
                # without one, so difflib glued the after-line to it:
                # "-  refuted_by: []+  refuted_by: [a card]" (2026-09-27, a card)
                glued = re.match(r"^(.*?[\]\w])\+(\s+\w+:.*)$", line[1:])
                if glued:
                    minus.append(glued.group(1))
                    plus.append(glued.group(2))
                else:
                    minus.append(line[1:])
            elif line.startswith("+") and not line.startswith("+++"):
                plus.append(line[1:])
    return when, dict(diffs)


def split_front(text):
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
    return (m.group(1), m.group(2)) if m else ("", text)


def front_meta(front):
    if yaml is None:
        return {}
    try:
        meta = yaml.safe_load(front) or {}
    except Exception:  # noqa: BLE001 - a malformed frontmatter is reported, not fatal
        return {"_yaml_error": True}
    return meta if isinstance(meta, dict) else {}


def links_of(meta):
    out = {}
    for k, v in (meta.get("links") or {}).items() if isinstance(meta.get("links"), dict) else []:
        if v in (None, "", "null"):
            continue
        ids = v if isinstance(v, list) else [v]
        ids = [str(x) for x in ids if re.match(r"^K-\d+$", str(x))]
        if ids:
            out[k] = ids
    return out


_GRAPH_CACHE: dict = {}


def edge_state(g, ids):
    """Out-edges of each id, and in-edges from every other card of the snapshot."""
    out = {}
    for cid in ids:
        node = g.get(cid) or {}
        inbound = []
        for other, o in g.items():
            if other == cid:
                continue
            for k, lst in o["links"].items():
                if cid in lst:
                    inbound.append({"from": other, "key": k, "status": o["status"],
                                    "type": o["type"], "date": o["date"], "title": o["title"]})
        out[cid] = {"out": node.get("links", {}), "in": sorted(inbound, key=lambda e: (e["from"], e["key"]))}
    return out


# ----------------------------------------------------------------------------- review rows

def load_review_rows(inbox=INBOX):
    """Every reviewed row: the night's proposal block as the sweep wrote it, plus the label."""
    rows = []
    for f in sorted(glob.glob(os.path.join(inbox, "*-triage.md"))):
        text = open(f, encoding="utf-8").read()
        if "## REVIEW VERDICT" not in text:
            continue
        head, ver = text.split("## REVIEW VERDICT", 1)
        name = os.path.basename(f)
        parsed = A.parse_rows(head)
        marks = list(A.ROW_RE.finditer(head))
        sections = [(m.start(), m.group(0)) for m in re.finditer(r"^## .*$", head, re.M)]
        for n, (verdict, cls, why) in sorted(A.parse_verdicts(ver).items()):
            if n > len(parsed):
                continue
            r = parsed[n - 1]
            m = marks[n - 1]
            end = marks[n].start() if n < len(marks) else len(head)
            block = head[m.start():end].split("\n## ")[0].rstrip()
            sec = [s for pos, s in sections if pos < m.start()]
            rows.append({"file": name, "n": n, "verdict": verdict, "cls": cls, "why": why,
                         "loser_id": r["loser"], "winner_id": r["winner"],
                         "action": r["action"], "severity": r["severity"],
                         "quote": r["quote"], "win_quote": r["winner_quote"],
                         "block": block, "section": sec[-1] if sec else ""})
    return rows


def pair_key(r):
    return frozenset((r["loser_id"], r["winner_id"]))


def cited_rows(docs=CITED_DOCS):
    """({unordered card pairs}, {(triage file, row)}) named in the documents the labellers read."""
    pairs, rows = set(), set()
    for d in docs:
        if not os.path.exists(d):
            continue
        t = open(d, encoding="utf-8").read()
        for m in re.finditer(r"(K-\d{5})\s*(?:<-|\u21c4|\u2190|vs\.?|against)\s*(K-\d{5})", t):
            pairs.add(frozenset((m.group(1), m.group(2))))
        for m in re.finditer(r"(20\d\d-\d\d-\d\d(?:-coordinator)?)(?:-triage(?:\.md)?)?\s+ROW\s+(\d+)", t):
            rows.add((f"{m.group(1)}-triage.md", int(m.group(2))))
    return pairs, rows


def is_cited(r, cited):
    pairs, rows = cited
    return pair_key(r) in pairs or (r["file"], r["n"]) in rows


def reject_bucket(why):
    w = why.lower()
    for name, pat in (("already", r"already|terminal|resolved by erratum|exists"),
                      ("inverted", r"inverted|winner is (the )?older|winner is the earlier"),
                      ("decision", CONFIG.get("decision_pattern", r"polic|decision|requirement|principle")),
                      ("era_history", r"era|history|historical|different runs?|different conditions"
                                      r"|different quantit|months apart|sequel"),
                      ("agree", r"agree|confirm|compatible|not contradict|no conflict"
                                r"|no contradiction|complementary|support"),
                      ("subject", r"subject|unrelated|wrong witness|different symptom"),
                      ("action", r"amend|mark_dead_end|retire|superseding card|proposed")):
        if re.search(pat, w):
            return name
    return "other"


def stable_misses(rows, prior):
    """Consistent APPLY rows of the 20 tune nights that at most ONE of the six Sonnet prompts
    measured on them (old, p1, p2, p2-whole, p4, final) called a contradiction."""
    is_cons, _ = J.consistent_mask(rows)
    files = sorted({r["file"] for r in rows})
    held = set(files[-E.N_HELD:])
    out = []
    for r in rows:
        if r["verdict"] != "APPLY" or r["file"] in held or not is_cons(r):
            continue
        key = (r["file"], r["n"])
        votes = {k: (a.get(key) or {}).get("j", {}).get("contradiction") for k, a in prior.items()}
        if any(v is None for v in votes.values()):
            continue
        if sum(v == "yes" for v in votes.values()) <= 1:
            out.append((r, votes))
    return out


def _order(r, salt=""):
    return sha(f"{SEED}|{salt}|{r['file']}|{r['n']}")


def select_cases(rows, prior, eligible, cited=(set(), set())):
    """The frozen development set: {category: [row]} (the independent reviewer \u00a74.A). Overlaps are replaced: no
    two cases share a card pair or a loser card."""
    is_cons, both = J.consistent_mask(rows)
    used_pairs, used_losers, chosen, notes = set(), set(), defaultdict(list), []
    night_count = Counter()

    def take(cat, r, night_cap=None):
        if pair_key(r) in used_pairs or r["loser_id"] in used_losers:
            return False
        if cat != "stable_miss_apply" and is_cited(r, cited):
            return False
        if night_cap and night_count[(cat, r["file"])] >= night_cap:
            return False
        if not eligible(r):
            return False
        chosen[cat].append(r)
        used_pairs.add(pair_key(r))
        used_losers.add(r["loser_id"])
        night_count[(cat, r["file"])] += 1
        return True

    sm = stable_misses(rows, prior)
    for r, votes in sorted(sm, key=lambda x: (x[0]["file"], x[0]["n"])):
        if not take("stable_miss_apply", r):
            notes.append(f"stable miss {r['file']} ROW {r['n']} not taken (ineligible/overlap)")
        r["_votes"] = votes
    sm_keys = {(r["file"], r["n"]) for r, _ in sm}

    ch = [r for r in rows if not is_cons(r)]
    by_pair = defaultdict(list)
    for r in ch:
        by_pair[pair_key(r)].append(r)
    want = {"APPLY": 5, "REJECT": 5}
    for pk in sorted(by_pair, key=lambda k: sha(f"{SEED}|pair|{sorted(k)}")):
        if len(chosen["changing"]) >= QUOTAS["changing"]:
            break
        for r in sorted(by_pair[pk], key=_order):
            if want[r["verdict"]] > 0 and take("changing", r, night_cap=2):
                want[r["verdict"]] -= 1
                break

    oa = [r for r in rows if r["verdict"] == "APPLY" and is_cons(r)
          and (r["file"], r["n"]) not in sm_keys]
    for r in sorted(oa, key=_order):
        if len(chosen["other_apply"]) >= QUOTAS["other_apply"]:
            break
        take("other_apply", r, night_cap=3)

    rj = [r for r in rows if r["verdict"] == "REJECT" and is_cons(r)]
    quota = {"subject": 2, "agree": 2, "already": 1, "era_history": 2, "decision": 1,
             "inverted": 1, "action": 1}
    for r in sorted(rj, key=_order):
        b = reject_bucket(r["why"])
        if quota.get(b, 0) > 0 and take("reject", r, night_cap=2):
            quota[b] -= 1
            r["_bucket"] = b
    for r in sorted(rj, key=_order):          # fill any bucket that ran dry
        if len(chosen["reject"]) >= QUOTAS["reject"]:
            break
        if take("reject", r, night_cap=2):
            r["_bucket"] = reject_bucket(r["why"]) + "(fill)"
    for cat, q in QUOTAS.items():
        if len(chosen[cat]) != q:
            notes.append(f"category {cat}: {len(chosen[cat])} cases, quota {q}")
    return chosen, notes


# ----------------------------------------------------------------------------- packets

def _field(block, name):
    m = re.search(rf"^- {name}: (.*)$", block, re.M)
    return m.group(1).strip() if m else ""


def source_cards(edges, lid, wid):
    """Directly linked correction / successor / source cards of L and W (no recursion),
    ordered: linked to both first, then by how strongly the key corrects."""
    prio = {k: i for i, k in enumerate(("superseded_by", "refuted_by", "challenged_by",
                                        "contradicts", "supersedes", "refutes", "challenges",
                                        "derived_from"))}
    found = defaultdict(list)          # card id -> [(priority, relation text, role)]
    for cid, role in ((lid, "L"), (wid, "W")):
        e = edges.get(cid) or {}
        for k, ids in e.get("out", {}).items():
            if k in prio:
                for x in ids:
                    found[x].append((prio[k], f"{role} {cid} {k} {x}", role))
        for ie in e.get("in", []):
            # an in-edge points at a corrector/successor only for these keys; `X refuted_by L`
            # or `X derived_from L` make X a card L corrected or a descendant of L
            if ie["key"] in IN_SOURCE_KEYS:
                found[ie["from"]].append((prio[ie["key"]], f"{ie['from']} {ie['key']} {role} {cid}", role))
    found.pop(lid, None)
    found.pop(wid, None)
    ranked = sorted(found.items(), key=lambda kv: (-len({role for _, _, role in kv[1]}),
                                                   min(pr for pr, _, _ in kv[1]), kv[0]))
    return [(cid, [rel for _, rel, _ in rels]) for cid, rels in ranked[:SOURCE_MAX]]


def _meta_line(c):
    return (f"id={c['id']} type={c.get('type', '?')} status={c['status']} "
            f"level={c['level']} date={c.get('date', '?')}")


def _parsed(card_entry, cid):
    import khms_triage_gate as G
    return G.parse_card_text(cid, card_entry["text"])


def render_task(pk):
    p = pk["proposal"]
    return (f"## THE PROPOSAL (as the nightly sweep wrote it, before any review)\n"
            f"L = {p['loser']} (the card the sweep calls wrong), W = {p['winner']} "
            f"(the card it says corrects L)\n"
            f"proposed operation: {p['action']} · severity {p['severity']}\n\n"
            f"{p['block']}\n")


def render_arm(pk, arm):
    """The case as the judge of `arm` sees it (the frozen standard is prepended by caller)."""
    p = pk["proposal"]
    parts = [render_task(pk)]
    for cid in (p["loser"], p["winner"]):
        ce = pk["cards"][cid]
        role = ce["role"]
        if arm == "A":
            parts.append(f"## CARD {role}\n{kc.card_blob(_parsed(ce, cid), maxlen=kc.JUDGE_CARD_CHARS)}")
        elif arm == "B":
            c = _parsed(ce, cid)
            parts.append(f"## CARD {role} (whole body)\n{_meta_line(c)}\n{c['body']}")
        elif arm == "C":
            parts.append(f"## CARD {role} (whole file: frontmatter and body)\n{ce['text'].rstrip()}")
        else:
            raise ValueError(f"unknown arm {arm!r}")
    if arm == "C":
        lines = ["## EDGE STATE in the base at the time of this proposal"]
        for cid in (p["loser"], p["winner"]):
            e = pk["edges"][cid]
            role = pk["cards"][cid]["role"]
            outs = "; ".join(f"{k}: [{', '.join(v)}]" for k, v in sorted(e["out"].items())) or "none"
            lines.append(f"{role} {cid} links out: {outs}")
            if e["in"]:
                for ie in e["in"]:
                    lines.append(f"  in: {ie['from']} --{ie['key']}--> {cid}  ({ie['type']}, "
                                 f"status {ie['status']}, {ie['date']}) {ie['title'][:100]}")
            else:
                lines.append(f"  in: no card links to {cid}")
            ment = pk["body_mentions"].get(cid) or []
            lines.append(f"  body of {cid} mentions: {', '.join(ment) or 'no other card'}")
        parts.append("\n".join(lines))
        used = 0
        src = ["## DIRECTLY LINKED CARDS (corrector / successor / source; one step, no recursion)"]
        if not pk["sources"]:
            src.append("none")
        for s in pk["sources"]:
            head = f"### {s['id']} — linked as: {'; '.join(s['relations'])}"
            if s.get("missing"):
                src.append(f"{head}\n[NOT IN THE SNAPSHOT — this card could not be shown]")
                continue
            t = s["text"].rstrip()
            room = max(0, SOURCE_BUDGET - used)
            if len(t) > room:
                src.append(f"{head}\n{t[:room]}\n[EXCERPT: the first {room} of {len(t)} characters "
                           f"of {s['id']} — the rest is NOT shown]")
                used = SOURCE_BUDGET
            else:
                src.append(f"{head}\n{t}")
                used += len(t)
        parts.append("\n\n".join(src))
    return "\n\n".join(parts)


def judge_prompt(standard, pk, arm):
    return standard.rstrip() + "\n\n# THE CASE\n\n" + render_arm(pk, arm) + "\n\nJSON object:"


# ----------------------------------------------------------------------------- sheets

LABEL_FORM = """```yaml
case: {case}
labeller: {who}
relation:               # CONTRADICTS | UNDERCUTS | SUPERSEDES | REFINES | AGREES | UNRELATED | INSUFFICIENT
claim_l: ""             # the affected claim of L, verbatim
claim_w: ""             # the claim of W it is compared with, verbatim
direction_determinable: # yes | no
action_needed:          # yes | no | unclear
action_kind:            # REFUTE | CHALLENGE | ERRATUM | SUPERSESSION | LINK_REPAIR | NO_ACTION_ALREADY_DONE | NO_ACTION | NEEDS_CONTEXT
target_card:            # the card the operation changes, or empty
proposed_op_ok:         # yes | no | partly
evidence_sufficient:    # yes | no  (were the primary sources below enough?)
missing: ""             # what was missing, if anything
uncertainty:            # low | medium | high
why: ""                 # two sentences at most
```"""


def render_sheet(pk, who):
    snap_dir = "server-version proofs (verify before trusting historical inputs)"
    return (f"# Blind labelling sheet — {pk['case_id']} — labeller: {who}\n\n"
            f"Label on BOTH axes of the frozen experiment standard (relation of the\n"
            f"claims; needed operation on the graph as it stood then). Work alone: do not open\n"
            f"`key/`, the other labeller's sheets, any `runs/` output or that night's morning review\n"
            f"section of `memory/inbox/<night>-triage.md` or its `.applied.md` ledger.\n"
            f"Primary sources = this packet and the frozen snapshot `{snap_dir}` (the state\n"
            f"before this proposal was reviewed). Do NOT use today's live card: a missing source is\n"
            f"`evidence_sufficient: no`, not something to fill from the live base.\n\n"
            f"Snapshot taken {pk['snapshot_stamp']}; the review's fix was applied after it.\n\n"
            f"## Your label\n\n" + LABEL_FORM.format(case=pk["case_id"], who=who) + "\n\n---\n\n"
            + render_arm(pk, "C") + "\n")


def parse_sheet(path):
    """The filled YAML label of one sheet (None values where still blank)."""
    text = open(path, encoding="utf-8").read()
    m = re.search(r"## Your label\n\n```yaml\n(.*?)```", text, re.S)
    if not m or yaml is None:
        return None
    return yaml.safe_load(m.group(1))


# ----------------------------------------------------------------------------- build

def load_prior(out):
    d = os.path.join(out, "key", "prior-answers")
    return {k: J.load_cache(os.path.join(d, f"{k}.jsonl")) for k in PRIOR_CACHES}


def _write_ro(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.chmod(path, 0o444)


def cmd_build(args):
    print("BUILD: UNVERIFIABLE: new historical graph sets require a contemporaneous "
          "server-version inventory. Existing packets cannot witness their own inputs; "
          "no local snapshot or live-card fallback is available.", file=sys.stderr)
    return 3


def own_fix_lines(night_file):
    """{card: (plus-only lines, minus-only lines)} the night's own apply wrote/removed."""
    _, diffs = applied_info(night_file)
    out = {}
    for cid, (minus, plus) in diffs.items():
        out[cid] = ([ln for ln in plus if ln not in minus], [ln for ln in minus if ln not in plus])
    return out


def own_fix_edges(night_file):
    """{(card, key, other)} edges that the night's own apply ADDED (from the ledger's diffs)."""
    edges = set()
    for cid, (plus_only, minus_only) in own_fix_lines(night_file).items():
        before = {}
        for ln in minus_only:
            m = re.match(r"^\s*(\w+):\s*\[?([^\]]*)\]?\s*$", ln)
            if m:
                before[m.group(1)] = set(re.findall(r"K-\d+", m.group(2)))
        for ln in plus_only:
            m = re.match(r"^\s*(\w+):\s*\[?([^\]]*)\]?\s*$", ln)
            if m:
                for x in set(re.findall(r"K-\d+", m.group(2))) - before.get(m.group(1), set()):
                    edges.add((cid, m.group(1), x))
    return edges


def verify_packet(pk, rec, client=None, pruned=(), lost=None):
    """Verify packet hashes against retained server versions. Missing history is a problem."""
    bad = []

    def anchor(label, path, want):
        try:
            dav = client or Versions.configured()
            # The legacy packet path supplies only a relative card location; it
            # is not read locally and the embedded body is never an anchor.
            rel = 'memory/' + path.split('/memory/', 1)[1]
            when = dt.datetime.fromisoformat(pk['fix_applied_at'])
            if when.tzinfo is None:
                raise VersionError('historical cutoff has no timezone')
            matches = []
            for version in dav.versions(dav.file_id(rel)):
                if int(version['version']) >= when.timestamp():
                    continue
                try:
                    content, proof = dav.read(version)
                except VersionError:
                    continue
                if proof['sha256'] == want:
                    matches.append(proof)
            if not matches:
                raise VersionError('no readable retained version with the expected hash')
        except (VersionError, ValueError, IndexError) as exc:
            bad.append(f'{label}: UNVERIFIABLE server anchor: {exc}')

    for cid, ce in pk["cards"].items():
        if sha(ce["text"]) != ce["sha256"]:
            bad.append(f"{cid}: embedded text does not hash to its recorded sha256")
        if rec["cards"].get(cid) != ce["sha256"]:
            bad.append(f"{cid}: packet sha256 differs from the manifest")
        anchor(cid, ce["path"], ce["sha256"])
    for s in pk["sources"]:
        if s.get("missing"):
            continue
        if sha(s["text"]) != s["sha256"] or rec["sources"].get(s["id"]) != s["sha256"]:
            bad.append(f"source {s['id']}: hash mismatch")
        anchor(f"source {s['id']}", s["path"], s["sha256"])
    if dt.datetime.fromisoformat(pk["snapshot_stamp"]) > dt.datetime.fromisoformat(pk["fix_applied_at"]):
        bad.append(f"snapshot {pk['snapshot']} is later than the fix ({pk['fix_applied_at']})")
    texts = {cid: ce["text"] for cid, ce in pk["cards"].items()}
    texts.update({s["id"]: s["text"] for s in pk["sources"] if not s.get("missing")})
    for cid, (plus_only, minus_only) in own_fix_lines(pk["night_file"]).items():
        if cid not in texts:
            continue
        front = split_front(texts[cid])[0].splitlines()
        for ln in plus_only:
            if ln in front:
                bad.append(f"{cid}: carries the line its own review fix wrote: {ln.strip()!r}")
        for ln in minus_only:
            if ln not in front:
                bad.append(f"{cid}: lacks the pre-fix line the review's diff removed: {ln.strip()!r}")
    have = set()
    for cid, e in pk["edges"].items():
        for k, ids in e["out"].items():
            have.update((cid, k, x) for x in ids)
        for ie in e["in"]:
            have.add((ie["from"], ie["key"], cid))
    for edge in own_fix_edges(pk["night_file"]) & have:
        bad.append(f"edge {edge} was created by this night's own review fix")
    return bad


def cmd_verify(args):
    out = args.out
    mpath = os.path.join(out, "manifest.json")
    if not os.path.exists(mpath):
        print("VERIFY: no manifest \u2014 nothing frozen", file=sys.stderr)
        return 2
    man = json.load(open(mpath))
    problems = []
    pruned = ()
    lost_cases = {}
    std = os.path.join(out, man["standard"]["path"])
    if fsha(std) != man["standard"]["sha256"]:
        problems.append("standard-v2.md changed since the freeze")
    key = json.load(open(os.path.join(out, "key", "answer-key.json")))
    for name, h in man["key"].items():
        if fsha(os.path.join(out, "key", name)) != h:
            problems.append(f"key/{name} changed since the freeze")
    leak_marks = {cid: k["review_reason"][:80] for cid, k in key.items()}
    n_ok = 0
    for cid, rec in sorted(man["cases"].items()):
        p = os.path.join(out, rec["packet"])
        if fsha(p) != rec["sha256"]:
            problems.append(f"{cid}: packet file changed since the freeze")
            continue
        pk = json.load(open(p, encoding="utf-8"))
        lost = []
        bad = verify_packet(pk, rec, pruned=pruned, lost=lost)
        if lost:
            lost_cases[cid] = lost
        problems += [f"{cid}: {b}" for b in bad]
        for who in LABELLERS:
            sp = os.path.join(out, "sheets", who, f"{cid}.md")
            if not os.path.exists(sp):
                problems.append(f"{cid}: sheet for {who} missing")
                continue
            st = open(sp, encoding="utf-8").read()
            for other, mark in leak_marks.items():
                if len(mark) > 30 and mark in st:
                    problems.append(f"{cid}: {who}'s sheet contains {other}'s review reason")
            if "REVIEW VERDICT" in st or "prior_sonnet_votes" in st:
                problems.append(f"{cid}: {who}'s sheet shows a verdict or a judge answer")
        n_ok += not bad
    for pr in problems:
        print(f"  PROBLEM {pr}")
    for cid, lost in sorted(lost_cases.items()):
        print(f"  ANCHOR-LOST {cid}: {len(lost)} file(s) in a pruned snapshot "
              f"({', '.join(sorted({x.split(': ', 1)[1].split('/', 1)[0] for x in lost}))}) — "
              "UNVERIFIABLE: no independently readable server anchor")
    print(f"VERIFY: {len(man['cases'])} cases · {n_ok} intact · sheets "
          f"{sum(1 for w in LABELLERS if len(man['sheets'].get(w, {})) == len(man['cases']))} · "
          f"problems {len(problems)} · anchors lost {len(lost_cases)}")
    return 0 if not problems and len(man["cases"]) == sum(QUOTAS.values()) else 1


# ----------------------------------------------------------------------------- harness

RELATIONS = ("CONTRADICTS", "UNDERCUTS", "SUPERSEDES", "REFINES", "AGREES", "UNRELATED",
             "INSUFFICIENT")
ACTION_KINDS = ("REFUTE", "CHALLENGE", "ERRATUM", "SUPERSESSION", "LINK_REPAIR",
                "NO_ACTION_ALREADY_DONE", "NO_ACTION", "NEEDS_CONTEXT")
CALL_TIMEOUT = 600


def load_frozen(out):
    man = json.load(open(os.path.join(out, "manifest.json")))
    std = open(os.path.join(out, man["standard"]["path"]), encoding="utf-8").read()
    if sha(std) != man["standard"]["sha256"]:
        raise SystemExit("refused: standard-v2.md changed since the freeze")
    packets = {}
    for cid, rec in sorted(man["cases"].items()):
        p = os.path.join(out, rec["packet"])
        if fsha(p) != rec["sha256"]:
            raise SystemExit(f"refused: {cid} packet changed since the freeze")
        packets[cid] = json.load(open(p, encoding="utf-8"))
    return man, std, packets


def judgment(man, std, pk, arm):
    prompt = judge_prompt(std, pk, arm)
    return {"judgment": f"{pk['case_id']}/{arm}", "case": pk["case_id"], "arm": arm,
            "model": MODEL, "serializer": SERIALIZER,
            "standard_sha256": man["standard"]["sha256"],
            "packet_sha256": man["cases"][pk["case_id"]]["sha256"],
            "prompt_sha256": sha(prompt), "prompt_chars": len(prompt)}, prompt


def parse_answer(txt):
    try:
        s = txt[txt.index("{"):txt.rindex("}") + 1]
        a = json.loads(s)
    except (ValueError, json.JSONDecodeError):
        return None, "BAD-JSON"
    if not isinstance(a, dict):
        return None, "BAD-JSON"
    if a.get("relation") not in RELATIONS or a.get("action_kind") not in ACTION_KINDS:
        return a, "OFF-SCHEMA"
    return a, "OK"


def call_claude(prompt, model=MODEL, timeout=CALL_TIMEOUT):
    """One `claude -p` call (the nightly judge's call shape, kc.claude_cmd); the CLI's own usage
    and total_cost_usd for THIS call are returned with the answer."""
    t0 = time.time()
    try:
        cp = RUNNER(kc.claude_cmd(model), input=prompt, capture_output=True, text=True,
                    timeout=timeout, cwd=tempfile.gettempdir())
    except subprocess.TimeoutExpired:
        return {"outcome": "TIMEOUT", "wall_s": round(time.time() - t0, 1)}
    wall = round(time.time() - t0, 1)
    try:
        r = json.loads(cp.stdout)
        if not isinstance(r, dict):
            raise ValueError
    except ValueError:
        return {"outcome": "CLI-ERROR", "wall_s": wall,
                "error": f"rc={cp.returncode} stdout={(cp.stdout or '')[:200]!r} "
                         f"stderr={(cp.stderr or '')[:200]!r}"}
    u = r.get("usage") or {}
    usage = {k: int(u.get(k) or 0) for k in ("input_tokens", "output_tokens",
                                              "cache_read_input_tokens",
                                              "cache_creation_input_tokens")}
    usage["thinking_tokens"] = int((u.get("output_tokens_details") or {}).get("thinking_tokens") or 0)
    rec = {"wall_s": wall, "usage": usage, "cost_usd": float(r.get("total_cost_usd") or 0.0),
           "models": sorted((m.get("canonicalModel") or k) for k, m in (r.get("modelUsage") or {}).items()),
           "raw": str(r.get("result") or "")[:4000]}
    if cp.returncode != 0 or r.get("is_error"):
        rec["outcome"] = "CLI-ERROR"
        return rec
    rec["answer"], rec["outcome"] = parse_answer(str(r.get("result") or ""))
    return rec


def runs_dir(out):
    d = os.path.join(out, "runs")
    os.makedirs(d, exist_ok=True)
    return d


def load_runs(out, arm):
    p = os.path.join(out, "runs", f"arm-{arm}.jsonl")
    recs = []
    if os.path.exists(p):
        recs = [json.loads(ln) for ln in open(p, encoding="utf-8") if ln.strip()]
    return recs


def cmd_run(args):
    if not args.dry and cmd_verify(args):
        print('RUN: refused: historical inputs are not verified by server versions', file=sys.stderr)
        return 3
    out = args.out
    man, std, packets = load_frozen(out)
    arms = ARMS if args.arm == "all" else (args.arm,)
    cases = sorted(packets) if not args.cases else [c.strip() for c in args.cases.split(",")]
    unknown = [c for c in cases if c not in packets]
    if unknown:
        print(f"refused: unknown cases {unknown}", file=sys.stderr)
        return 2
    if args.dry:
        plan = []
        for arm in arms:
            for cid in cases:
                j, prompt = judgment(man, std, packets[cid], arm)
                plan.append(j)
        p = os.path.join(runs_dir(out), "dry-plan.jsonl")
        with open(p, "w", encoding="utf-8") as fh:
            for j in plan:
                fh.write(json.dumps(j) + "\n")
        by_arm = Counter(j["arm"] for j in plan)
        chars = defaultdict(int)
        for j in plan:
            chars[j["arm"]] += j["prompt_chars"]
        for j in plan:
            print(f"  DRY {j['judgment']:8s} prompt {j['prompt_chars']:7d} chars  "
                  f"sha {j['prompt_sha256'][:12]}")
        print(f"DRY: {len(plan)} judgments ({len(cases)} cases x {len(arms)} arms; "
              + ", ".join(f"arm {a}: {by_arm[a]} prompts, {chars[a] / 1e3:.0f}k chars" for a in arms)
              + f") -> {p}; no model called")
        return 0
    if not args.cases and not args.all:
        print("refused: a live run needs --cases <ids> or an explicit --all", file=sys.stderr)
        return 2
    spent = 0.0
    for arm in arms:
        done = {(r["judgment"], r["prompt_sha256"]) for r in load_runs(out, arm)
                if r.get("outcome") == "OK"}
        p = os.path.join(runs_dir(out), f"arm-{arm}.jsonl")
        with open(p, "a", encoding="utf-8") as fh:
            for cid in cases:
                j, prompt = judgment(man, std, packets[cid], arm)
                if (j["judgment"], j["prompt_sha256"]) in done:
                    print(f"  skip {j['judgment']} (answered with this exact prompt)")
                    continue
                if args.max_usd is not None and spent >= args.max_usd:
                    print(f"STOP: spent ${spent:.4f} >= --max-usd {args.max_usd}", file=sys.stderr)
                    return 3
                rec = call_claude(prompt)
                j.update(rec)
                j["ended"] = dt.datetime.now().astimezone().isoformat(timespec="seconds")
                fh.write(json.dumps(j, ensure_ascii=False) + "\n")
                fh.flush()
                spent += rec.get("cost_usd") or 0.0
                u = rec.get("usage") or {}
                a = rec.get("answer") or {}
                print(f"  {j['judgment']:8s} {rec['outcome']:10s} rel={a.get('relation')} "
                      f"act={a.get('action_kind')} in={u.get('input_tokens', 0)}+cw"
                      f"{u.get('cache_creation_input_tokens', 0)}+cr{u.get('cache_read_input_tokens', 0)} "
                      f"out={u.get('output_tokens', 0)} (think {u.get('thinking_tokens', 0)}) "
                      f"${rec.get('cost_usd', 0):.4f} {rec.get('wall_s')}s {rec.get('models')}")
    print(f"spent ${spent:.4f} (CLI total_cost_usd)")
    return 0


# ----------------------------------------------------------------------------- report

def project(out, plan, runs_by_arm):
    """Projected $ of every judgment in the dry plan, from the live calls measured so far.

    Input: per arm, input tokens per prompt character and the effective $ per input token come
    from that arm's OK calls (the CLI's total_cost_usd minus the output priced by tok_account's
    table \u2014 so the CLI's cache-write multiplier is inside it). Output: the mean over ALL live
    calls, pooled across arms \u2014 with one call per arm the per-arm output (mostly thinking)
    differs by noise, not by arm; the range from the smallest and largest call is reported.
    Returns ({arm: $}, notes); an arm without a live call is not projected."""
    try:
        import tok_account as T
    except ImportError:
        return None, ["tok_account.py not importable \u2014 no projection"]
    res, notes = {}, []
    live_all = [r for a in runs_by_arm for r in runs_by_arm[a]
                if r.get("outcome") == "OK" and r.get("usage")]
    if not live_all:
        return {}, ["no live call measured \u2014 not projected"]
    outs = [r["usage"]["output_tokens"] for r in live_all]
    out_mean = sum(outs) / len(outs)
    lo = hi = 0.0
    for arm in sorted({j["arm"] for j in plan}):
        live = [r for r in live_all if r["arm"] == arm]
        if not live:
            notes.append(f"arm {arm}: no live call measured — not projected")
            continue
        models = sorted({m for r in live for m in r.get("models") or []})
        pr = T.price_for_model(models[0]) if len(models) == 1 else None
        if not pr:
            notes.append(f"arm {arm}: model(s) {models} not priced by tok_account")
            continue
        p_out = pr[1] / 1e6
        tin = sum(r["usage"]["input_tokens"] + r["usage"]["cache_creation_input_tokens"]
                  + r["usage"]["cache_read_input_tokens"] for r in live)
        tout = sum(r["usage"]["output_tokens"] for r in live)
        chars = sum(r["prompt_chars"] for r in live)
        cost = sum(r["cost_usd"] for r in live)
        p_in = (cost - tout * p_out) / tin if tin else 0.0
        tpc = tin / chars
        mine = [j for j in plan if j["arm"] == arm]
        t_in = sum(j["prompt_chars"] * tpc * p_in for j in mine)
        res[arm] = t_in + len(mine) * out_mean * p_out
        lo += t_in + len(mine) * min(outs) * p_out
        hi += t_in + len(mine) * max(outs) * p_out
        notes.append(f"arm {arm}: {len(live)} live call(s): {tpc:.3f} input tok/char, "
                     f"${p_in * 1e6:.3f}/M effective input (CLI-anchored); {len(mine)} judgments: "
                     f"input ${t_in:.2f} + output {out_mean:.0f} tok/call (pooled) "
                     f"${len(mine) * out_mean * p_out:.2f} = ${res[arm]:.2f}")
    notes.append(f"output per call measured {min(outs)}..{max(outs)} tokens over {len(outs)} "
                 f"calls -> total range ${lo:.2f}..${hi:.2f}")
    return res, notes


# ----------------------------------------------------------------------------- final labels

ADJ_FORMAT = CONFIG.get("adjudication_format", "khms-triage-two-axis-adjudication-v1")
LABEL_SOURCES = ("adjudicated", "two_label_agreement")
AXES = ("relation", "action_kind")
ACTIVE_OPS = ("REFUTE", "CHALLENGE", "ERRATUM", "SUPERSESSION", "LINK_REPAIR")
NO_OPS = ("NO_ACTION", "NO_ACTION_ALREADY_DONE")


def _refuse(msg):
    raise SystemExit(f"refused: final labels — {msg}")


def load_final_labels(path, out, manifest_path=None):
    """The complete final label set of the 40 cases, imported fail-closed (the independent
    notes-format-adjudicate-sol.md): the JSONL must carry the sha256 its adjudication manifest
    records; every input the adjudication read (packets, both sheets, the standard) must be
    unchanged since; exactly one row per frozen case, valid enums on both axes, a reason on
    every row; a `two_label_agreement` row must equal BOTH blind sheets on both axes, and the
    `adjudicated` rows must be exactly the cases the sheets disagree on (= the manifest's lists).
    Returns ({case: row}, info). Reads cases/ and sheets/ only \u2014 never key/ or runs/."""
    mpath = manifest_path or os.path.join(os.path.dirname(os.path.abspath(path)),
                                          "adjudicated-manifest.json")
    if not os.path.exists(path) or not os.path.exists(mpath):
        _refuse(f"{path} or its manifest {mpath} is missing")
    man_a = json.load(open(mpath, encoding="utf-8"))
    if man_a.get("format") != ADJ_FORMAT:
        _refuse(f"manifest format {man_a.get('format')!r} is not {ADJ_FORMAT}")
    want = (man_a.get("outputs_sha256") or {}).get(os.path.basename(path))
    got = fsha(path)
    if not want or got != want:
        _refuse(f"sha256 of {os.path.basename(path)} is {got[:12]}, the manifest records "
                f"{(want or 'none')[:12]} — the label file changed after the adjudication")
    adir = os.path.dirname(os.path.abspath(path))
    for name, h in (man_a.get("outputs_sha256") or {}).items():   # the human record too
        p = os.path.join(adir, name)
        if not os.path.exists(p) or fsha(p) != h:
            _refuse(f"{name} is missing or its sha256 differs from the manifest")
    # every frozen input the adjudication read: mapped onto THIS frozen directory
    frozen_root = None
    for p in man_a.get("inputs_sha256") or {}:
        if re.search(r"/cases/JX-\d\d\.json$", p):
            frozen_root = os.path.dirname(os.path.dirname(p))
            break
    if frozen_root is None:
        _refuse("the manifest lists no frozen case packet among its inputs")
    for p, h in (man_a.get("inputs_sha256") or {}).items():
        local = (os.path.join(out, os.path.relpath(p, frozen_root))
                 if p.startswith(frozen_root + os.sep) else p)
        if not os.path.exists(local) or fsha(local) != h:
            _refuse(f"{local} changed since the adjudication (or is missing)")
    man = json.load(open(os.path.join(out, "manifest.json")))
    cases = sorted(man["cases"])
    rows, seen = {}, Counter()
    for i, ln in enumerate(open(path, encoding="utf-8"), 1):
        if not ln.strip():
            continue
        try:
            r = json.loads(ln)
        except json.JSONDecodeError:
            _refuse(f"line {i} is not JSON")
        cid = r.get("case_id", r.get("case"))
        seen[cid] += 1
        if r.get("relation") not in RELATIONS or r.get("action_kind") not in ACTION_KINDS:
            _refuse(f"{cid}: enum relation={r.get('relation')!r} action_kind="
                    f"{r.get('action_kind')!r} is not in the standard")
        if not str(r.get("why") or "").strip():
            _refuse(f"{cid}: blank why")
        if r.get("label_source") not in LABEL_SOURCES:
            _refuse(f"{cid}: label_source {r.get('label_source')!r}")
        rows[cid] = r
    dup = sorted(c for c, n in seen.items() if n > 1)
    if dup:
        _refuse(f"duplicate case ids {dup}")
    missing = sorted(set(cases) - set(rows))
    extra = sorted(set(rows) - set(cases))
    if missing or extra:
        _refuse(f"missing case ids {missing}, extra {extra}")
    sheets = {w: {c: parse_sheet(os.path.join(out, "sheets", w, f"{c}.md")) or {} for c in cases}
              for w in LABELLERS}
    agree = {ax: sum(sheets[LABELLERS[0]][c].get(ax) == sheets[LABELLERS[1]][c].get(ax)
                     for c in cases) for ax in AXES}
    disputed = set()
    for c in cases:
        s0, s1 = (sheets[w][c] for w in LABELLERS)
        both = all(s0.get(ax) == s1.get(ax) for ax in AXES)
        r = rows[c]
        if r["label_source"] == "two_label_agreement":
            if not both or any(r[ax] != s0.get(ax) for ax in AXES):
                _refuse(f"{c}: agreement row {r['relation']}/{r['action_kind']} is not what the "
                        f"sheets say ({s0.get('relation')}/{s0.get('action_kind')} vs "
                        f"{s1.get('relation')}/{s1.get('action_kind')})")
        else:
            disputed.add(c)
            if both:
                _refuse(f"{c}: adjudicated although the sheets agree on both axes")
    listed = set(man_a.get("relation_disagreements") or []) | set(
        man_a.get("operation_disagreements") or [])
    if disputed != listed or len(disputed) != man_a.get("adjudicated_count"):
        _refuse(f"adjudicated ids {sorted(disputed)} differ from the manifest's {sorted(listed)}")
    info = {"path": os.path.abspath(path), "sha256": got, "manifest": mpath,
            "manifest_sha256": fsha(mpath), "adjudicated": sorted(disputed),
            "pre_agreement": {ax: (agree[ax], len(cases)) for ax in AXES}, "sheets": sheets}
    return rows, info


def is_active(kind):
    return kind in ACTIVE_OPS


def miss_cause(final, by_arm, exact=False):
    """Why a stably-missed APPLY was missed (the independent reviewer review \u00a74): `label` when the final label
    needs no operation (the old APPLY was the error), `needs_context` when even the labellers
    could not choose one, else the smallest arm whose judge asks for an operation \u2014 A = the
    standard alone fixed it (`standard`), B = the cut text (`text`), C = the context
    (`context`) \u2014 provided every larger arm keeps it; a larger arm that loses it again, or no arm
    finding it, is `judge` (the judge failed although the fullest packet was there).
    `exact=True` counts an arm as finding it only with the final action_kind itself (not just
    some operation). Returns (cause, note)."""
    if final["action_kind"] in NO_OPS:
        return "label", "final label needs no operation"
    if final["action_kind"] == "NEEDS_CONTEXT":
        return "needs_context", "final label itself is NEEDS_CONTEXT"
    got = [(by_arm.get(a) or {}).get("action_kind") for a in ARMS]
    found = [g == final["action_kind"] if exact else is_active(g) for g in got]
    if not any(found):
        return "judge", "no arm asks for an operation"
    first = found.index(True)
    if not all(found[first:]):
        return "judge", f"found in arm {ARMS[first]} but lost again in a fuller arm"
    return {0: "standard", 1: "text", 2: "context"}[first], f"first found in arm {ARMS[first]}"


def _pct(n, d):
    return f"{100.0 * n / d:.1f}%" if d else "n/a"


def _sign_p(b, c):
    """Exact two-sided sign test (McNemar) on b gained vs c lost discordant pairs."""
    from math import comb
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    return min(1.0, 2 * sum(comb(n, i) for i in range(k + 1)) / 2 ** n)


def report_final(packets, runs, labels, info, key):
    """The tables of the unblinding (the independent reviewer review \u00a74-\u00a75). Returns the verdict dict."""
    cases = sorted(packets)
    last = {}
    for a, recs in runs.items():
        for r in recs:
            last[(r["case"], a)] = r
    ans = {(c, a): ((last.get((c, a)) or {}).get("answer") or {}) if
           (last.get((c, a)) or {}).get("outcome") == "OK" else {} for c in cases for a in ARMS}
    hit = {(c, a, ax): ans[(c, a)].get(ax) == labels[c][ax]
           for c in cases for a in ARMS for ax in AXES}
    binf = {c: is_active(labels[c]["action_kind"]) for c in cases}
    print(f"\nfinal labels: {info['path']} sha256 {info['sha256'][:16]} (manifest "
          f"{info['manifest_sha256'][:16]}) — {len(cases) - len(info['adjudicated'])} two-sheet "
          f"agreement + {len(info['adjudicated'])} adjudicated {','.join(info['adjudicated'])}")
    pa = info["pre_agreement"]
    print(f"human agreement BEFORE adjudication (kept, not 100%): relation "
          f"{pa['relation'][0]}/{pa['relation'][1]} = {_pct(*pa['relation'])} · operation "
          f"{pa['action_kind'][0]}/{pa['action_kind'][1]} = {_pct(*pa['action_kind'])}")
    bad = [f"{c}/{a}" for c in cases for a in ARMS if not ans[(c, a)]]
    if bad:
        print(f"unscored (no OK answer, counted as a miss): {bad}")
    cats = sorted({key[c]["category"] for c in cases}) if key else []
    print("\n## per arm and axis vs FINAL labels (n=40, one judgment per case and arm)")
    print("| arm | relation | operation (exact) | both axes | op needed? (binary) | $ CLI |")
    print("|---|---|---|---|---|---|")
    acc = {}
    for a in ARMS:
        r = sum(hit[(c, a, "relation")] for c in cases)
        o = sum(hit[(c, a, "action_kind")] for c in cases)
        bth = sum(hit[(c, a, "relation")] and hit[(c, a, "action_kind")] for c in cases)
        bn = sum(is_active(ans[(c, a)].get("action_kind")) == binf[c] for c in cases)
        usd = sum(rr.get("cost_usd") or 0.0 for rr in runs[a])
        acc[a] = (r, o, usd)
        print(f"| {a} | {r}/40 {_pct(r, 40)} | {o}/40 {_pct(o, 40)} | {bth}/40 {_pct(bth, 40)} "
              f"| {bn}/40 {_pct(bn, 40)} | ${usd:.2f} |")
    if cats:
        print("\nper category (relation / operation hits):")
        print("| category | n | " + " | ".join(f"arm {a}" for a in ARMS) + " |")
        print("|---|---|" + "---|" * len(ARMS))
        for cat in cats:
            cc = [c for c in cases if key[c]["category"] == cat]
            print(f"| {cat} | {len(cc)} | " + " | ".join(
                f"{sum(hit[(c, a, 'relation')] for c in cc)} / "
                f"{sum(hit[(c, a, 'action_kind')] for c in cc)}" for a in ARMS) + " |")
        src = {s: [c for c in cases if labels[c]["label_source"] == s] for s in LABEL_SOURCES}
        print("\nper label source (relation / operation hits):")
        for s, cc in src.items():
            print(f"  {s:20s} n={len(cc):2d} " + "  ".join(
                f"{a}: {sum(hit[(c, a, 'relation')] for c in cc)}/"
                f"{sum(hit[(c, a, 'action_kind')] for c in cc)}" for a in ARMS))
    print("\n## paired per-case changes (miss->hit gained, hit->miss lost; exact sign test)")
    print("| pair | axis | gained | lost | net | p | gained cases | lost cases |")
    print("|---|---|---|---|---|---|---|---|")
    for x, y in (("A", "B"), ("B", "C"), ("A", "C")):
        for ax in AXES:
            g = [c for c in cases if not hit[(c, x, ax)] and hit[(c, y, ax)]]
            lo = [c for c in cases if hit[(c, x, ax)] and not hit[(c, y, ax)]]
            print(f"| {x}->{y} | {ax} | {len(g)} | {len(lo)} | {len(g) - len(lo):+d} | "
                  f"{_sign_p(len(g), len(lo)):.2f} | {','.join(g) or '-'} | {','.join(lo) or '-'} |")
    print("\nper case (final label | arm A | arm B | arm C; * = hit on that axis):")
    for c in cases:
        f = labels[c]
        cells = []
        for a in ARMS:
            an = ans[(c, a)]
            cells.append(f"{an.get('relation', '-')}{'*' if hit[(c, a, 'relation')] else ''}/"
                         f"{an.get('action_kind', '-')}{'*' if hit[(c, a, 'action_kind')] else ''}")
        tag = (key[c]["category"][:6] + " " + key[c]["original_verdict"]) if key else ""
        print(f"  {c} {tag:13s} {f['relation']}/{f['action_kind']}"
              f"{' (adj)' if f['label_source'] == 'adjudicated' else ''} | " + " | ".join(cells))
    causes, causes_exact = Counter(), Counter()
    if key:
        sm = [c for c in cases if key[c]["category"] == "stable_miss_apply"]
        print(f"\n## stably-missed cases ({len(sm)}): why — final label, each arm's operation, "
              f"labellers' evidence_sufficient")
        print("| case | orig | final relation/op | A | B | C | cause (any op) | note | cause (exact op) "
              "| labellers ev.suff. |")
        print("|---|---|---|---|---|---|---|---|---|---|")
        for c in sm:
            cause, note = miss_cause(labels[c], {a: ans[(c, a)] for a in ARMS})
            causes[cause] += 1
            ex, _ = miss_cause(labels[c], {a: ans[(c, a)] for a in ARMS}, exact=True)
            causes_exact[ex] += 1
            ev = "/".join({True: "yes", False: "no"}.get(v, str(v)) for v in (
                (info["sheets"][w][c] or {}).get("evidence_sufficient") for w in LABELLERS))
            print(f"| {c} | {key[c]['original_verdict']} {key[c]['verdict_class'] or ''} | "
                  f"{labels[c]['relation']}/{labels[c]['action_kind']} | "
                  + " | ".join(f"{ans[(c, a)].get('relation', '-')}/{ans[(c, a)].get('action_kind', '-')}"
                               for a in ARMS) + f" | **{cause}** | {note} | {ex} | {ev} |")
        print("  causes (any op = the verdict line): " + ", ".join(
            f"{k}={v}" for k, v in sorted(causes.items())) + " \u00b7 exact op: " + ", ".join(
            f"{k}={v}" for k, v in sorted(causes_exact.items())))
        print("\n## vs ORIGINAL labels (the morning review's APPLY/REJECT; op needed = "
              "REFUTE|CHALLENGE|ERRATUM|SUPERSESSION|LINK_REPAIR, NEEDS_CONTEXT = kept for review)")
        apply_ = [c for c in cases if key[c]["original_verdict"] == "APPLY"]
        rej = [c for c in cases if key[c]["original_verdict"] == "REJECT"]
        kept = {(c, a): ans[(c, a)].get("action_kind") in ACTIVE_OPS + ("NEEDS_CONTEXT",)
                for c in cases for a in ARMS}
        print("| who | APPLY kept | REJECT filtered | agreement | stable-miss APPLY kept |")
        print("|---|---|---|---|---|")
        sm_apply = [c for c in apply_ if key[c]["category"] == "stable_miss_apply"]
        for a in ARMS:
            k_ = sum(kept[(c, a)] for c in apply_)
            f_ = sum(not kept[(c, a)] for c in rej)
            print(f"| arm {a} | {k_}/{len(apply_)} {_pct(k_, len(apply_))} | {f_}/{len(rej)} "
                  f"{_pct(f_, len(rej))} | {k_ + f_}/40 {_pct(k_ + f_, 40)} | "
                  f"{sum(kept[(c, a)] for c in sm_apply)}/{len(sm_apply)} |")
        fk = {c: labels[c]["action_kind"] in ACTIVE_OPS + ("NEEDS_CONTEXT",) for c in cases}
        k_ = sum(fk[c] for c in apply_)
        f_ = sum(not fk[c] for c in rej)
        print(f"| FINAL labels | {k_}/{len(apply_)} {_pct(k_, len(apply_))} | {f_}/{len(rej)} "
              f"{_pct(f_, len(rej))} | {k_ + f_}/40 {_pct(k_ + f_, 40)} | "
              f"{sum(fk[c] for c in sm_apply)}/{len(sm_apply)} |")
        flips = [f"{c} {key[c]['original_verdict']}->{labels[c]['action_kind']}" for c in cases
                 if fk[c] != (key[c]["original_verdict"] == "APPLY")]
        print(f"label changes original -> final ({len(flips)}): {'; '.join(flips) or '-'}")
        print("\nexact two-axis hits vs each labeller's ORIGINAL blind sheet (pre-adjudication):")
        for w in LABELLERS:
            sh = info["sheets"][w]
            print(f"  {w:7s} " + "  ".join(
                f"arm {a}: rel {sum(ans[(c, a)].get('relation') == sh[c].get('relation') for c in cases)}/40"
                f" op {sum(ans[(c, a)].get('action_kind') == sh[c].get('action_kind') for c in cases)}/40"
                for a in ARMS))
    # best = most hits over both axes; a tie goes to the cheaper arm (CLI $)
    best = sorted(ARMS, key=lambda a: (-(acc[a][0] + acc[a][1]), acc[a][2]))[0]
    return {"acc": acc, "best": best, "causes": causes, "n": len(cases)}


def verdict_line(v):
    acc, c = v["acc"], v["causes"]
    extra = "".join(f",{k}:{n}" for k, n in sorted(c.items())
                    if k not in ("label", "text", "context", "judge") and n)
    return ("JUDGEUNBLIND-VERDICT: "
            + " ".join(f"arm{a}_rel={_pct(acc[a][0], v['n'])}" for a in ARMS) + " "
            + " ".join(f"arm{a}_op={_pct(acc[a][1], v['n'])}" for a in ARMS)
            + f" best={v['best']} stable_miss_causes=label:{c['label']},text:{c['text']},"
              f"context:{c['context']},judge:{c['judge']}{extra}")


def cmd_report(args):
    out = args.out
    man, std, packets = load_frozen(out)
    runs = {a: load_runs(out, a) for a in ARMS}
    last = {}
    for a, recs in runs.items():
        for r in recs:
            last[(r["case"], a)] = r
    final = info = None
    if getattr(args, "final_labels", None):    # fail closed BEFORE anything is printed
        final, info = load_final_labels(args.final_labels, out, args.labels_manifest)
    key = json.load(open(os.path.join(out, "key", "answer-key.json"))) if args.unblind else {}
    print("case   " + ("orig    " if key else "") + " | ".join(f"arm {a}: relation / action" for a in ARMS))
    for cid in sorted(packets):
        cells = []
        for a in ARMS:
            r = last.get((cid, a))
            ans = (r or {}).get("answer") or {}
            cells.append(f"{ans.get('relation', '-')}/{ans.get('action_kind', '-')}"
                         if r else "-")
        print(f"{cid}  " + (f"{key[cid]['original_verdict']:7s} " if key else "") + " | ".join(cells))
    tot = defaultdict(float)
    for a, recs in runs.items():
        for r in recs:
            u = r.get("usage") or {}
            for k, v in u.items():
                tot[(a, k)] += v
            tot[(a, "cost")] += r.get("cost_usd") or 0.0
            tot[(a, "calls")] += 1
        if recs:
            print(f"arm {a}: {int(tot[(a, 'calls')])} live calls · in {int(tot[(a, 'input_tokens')])} "
                  f"+ cache w {int(tot[(a, 'cache_creation_input_tokens')])} / r "
                  f"{int(tot[(a, 'cache_read_input_tokens')])} · out {int(tot[(a, 'output_tokens')])} "
                  f"(thinking {int(tot[(a, 'thinking_tokens')])}) · ${tot[(a, 'cost')]:.4f} CLI")
    plan_p = os.path.join(out, "runs", "dry-plan.jsonl")
    if os.path.exists(plan_p):
        plan = [json.loads(ln) for ln in open(plan_p) if ln.strip()]
        res, notes = project(out, plan, runs)
        for n in notes:
            print(f"  projection {n}")
        if res:
            print(f"projected for {len(plan)} judgments: ${sum(res.values()):.2f} "
                  f"({', '.join(f'{a} ${v:.2f}' for a, v in sorted(res.items()))}) — an estimate "
                  f"from {sum(1 for a in ARMS for r in runs[a] if r.get('outcome') == 'OK')} live calls")
    if final is not None:
        v = report_final(packets, runs, final, info, key)
        if key:
            print("\n" + verdict_line(v))
        else:
            print("\n(stably-missed causes and the verdict line need --unblind)")
    return 0


def cmd_verdict(args):
    out = args.out
    rc_v = cmd_verify(args)
    man, std, packets = load_frozen(out)
    n_cases = len(packets) if rc_v == 0 else 0
    sheets = 0
    for who in LABELLERS:
        if all(os.path.exists(os.path.join(out, "sheets", who, f"{c}.md")) for c in packets):
            sheets += 1
    plan_p = os.path.join(out, "runs", "dry-plan.jsonl")
    dry_ok, arms_n, plan = False, 0, []
    if os.path.exists(plan_p):
        plan = [json.loads(ln) for ln in open(plan_p) if ln.strip()]
        current = {}
        for arm in ARMS:
            for cid, pk in packets.items():
                j, _ = judgment(man, std, pk, arm)
                current[j["judgment"]] = j["prompt_sha256"]
        dry_ok = (len(plan) == len(current) == 3 * len(packets)
                  and all(current.get(j["judgment"]) == j["prompt_sha256"] for j in plan))
        arms_n = sum(1 for a in ARMS if sum(j["arm"] == a for j in plan) == len(packets))
    runs = {a: load_runs(out, a) for a in ARMS}
    res, _ = project(out, plan, runs) if plan else (None, [])
    proj = sum(res.values()) if res and len(res) == len(ARMS) else None
    print(f"JUDGEEXP-VERDICT: cases={n_cases} sheets={sheets} arms={arms_n} "
          f"dry={'ok' if dry_ok else 'fail'} projected_usd="
          f"{f'{proj:.2f}' if proj is not None else 'none'}")
    ok = (n_cases == sum(QUOTAS.values()) and sheets == len(LABELLERS) and arms_n == len(ARMS)
          and dry_ok and proj is not None)
    return 0 if ok else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", default=OUT)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("build")
    sub.add_parser("verify")
    r = sub.add_parser("run")
    r.add_argument("--arm", required=True, choices=ARMS + ("all",))
    r.add_argument("--dry", action="store_true", help="build every prompt, call nothing")
    r.add_argument("--cases", help="comma-separated case ids (live runs need this or --all)")
    r.add_argument("--all", action="store_true", help="live run over every case")
    r.add_argument("--max-usd", type=float, default=None, help="stop before a call past this")
    p = sub.add_parser("report")
    p.add_argument("--unblind", action="store_true",
                   help="show the original verdict column (only after labelling is finished)")
    p.add_argument("--final-labels", metavar="JSONL",
                   help="complete 40-case final label set (two-sheet agreement + adjudication), "
                        "hash-verified against its adjudicated-manifest.json; scores every arm")
    p.add_argument("--labels-manifest", metavar="JSON", default=None,
                   help="the adjudication manifest (default: adjudicated-manifest.json beside "
                        "the label file)")
    sub.add_parser("verdict")
    args = ap.parse_args(argv)
    return {"build": cmd_build, "verify": cmd_verify, "run": cmd_run, "report": cmd_report,
            "verdict": cmd_verdict}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
