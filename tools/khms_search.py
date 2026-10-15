#!/usr/bin/env python3
"""KHMS search core — ONE scoring implementation, two consumers:
  - recall.sh (manual CLI, contract unchanged: free text in, ranked cards out)
  - khms_hook.py (harness hook layer: injects cards into the main session)

Usage: python3 tools/khms_search.py <query ...>   (normally through tools/recall.sh; imported by khms_hook)

Extracted 2026-08-04 from recall.sh's embedded python so the hook layer and the
CLI cannot drift apart. Scoring is IDENTICAL to the original: idf per token,
first-line x3, tag x1.5, phrase +10, fog x0.6 (no card is fog since #219). Additions are structural only:
frontmatter links/level are parsed, and a reverse index answers "which card
corrects this one" (supersedes/contradicts/refuted_by) so a consumer never
serves a refuted card without its refuter.
"""
import khms_files as glob
import math
import os
import re
import sys
import unicodedata

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import khms_paths as P  # noqa: E402

BRAIN = P.soft("ROOT")          # None when no base resolves; load_cards then raises the clean error
MEM = P.soft("MEM")
TOPN = 8

_WORD = re.compile(r"[a-z0-9_/.\-]+")
_LINK_KEYS = ("derived_from", "supports", "contradicts", "refuted_by")


def norm(s):
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    for a, b in (("’", "'"), ("‘", "'"), ("‚", "'"), ("“", '"'), ("”", '"'),
                 ("„", '"'), ("—", "-"), ("–", "-")):
        s = s.replace(a, b)
    return s.lower()


def tokens(s):
    return [t for t in _WORD.findall(norm(s))
            if len(t) >= 3 or re.fullmatch(r"0x[0-9a-f]+|\d+", t)]


def _parse_links(front):
    links = {k: [] for k in _LINK_KEYS}
    links["supersedes"] = None
    m = re.search(r"^links:\n((?:[ \t]+.*\n?)*)", front, re.M)
    if not m:
        return links
    block = m.group(1)
    for k in _LINK_KEYS:
        km = re.search(rf"^\s+{k}:\s*\[([^\]]*)\]", block, re.M)
        if km:
            links[k] = [x.strip() for x in km.group(1).split(",") if x.strip()]
    # `supersedes` is written BOTH ways in this base: scalar (`supersedes: K-x`,
    # what approve_inbox emits) and flow sequence (`supersedes: [K-x]`, what
    # convert_inbox2's LINK_KEYS emits, and what 49 cards carry). The scalar
    # branch below used to be the only one, so the bracket form parsed to the
    # literal string "[K-x]": a target id that matches no card. The forward line
    # then rendered `supersedes [K-x]: (not in the base)` and — the part that costs
    # something — `reverse_correctors`/`link_maps` keyed the reverse edge on that
    # same bogus string, so the SUPERSEDED card never learned what replaced it.
    # 16 live edges were affected (measured 2026-09-19). Bracket form first, the
    # scalar form unchanged; consumers already accept either shape.
    sm = re.search(r"^\s+supersedes:\s*\[([^\]]*)\]", block, re.M)
    if sm:
        ids = [x.strip() for x in sm.group(1).split(",") if x.strip()]
        if ids:
            links["supersedes"] = ids
        # `supersedes: []` keeps the None default it has always had — the
        # bracket branch must change the EMPTY forms' behaviour in no way.
        return links
    sm = re.search(r"^\s+supersedes:\s*(\S+)", block, re.M)
    if sm and sm.group(1) not in ("null", "~", "[]"):
        links["supersedes"] = sm.group(1)
    return links


def load_cards():
    cards = []
    mem = MEM or P.MEM
    # memory/archive/know/ is NOT read (#219, owner 2026-10-15): a card moved there — superseded, or a
    # state-only card thrown out by khms_archive.py — has left the base, and recall, the hook and the
    # dense index (all of which read this function) must not serve it. It used to be read as "fog" at
    # x0.6; the key stays False so the consumers that test it keep working.
    for path in sorted(glob.glob(f"{mem}/know/K-*.md")):
        fog = False
        try:
            text = open(path, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
        if not m:
            continue
        front, body = m.groups()

        def fm(key, default=""):
            mm = re.search(rf"^{key}:\s*(.+)$", front, re.M)
            return mm.group(1).strip() if mm else default

        tg = re.search(r"^tags:\s*\[([^\]]*)\]", front, re.M)
        body = body.strip()
        cards.append({
            "id": fm("id", path),
            "status": fm("status", "?"),
            "level": fm("level", "observation"),
            "type": fm("type", ""),
            "tags": [t.strip() for t in (tg.group(1) if tg else "").split(",") if t.strip()],
            "ntags": norm(tg.group(1)) if tg else "",
            "links": _parse_links(front),
            # the reverse edge some cards carry in their own frontmatter (`  superseded_by: K-x`)
            "superseded_by": re.findall(r"K-\d{5}", (re.search(r"^\s+superseded_by:\s*(.+)$", front, re.M)
                                                     or [None, ""])[1]),
            # errata: [{ts: TS-nnnnn, …}] entries (khms_erratum.py) — a count, so the
            # renderers can flag a card whose text was corrected in place
            "errata": len(re.findall(r"^\s+-\s*\{?\s*ts:\s*TS-\d{5}", front, re.M)),
            "body": body,
            "nbody": norm(body),
            "first": body.splitlines()[0] if body else "",
            "fog": fog,
            "path": path,
        })
    return cards


def reverse_correctors(cards):
    """kid -> [cards that supersede/contradict/… it]. The old card itself also
    names refuters in links.refuted_by; both directions are merged by callers."""
    rev = {}
    for c in cards:
        links = c.get("links") or {}
        # .get, not [], because this map is now consulted for EVERY served card
        # (2026-08-28), including on the live hook path: one card with a link
        # block this parser did not fill must not cost the whole injection.
        for tgt in (links.get("contradicts") or []):
            rev.setdefault(tgt, []).append(c)
        sup = links.get("supersedes")
        for tgt in (sup if isinstance(sup, list) else [sup] if sup else []):
            rev.setdefault(tgt, []).append(c)
    return rev


def load_glossary(path=None):
    """Lines: '<stem-without-diacritics><TAB><english words>'. '#' comments.
    The glossary is a private resource (KHMS_GLOSSARY); unset or absent means no glossary."""
    gl = {}
    path = path or P.GLOSSARY
    if not path:
        return gl
    try:
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if not line or line.startswith("#") or "\t" not in line:
                continue
            cz, en = line.split("\t", 1)
            gl[norm(cz.strip())] = en.strip()
    except OSError:
        pass
    return gl


def expand_query(query_raw, glossary):
    """Append English equivalents for Czech stems found in the query.
    Stem match: a glossary key matching the start of a query token (>=3 chars),
    so 'otocky'/'otocka' both hit key 'otock'. Returns (expanded, added_terms)."""
    if not glossary:
        return query_raw, []
    added = []
    for t in set(tokens(query_raw)):
        for key, en in glossary.items():
            if t.startswith(key) and en not in added:
                added.append(en)
    return (query_raw + " " + " ".join(added)).strip() if added else query_raw, added


def search(query_raw, cards=None, exclude_meta=False, topn=TOPN):
    """Returns (results, cards). results: list of (score, matched_tokens, card)."""
    if cards is None:
        cards = load_cards()
    q_tokens = tokens(query_raw)
    q_phrase = re.sub(r"\s+", " ", norm(query_raw)).strip()
    if not q_tokens:
        return [], cards

    meta_ok = bool(re.search(r"khms|memory|karta|card|baze|recall", norm(query_raw)))
    pool = cards
    if exclude_meta and not meta_ok:
        pool = [c for c in cards if not ({"khms", "core"} & set(c["tags"]))]

    df = {t: sum(1 for c in cards if t in c["nbody"]) for t in set(q_tokens)}
    N = len(cards)
    idf = {t: math.log((N + 1) / (df[t] + 1)) + 0.1 for t in df}

    results = []
    for c in pool:
        nfirst = norm(c["first"])
        score, matched = 0.0, []
        for t in set(q_tokens):
            if t in c["nbody"]:
                w = idf[t]
                if t in nfirst:
                    w *= 3.0
                if t in c["ntags"]:
                    w *= 1.5
                score += w
                matched.append(t)
        if q_phrase and len(q_phrase) > 12 and q_phrase in c["nbody"]:
            score += 10.0
        if c["fog"]:
            score *= 0.6
        if score > 0:
            results.append((score, matched, c))
    results.sort(key=lambda r: -r[0])
    return results[:topn], cards


def recall_log_path():
    """The retrieval record. KHMS_RECALL_LOG redirects it, so a test (and the
    retrieval-claim gate's own tests) can never write into the live log — and so the
    gate that READS this file in khms_hook.py has exactly one path to read."""
    return P.RECALL_LOG


def recall_jsonl_path():
    """The STRUCTURED retrieval record (review finding P1-1): one JSON line per
    CLI recall with its RESULT (hit count, card ids) and the session it ran in. The
    proposal gate binds a recall to a reply through this file plus the hook's own
    PreToolUse record of who issued the command — never by time alone. Follows
    KHMS_RECALL_LOG when that is redirected, so a test never writes the live file."""
    r = P.resolved()
    if r.env.get("KHMS_RECALL_JSONL") or r.raw.get("KHMS_RECALL_JSONL"):
        return P.RECALL_JSONL
    if r.env.get("KHMS_RECALL_LOG") or r.raw.get("KHMS_RECALL_LOG"):
        return P.RECALL_LOG + ".jsonl"
    return P.RECALL_JSONL


def log_query(query_raw, results, src="cli"):
    import datetime
    import json
    import time
    top = f"{results[0][2]['id']}:{results[0][0]:.1f}" if results else "-"
    if not P.may_write(recall_log_path()):
        print("recall: read-only deployment and the recall log points into the shared base -- "
              "not logged (set KHMS_STATE outside KHMS_ROOT)", file=sys.stderr)
        return
    os.makedirs(os.path.dirname(recall_log_path()), exist_ok=True)
    with open(recall_log_path(), "a", encoding="utf-8") as lf:
        lf.write(f"{datetime.datetime.now().astimezone().isoformat(timespec='seconds')}"
                 f" | {query_raw} | nhits={len(results)} | top={top} | src={src}\n")
    if not str(src).startswith("cli"):
        return
    try:
        rec = {"ts": round(time.time(), 3),
               "sid": os.environ.get("CLAUDE_CODE_SESSION_ID", ""),
               "query": query_raw, "nhits": len(results),
               "top": [r[2].get("id") for r in results[:10] if isinstance(r[2], dict)],
               "src": src}
        if not P.may_write(recall_jsonl_path()):
            return
        with open(recall_jsonl_path(), "a", encoding="utf-8") as jf:
            jf.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:                                              # noqa: BLE001
        pass          # the plain log line above is written; a recall never fails on this


def main_cli(argv):
    query_raw = " ".join(argv)
    expanded, added = expand_query(query_raw, load_glossary())
    results, cards = search(expanded)           # CLI keeps FULL pool (no meta filter)
    log_query(query_raw, results, src="cli")
    if added:
        import khms_messages as M
        print(M.msg("GLOSSARY_LABEL", terms=" ".join(added)[:120]))
    if not tokens(query_raw):
        print("recall: query has no searchable tokens")
        return 2
    if not results:
        print(f"recall: nothing on record for: {query_raw}")
        return 1
    df = {t: sum(1 for c in cards if t in c["nbody"])
          for r in results for t in r[1]}
    for score, matched, c in results:
        flags = []
        if c["status"] in ("refuted", "challenged"):
            flags.append("⛔" + c["status"].upper())
        elif c["status"] != "active":
            flags.append(c["status"])
        if "gotcha" in c["tags"]:
            flags.append("⚠gotcha")
        if c.get("errata"):
            flags.append("✎ERRATUM")
        if c["fog"]:
            flags.append("fog")
        flag = (" [" + ",".join(flags) + "]") if flags else ""
        print(f"{c['id']}{flag} ({score:.1f}): {c['first'][:110]}")
        rare = min(sorted(matched), key=lambda t: df.get(t, 0)) if matched else None
        if rare:
            for line in c["body"].splitlines()[1:]:
                if rare in norm(line):
                    print(f"    ↳ {line.strip()[:150]}")
                    break
    return 0


if __name__ == "__main__":
    sys.exit(main_cli(sys.argv[1:]))
