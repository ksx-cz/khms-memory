#!/usr/bin/env python3
"""KHMS view/index generator. Deterministic, zero LLM tokens.

Usage: python3 tools/build_views.py   (no arguments; run after every card write)

Reads memory/know/K-*.md, validates against the spec (plans/khms-spec.md),
computes belief and condense_score, regenerates views/ and MEMORY.md.
Exit 1 on schema violations (bad YAML, derived without derived_from, id mismatch)
and on a half-closed `supersedes` edge (target still served as current, issue #209).
"""
import glob
import os
import re
import sys
import math
import datetime

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import khms_lint  # noqa: E402  (errata rules R1/R2 — see khms_lint.py, 2026-09-20)

import khms_paths as P  # noqa: E402

BRAIN = P.ROOT
MEM = os.path.join(BRAIN, "memory")
KNOW = os.path.join(MEM, "know")
VIEWS = os.path.join(MEM, "views")
# Captured at import. Tests (test_khms_erratum.py, test_type_gate.py) re-point
# KNOW at a temp base AFTER importing this module, and the dense-index refresh
# in main() must then do nothing at all — the live index has no business being
# rewritten from a fixture.
REAL_KNOW = KNOW
DENSE_MODEL = os.environ.get("KHMS_DENSE_MODEL", "bge")

# --- configuration (spec §7) -------------------------------------------------
EVIDENCE_WEIGHT = {"measured": 3, "observed": 2, "reported": 1}
W_ASSUMPTION = 1
W_INDIRECT = 2          # active derived card cited as support
K_SLOPE = 4             # belief = tanh((support-oppose)/K_SLOPE)
F_UNABSORBED = 0.3
OBS_TYPES = {"action→outcome", "problem→solution", "fact", "requirement", "decision→rationale"}
DER_TYPES = {"principle", "policy", "goal→method", "overview"}
# §4.2's table is a type AND the level of that type: `problem→solution` IS an
# observation, `principle` IS derived.  Both sets were declared here from the
# start and READ BY NOTHING until 2026-09-20 (package TYPEGATE): only `level` and
# `status` were validated, so the 2026-09-20 nightly's `type: observation
# (problem→solution)` — a shape 0 of 2,851 cards in know/ use — would have been
# approved unnoticed on all 20 cards, and one of them at a level its own type
# contradicts.  The type is not decoration: triage reads it (a principle or a
# decision cannot be the winner of a factual contradiction), the
# condense score reads it (`fact` is protected), and cards are immutable once
# approved, so a wrong one stays wrong for good.
TYPES = OBS_TYPES | DER_TYPES
LEVEL_OF_TYPE = dict.fromkeys(OBS_TYPES, "observation")
LEVEL_OF_TYPE.update(dict.fromkeys(DER_TYPES, "derived"))
LEVELS = {"observation", "derived", "assumption"}
STATUSES = {"active", "challenged", "refuted", "superseded", "condensed"}
# The gate is HARD on everything approved from now on and SOFT on history — but
# the softness is a finite, frozen LIST of ids, not a rule that also excuses
# tomorrow's card.  See the file's own header.
# A base-specific frozen list (private configuration); absent = nothing grandfathered.
LEGACY_TYPES_FILE = os.path.join(P.CONFIG_DIR, "khms_type_legacy.txt")
TOP_PATTERNS = 5
RECENT_DAYS = 30


def fail(msg):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def load_legacy_types(path=None):
    """-> {id: 'type/level'} for cards approved before the type gate existed.

    A per-ID AND per-VALUE exception: the pair has to still match, so editing a
    grandfathered card into a different invalid pair fails like any other card.
    A missing file is NOT a silent pass — it means no exceptions, and then the
    gate is hard on the whole base, which is loud by construction.
    """
    out = {}
    path = path or LEGACY_TYPES_FILE
    if not os.path.exists(path):
        return {}                      # optional private list: absent = nothing grandfathered
    for line in open(path, encoding="utf-8"):
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) >= 2:
            out[parts[0]] = parts[1]
    return out


def type_problem(meta):
    """-> why this card's type/level pair violates §4.2, or None.

    `assumption` is orthogonal (§4.3: a hypothesis not yet evidenced), so it is
    accepted under any type; the type itself still has to be one of the nine.
    """
    typ, level = meta.get("type"), meta.get("level")
    if typ not in TYPES:
        return (f"type '{typ}' is not one of the nine of spec §4.2 "
                f"({', '.join(sorted(TYPES))})")
    want = LEVEL_OF_TYPE[typ]
    if level not in (want, "assumption"):
        return f"type '{typ}' is a {want}-level type (§4.2) but level is '{level}'"
    return None


def load_cards():
    cards = {}
    legacy = load_legacy_types()
    grandfathered = set()
    for path in sorted(glob.glob(os.path.join(KNOW, "K-*.md"))):
        with open(path, encoding="utf-8") as f:
            text = f.read()
        m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
        if not m:
            fail(f"{path}: no frontmatter")
        try:
            meta = yaml.safe_load(m.group(1))
        except yaml.YAMLError as e:
            fail(f"{path}: bad YAML: {e}")
        body = m.group(2).strip()
        cid = meta.get("id")
        if cid != os.path.basename(path)[:-3]:
            fail(f"{path}: id '{cid}' != filename")
        for req in ("type", "level", "status", "tags", "scope", "date"):
            if req not in meta:
                fail(f"{cid}: missing '{req}'")
        if meta["level"] not in LEVELS:
            fail(f"{cid}: bad level {meta['level']}")
        if meta["status"] not in STATUSES:
            fail(f"{cid}: bad status {meta['status']}")
        bad_type = type_problem(meta)
        if bad_type:
            if legacy.get(cid) == f"{meta.get('type')}/{meta.get('level')}":
                grandfathered.add(cid)
            else:
                fail(f"{cid}: {bad_type}. A new card must carry one of the nine. "
                     f"A genuinely NEW type is a decision card + a row in spec §4.2 "
                     f"+ a member of OBS_TYPES/DER_TYPES here — never a new row in "
                     f"{LEGACY_TYPES_FILE}, which is frozen "
                     f"history and may only shrink")
        links = meta.get("links") or {}
        for k in ("derived_from", "supports", "contradicts", "refuted_by"):
            links.setdefault(k, [])
            if links[k] is None:
                links[k] = []
        links.setdefault("supersedes", None)
        if meta["level"] == "derived" and not links["derived_from"]:
            fail(f"{cid}: derived card with empty derived_from")
        if meta["level"] == "observation" and "evidence" not in meta:
            fail(f"{cid}: observation without evidence")
        meta["links"] = links
        meta["body"] = body
        lines = body.splitlines()
        title = lines[0] if lines else "(empty)"
        # requirement cards: WHAT line is the informative one
        if meta.get("type") == "requirement":
            title = next((l for l in lines if l.startswith("WHAT:")), title)
        meta["title"] = title[:120]
        cards[cid] = meta
    # Say what the exception list actually bought, both ways: how many cards
    # needed it, and which rows no longer do (a row whose card was repaired,
    # renumbered or never existed).  A list that only shrinks has to be visibly
    # shrinkable.
    if grandfathered:
        print(f"type gate: {len(grandfathered)} card(s) predate the gate and are "
              f"grandfathered by {LEGACY_TYPES_FILE}")
    stale = sorted(set(legacy) - grandfathered)
    if stale:
        print(f"type gate: {len(stale)} exception row(s) no longer needed, delete "
              f"them: {', '.join(stale[:10])}{' …' if len(stale) > 10 else ''}")
    # Errata (2026-09-20, package KBFIX): a claim retracted into the tombstone ledger may not
    # stand unmarked in ANY card (R1), and a card's `errata:` head, its ERRATUM blocks and the
    # ledger must agree (R2). Loud on purpose: between `khms_erratum.py declare` and `apply`
    # this build FAILS, which is the check being a check (a check that passes either way is
    # no check) — and a nightly card that
    # re-derives a retracted sentence from the journal fails here too, not in a review's eye.
    retracted = khms_lint.load_retracted(BRAIN)
    ledger_ids = {t["ts"] for t in retracted}
    ledger_owner = {t["ts"]: t["was"] for t in retracted}
    for cid, c in cards.items():
        for msg in (khms_lint.check_retracted_claims(cid, c["body"], retracted),
                    khms_lint.check_errata_consistency(cid, c, c["body"], ledger_ids,
                                                       ledger_owner)):
            if msg:
                fail(msg)
    n_err = sum(1 for c in cards.values() if c.get("errata"))
    if retracted or n_err:
        print(f"errata: {len(retracted)} tombstone(s) in the ledger, {n_err} card(s) carry "
              f"an ERRATUM block, no card states a retracted claim")
    half = supersede_problems(cards, load_archived_links())
    if half:
        fail(f"{len(half)} half-closed `supersedes` edge(s) — the old card is still served "
             f"as current:\n  " + "\n  ".join(half)
             + "\n  Close each with approve_inbox.mark_superseded([(NEW, [OLD])]) — never by "
               "hand; a target that is wrong only in one claim takes an erratum "
               "(tools/khms_erratum.py), one retired whole takes apply_triage refute.")
    return cards


def _ids(v):
    """A link field as a list of ids: None, 'K-x' and [K-x, …] all occur in know/."""
    if not v:
        return []
    return [str(v)] if isinstance(v, str) else [str(x) for x in v]


def load_archived_links():
    """{id: links} of the fog (memory/archive/know/, beside KNOW). Retired cards live
    there; an edge into it is closed by the move itself (khms_lint counts the same dir
    as known for the same reason)."""
    out = {}
    arch = os.path.join(os.path.dirname(KNOW), "archive", "know")
    for path in sorted(glob.glob(os.path.join(arch, "K-*.md"))):
        m = re.match(r"^---\n(.*?)\n---\n", open(path, encoding="utf-8").read(), re.S)
        try:
            meta = yaml.safe_load(m.group(1)) if m else {}
        except yaml.YAMLError:
            meta = {}
        out[os.path.basename(path)[:-3]] = (meta or {}).get("links") or {}
    return out


def supersede_problems(cards, archived=None):
    """-> [message] for every `supersedes` edge whose back half is open.

    `NEW supersedes: OLD` written on the new card alone leaves OLD `status: active`, and
    retrieval keeps serving it as current. The back half used to be closed only on the
    inbox path (approve_inbox.mark_superseded); two cards written by hand in the source
    deployment left both their targets active and this build stayed green. The edge is
    CLOSED when OLD is:
      * `superseded` with `superseded_by` naming NEW — or naming another card that itself
        declares `supersedes: OLD` (two successors, one scalar pointer);
      * `refuted` — retired whole by apply_triage, never served as current;
      * absent from know/ but present in the fog (archive/know/): moved out of the base.
    Anything else — active, challenged, superseded without a pointer, a pointer to a card
    that does not supersede OLD, an edge to no card at all — is a message naming both ids.
    An `errata:` key on OLD is NOT an exemption: an erratum corrects a wrong record in an
    otherwise true card that stays active, and the ledger names no correcting card, so the
    exemption could not be tied to NEW — it once spared a card whose title cause a later
    card refutes, until a review closed it.
    """
    archived = archived or {}
    successors = {}
    every_links = {cid: c.get("links") or {} for cid, c in cards.items()}
    every_links.update({cid: l for cid, l in archived.items() if cid not in cards})
    for cid, links in every_links.items():
        for old in _ids(links.get("supersedes")):
            successors.setdefault(old, set()).add(cid)
    out = []
    for new_id in sorted(cards):
        for old in _ids(cards[new_id]["links"].get("supersedes")):
            if old not in cards:
                if old not in archived:
                    out.append(f"{new_id} supersedes {old}: no such card in know/ or "
                               f"the archive")
                continue
            t = cards[old]
            status = t.get("status")
            back = _ids((t.get("links") or {}).get("superseded_by"))
            if status == "refuted":
                continue
            if status == "superseded" and (new_id in back
                                           or any(b in successors.get(old, ()) for b in back)):
                continue
            if status == "superseded" and back:
                why = (f"is superseded_by {', '.join(back)}, which does not declare "
                       f"`supersedes: {old}`")
            else:
                why = f"is status '{status}' with superseded_by {back[0] if back else 'empty'}"
            out.append(f"{new_id} supersedes {old}, but {old} {why}")
    return out


def load_registry():
    """tags.md: | tag | group | description | aliases | (count col regenerated)."""
    reg, alias = {}, {}
    path = os.path.join(VIEWS, "tags.md")
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) >= 4 and cells[0] not in ("tag", ":---", "---") and not cells[0].startswith(":"):
                tag, group, desc = cells[0], cells[1], cells[2]
                aliases = [a.strip() for a in cells[3].split(",") if a.strip() and a.strip() != "—"]
                if tag and not tag.startswith("#"):
                    reg[tag] = {"group": group, "desc": desc, "aliases": aliases}
                    for a in aliases:
                        alias[a] = tag
    return reg, alias


def weight(card):
    if card["level"] == "observation":
        return EVIDENCE_WEIGHT.get(card.get("evidence"), 1)
    if card["level"] == "assumption":
        return W_ASSUMPTION
    return W_INDIRECT


def compute(cards):
    # symmetric contradicts map
    contra = {cid: set(c["links"]["contradicts"]) for cid, c in cards.items()}
    for cid, c in cards.items():
        for other in c["links"]["contradicts"]:
            if other in contra:
                contra[other].add(cid)
    # supports directed toward target
    incoming_support = {cid: set() for cid in cards}
    for cid, c in cards.items():
        for target in c["links"]["supports"]:
            if target in incoming_support:
                incoming_support[target].add(cid)
    today = datetime.date.today()
    for cid, c in cards.items():
        active = lambda x: x in cards and cards[x]["status"] == "active"
        if c["level"] == "derived":
            sources = set(c["links"]["derived_from"]) | incoming_support[cid]
            support = sum(weight(cards[s]) for s in sources if active(s))
            opp_ids = contra[cid] | set(c["links"]["refuted_by"])
            oppose = sum(weight(cards[o]) for o in opp_ids if active(o))
            c["belief"] = math.tanh((support - oppose) / K_SLOPE)
            c["evi"] = (sum(1 for s in sources if active(s)), sum(1 for o in opp_ids if active(o)))
        else:
            c["belief"] = None
        # condense score (observations only; conservative protections)
        protected = (
            c["level"] != "observation"
            or c["status"] in ("refuted", "condensed")
            or "gotcha" in c["tags"]
            or c["type"] == "fact"   # conservative until "live values" signal exists
        )
        if protected:
            c["condense"] = 0.0
        else:
            try:
                d = datetime.date.fromisoformat(str(c["date"]))
            except ValueError:
                d = today
            weeks = max(0.0, (today - d).days / 7.0)
            absorbed = any(
                cid in cards[p]["links"]["derived_from"]
                for p in cards if cards[p]["level"] == "derived" and cards[p]["status"] == "active"
            )
            c["condense"] = weeks * (1.0 if absorbed else F_UNABSORBED)
    return cards


def card_line(c):
    b = f" · belief {c['belief']:+.2f} ({c['evi'][0]} pro, {c['evi'][1]} contra)" if c["belief"] is not None else ""
    ev = f" · {c.get('evidence')}" if c.get("evidence") else ""
    er = f" · ✎errata:{len(c['errata'])}" if c.get("errata") else ""
    return f"- **{c['id']}** [{c['type']}/{c['status']}]{ev}{b}{er} — {c['title']}"


def write_views(cards, reg, alias):
    os.makedirs(os.path.join(VIEWS, "topics"), exist_ok=True)
    os.makedirs(os.path.join(VIEWS, "by-type"), exist_ok=True)
    unregistered = {}
    tagmap = {}
    for c in cards.values():
        for t in c["tags"]:
            t2 = alias.get(t, t)
            if t2 not in reg:
                unregistered.setdefault(t2, 0)
                unregistered[t2] += 1
            tagmap.setdefault(t2, []).append(c)

    # topic views
    for old in glob.glob(os.path.join(VIEWS, "topics", "*.md")):
        os.remove(old)
    for tag, group in sorted(tagmap.items()):
        L = ["# topic: " + tag, ""]
        pats = sorted((c for c in group if c["level"] == "derived" and c["status"] == "active"),
                      key=lambda c: -(c["belief"] or 0))
        chal = [c for c in group if c["status"] == "challenged"]
        refu = [c for c in group if c["status"] == "refuted"]
        gotc = [c for c in group if "gotcha" in c["tags"] and c["status"] == "active"]
        obs = [c for c in group if c["level"] in ("observation", "assumption")
               and c["status"] == "active" and "gotcha" not in c["tags"]]
        for name, items in (("Patterns", pats), ("Facts & observations", obs),
                            ("Gotchas", gotc), ("Challenged", chal),
                            ("⛔ Refuted & dead ends", refu)):
            if items:
                L += [f"## {name}", *[card_line(c) for c in items], ""]
        open(os.path.join(VIEWS, "topics", f"{tag}.md"), "w", encoding="utf-8").write("\n".join(L))

    # by-type
    for old in glob.glob(os.path.join(VIEWS, "by-type", "*.md")):
        os.remove(old)
    bytype = {}
    for c in cards.values():
        bytype.setdefault(c["type"], []).append(c)
    for t, items in bytype.items():
        fn = re.sub(r"[^a-z0-9]+", "-", t.lower())
        body = "\n".join(card_line(c) for c in sorted(items, key=lambda c: c["id"]))
        open(os.path.join(VIEWS, "by-type", f"{fn}.md"), "w", encoding="utf-8").write(
            f"# type: {t}\n\n{body}\n")

    # recent
    cutoff = (datetime.date.today() - datetime.timedelta(days=RECENT_DAYS)).isoformat()
    rec = [c for c in cards.values() if str(c["date"]) >= cutoff]
    open(os.path.join(VIEWS, "recent.md"), "w", encoding="utf-8").write(
        "# recent (last %d days)\n\n%s\n" % (
            RECENT_DAYS, "\n".join(card_line(c) for c in sorted(rec, key=lambda c: str(c["date"]), reverse=True))))

    # tags registry rewrite (keep desc/aliases, refresh counts, list unregistered)
    L = ["# Tag registry", ""]
    if unregistered:
        L += ["## UNREGISTERED (add at review!)", *[f"- {t} ({n} cards)" for t, n in sorted(unregistered.items())], ""]
    L += ["| tag | group | description | aliases | cards |", "|---|---|---|---|---|"]
    for tag in sorted(reg):
        r = reg[tag]
        n = len(tagmap.get(tag, []))
        L.append(f"| {tag} | {r['group']} | {r['desc']} | {', '.join(r['aliases']) or '—'} | {n} |")
    open(os.path.join(VIEWS, "tags.md"), "w", encoding="utf-8").write("\n".join(L) + "\n")

    # condense candidates (weekly review input)
    cand = sorted((c for c in cards.values() if c["condense"] > 0), key=lambda c: -c["condense"])
    open(os.path.join(VIEWS, "condense-candidates.md"), "w", encoding="utf-8").write(
        "# condense candidates (ranked)\n\n" +
        "\n".join(f"- {c['id']} score {c['condense']:.1f} — {c['title']}" for c in cand[:50]) + "\n")

    # reverse correction index: "who corrects ME". The hook builds the same map
    # in memory from the cards it has already loaded (ks.reverse_correctors), so
    # this file is not what it reads — it is the human-readable half, and the
    # place a review can see at a glance which corrections are wired up. Written
    # after a correction with no edge left the cards it corrected being recited
    # as current for four days.
    rev = {}
    for c in cards.values():
        for tgt in c["links"]["contradicts"]:
            rev.setdefault(tgt, []).append(c)
        for tgt in c["links"]["refuted_by"]:      # the corrected card's own edge
            if tgt in cards:
                rev.setdefault(c["id"], []).append(cards[tgt])
        ss = c["links"]["supersedes"]        # scalar per spec, list in the wild
        for tgt in (ss if isinstance(ss, list) else [ss] if ss else []):
            rev.setdefault(tgt, []).append(c)
    L = ["# reverse correction index (who corrects whom — generated)", ""]
    for tgt in sorted(rev):
        name = cards[tgt]["title"][:80] if tgt in cards else "(no such card)"
        L.append(f"- **{tgt}** {name}")
        for cor in sorted({c["id"]: c for c in rev[tgt]}.values(), key=lambda c: c["id"]):
            L.append(f"    ⚠ corrected by {cor['id']} [{cor['status']}] — {cor['title'][:80]}")
    open(os.path.join(VIEWS, "correctors.md"), "w", encoding="utf-8").write("\n".join(L) + "\n")

    # errata index: cards whose text was corrected in place (khms_erratum.py). The retracted
    # wording is inside each card's ERRATUM block and in memory/tombstones/; this is the
    # at-a-glance list a review reads.
    L = ["# errata (claims corrected in place — generated; ledger: memory/tombstones/LEDGER.md)", ""]
    for c in sorted((c for c in cards.values() if c.get("errata")), key=lambda c: c["id"]):
        L.append(f"- **{c['id']}** [{c['status']}] {c['title'][:80]}")
        for e in c["errata"]:
            L.append(f"    ✎ {e.get('ts')} {e.get('date')} by {e.get('by')}")
    open(os.path.join(VIEWS, "errata.md"), "w", encoding="utf-8").write("\n".join(L) + "\n")

    # scope tree usage (open ontology — this documents the branches actually in use)
    scopes = {}
    for c in cards.values():
        scopes.setdefault(str(c["scope"]), 0)
        scopes[str(c["scope"])] += 1
    open(os.path.join(VIEWS, "scopes.md"), "w", encoding="utf-8").write(
        "# scope tree (branches in use)\n\n" +
        "\n".join(f"- {s} ({n} cards)" for s, n in sorted(scopes.items())) + "\n")
    return tagmap, unregistered


def write_memory_md(cards, reg, tagmap):
    core = [c for c in cards.values() if "core" in c["tags"] and c["status"] == "active"]
    usage = [c for c in core if "khms" in c["tags"] and c["type"] == "policy"]
    usage_body = usage[-1]["body"] if usage else "(memory-usage core card not written yet)"
    pats = sorted((c for c in cards.values() if c["belief"] is not None and c["status"] == "active"),
                  key=lambda c: -c["belief"])[:TOP_PATTERNS]
    plans = sorted(glob.glob(os.path.join(BRAIN, "plans", "*.md")), key=os.path.getmtime, reverse=True)[:2]
    journals = sorted(glob.glob(os.path.join(BRAIN, "journal", "*.md")))[-1:]
    groups = {}
    for tag, items in tagmap.items():
        g = reg.get(tag, {}).get("group", "other")
        groups.setdefault(g, []).append(f"{tag} {len(items)}")
    L = ["# MEMORY — KHMS index (generated by tools/build_views.py — do not edit)", ""]
    L += ["## 1 How this KB works", usage_body, ""]
    L += ["## 2 Core"]
    L += [f"- [{c['id']}](know/{c['id']}.md) — {c['title']}" for c in sorted(core, key=lambda c: c["id"])] or ["- (none yet)"]
    L += ["", "## 3 Current focus"]
    L += [f"- plans/{os.path.basename(p)}" for p in plans]
    L += [f"- journal/{os.path.basename(j)}" for j in journals]
    L += ["", "## 4 Top patterns"]
    L += [f"- {c['id']} belief {c['belief']:+.2f} — {c['title']}" for c in pats] or ["- (none yet)"]
    L += ["", "## 5 Topics (full registry: views/tags.md)"]
    L += [f"- {g}: " + ", ".join(sorted(items)) for g, items in sorted(groups.items())]
    text = "\n".join(L) + "\n"
    target = os.path.join(MEM, "MEMORY.md")
    try:
        open(target, "w", encoding="utf-8").write(text)
    except PermissionError:
        # NC sync can restore stale read-only perms — reclaim and retry
        try:
            os.chmod(target, 0o644)
            open(target, "w", encoding="utf-8").write(text)
            print("note: MEMORY.md was read-only (sync artifact) — perms fixed, written")
        except OSError:
            preview = os.path.join(VIEWS, "MEMORY-preview.md")
            open(preview, "w", encoding="utf-8").write(text)
            print(f"note: MEMORY.md not writable — wrote {preview}")
    return len(text.splitlines())


def refresh_dense_index():
    """Half 1 of EMBSTALE (#168): the write path leaves the base SEARCHABLE.

    WHY HERE. Every card-writing path in the brain already ends in
    `build_views.py` — approve_inbox.py tells you to run it, khms_erratum.py
    tells you to run it, the review ping's four-step recipe ends with it, and a
    card written by hand is not considered written until it is green (core rule
    #8). `approve_inbox.py` was the other candidate and is the WRONG one: it
    covers the inbox route only, and both of the measured misses were cards
    written by hand, which never touch it. One choke point,
    on the path that is already mandatory.

    It is INCREMENTAL by construction (embed_index keeps a content hash per
    card) and it encodes through the warm daemon, so a one-card write costs a
    fraction of a second, not the ~9 s a cold model load costs; the only thing
    that ever loads the model here is a delta the daemon refuses, and that goes
    to the embed venv in a subprocess because the host python has no torch.

    A failure is never swallowed: it is printed, and the read path
    (khms_recall_hybrid.py) then says KHMS-INDEX-STALE over every answer until
    it is fixed. build_views' own exit code stays about the CARDS.
    """
    if KNOW != REAL_KNOW:                       # a fixture base — not the index
        return
    try:
        import embed_index as ei
    except Exception as e:                                        # noqa: BLE001
        print(f"WARNING: dense index NOT refreshed (import embed_index: {e})",
              file=sys.stderr)
        return
    if not ei.dense_configured(DENSE_MODEL):
        print("dense index: not configured (optional; KHMS_EMBED_PY unset and no embedding daemon) "
              "-- recall answers from the lexical channel")
        return
    res = ei.refresh(DENSE_MODEL, via="daemon")
    if res["status"] == "failed":
        # the daemon is down or too far behind: pay the model load, in the venv
        # that has torch, rather than leave the base unsearchable
        import subprocess
        print(f"note: dense index via daemon failed ({res['err']}) — "
              f"falling back to a local model load")
        try:
            p = subprocess.run([ei.VENV_PY, os.path.join(P.CODE, "embed_index.py"),
                                "--model", DENSE_MODEL, "--via", "local"],
                               capture_output=True, text=True, timeout=1800)
            ok = p.returncode == 0
            if ok:
                res["total"] = (ei.load_manifest(DENSE_MODEL,
                                                 ei.MODELS[DENSE_MODEL]["hf"])
                                or {}).get("count", res["total"])
            res = {"status": "ok" if ok else "failed",
                   "missing": res["missing"], "dropped": res["dropped"],
                   "seconds": 0.0, "how": "venv", "total": res["total"],
                   "err": None if ok else (p.stderr or p.stdout)[-400:]}
        except Exception as e:                                    # noqa: BLE001
            res = dict(res, err=f"venv fallback: {type(e).__name__}: {e}")
    if res["status"] == "fresh":
        print(f"dense index: up to date ({res['total']} vectors)")
    elif res["status"] == "ok":
        print(f"dense index: {res['missing']} card(s) embedded, "
              f"{res['dropped']} dropped, in {res['seconds']:.1f}s via "
              f"{res['how']} ({res['total']} vectors)")
    else:
        print(f"WARNING: dense index NOT refreshed — {res['err']}\n"
              f"         recall.sh will print KHMS-INDEX-STALE over every answer "
              f"until it is. Fix: {ei.VENV_PY} "
              f"{P.CODE}/embed_index.py --model {DENSE_MODEL}",
              file=sys.stderr)


def main():
    P.require_writable("rebuild the views and MEMORY.md")
    cards = load_cards()
    reg, alias = load_registry()
    cards = compute(cards)
    tagmap, unreg = write_views(cards, reg, alias)
    n_lines = write_memory_md(cards, reg, tagmap)
    n_der = sum(1 for c in cards.values() if c["level"] == "derived")
    print(f"OK: {len(cards)} cards ({n_der} derived), {len(tagmap)} tags "
          f"({len(unreg)} unregistered), MEMORY.md {n_lines} lines")
    if n_lines > 80:
        print("WARNING: MEMORY.md exceeds 80 lines", file=sys.stderr)
    refresh_dense_index()


if __name__ == "__main__":
    main()
