#!/usr/bin/env python3
"""KHMS recall CLI — hybrid (lexical + dense) by DEFAULT since 2026-09-04.

Usage: run through tools/recall.sh [--lexical] <query ...>

HISTORY. 2026-08-09 (WP-E) added the dense channel as strictly OPT-IN
(`recall.sh --dense` / KHMS_DENSE=1), so the default path stayed byte-identical
lexical. 2026-09-04 measured what that cost: card K-00542 (Foxglove loads
meshes Y-up, ROS is Z-up) existed since 2026-07-29 and did not fire in three
incidents (07-27, 08-09, 09-04); the owner's Czech sentence returns it at
lexical rank 7 / not at all, and at dense-fused rank 1. The opt-in was the
defect (K-02517, K-01867: an English KB queried in Czech owes the system a
working cross-lingual channel, and that channel was switched off by default).

WHAT CHANGED (2026-09-04):
  * recall.sh routes EVERY invocation here. The dense channel is used unless
    it is opted out of (`--lexical` / KHMS_DENSE=0) or the daemon does not
    answer inside KHMS_DENSE_TIMEOUT_MS (default 150 ms).
  * the first printed line always names the channel that actually answered —
    `channel=hybrid`, `channel=lexical(fallback: daemon down)`,
    `channel=lexical(opt-out ...)` — because a silent fail-back to the channel
    that caused the miss is exactly the failure this change is about.
  * `.recall.log` src field carries the channel: `src=cli:hybrid:<model>`,
    `src=cli:lexical:fallback`, `src=cli:lexical:optout`. It still starts with
    `src=cli`, which is what khms_hook.py's retrieval-claim gate matches on.
  * `--dense` is accepted and ignored (back-compat for scripts and habits).
  * each of the top hits prints its depth-1 LINKS (`↪ superseded_by K-x: …`),
    so a card that has been replaced or refuted says so without being opened.

LIBRARY CONTRACT — UNCHANGED. `dense_query`, `rrf` and `hybrid_rank` keep their
signatures and behaviour: khms_hook.py (live, every tool call) imports
`dense_query`, hook_replay.py and nearest_cards.py import this module, and
tools/eval/run_eval.py imports `rrf`. Only the CLI front end below changed.

FUSION: reciprocal rank fusion, score = sum_channels 1/(k + rank), k=60.
Rank-based, so the two channels' incomparable score scales (lexical idf sums of
10-60 vs. cosine 0-1) never need calibrating. Both candidate lists are capped
(top CAND each) — that cap is what stops the dense channel from flooding an
exact-identifier query where lexical is already right, which K-00601 warns is
the specific risk of adding embeddings ("an embedding would blur the exact token
that is the signal").

FAIL-SAFE: the dense channel is best-effort. Socket missing, daemon wedged, or
slower than KHMS_DENSE_TIMEOUT_MS -> pure lexical output, said out loud, no
traceback on stdout.
"""
import json
import math
import os
import re
import socket
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import khms_search as ks  # noqa: E402
import khms_paths as P  # noqa: E402
import khms_messages as M  # noqa: E402

BRAIN = ks.BRAIN
RRF_K = float(os.environ.get("KHMS_RRF_K", "60"))
CAND = int(os.environ.get("KHMS_RRF_CAND", "50"))
TIMEOUT_MS = float(os.environ.get("KHMS_DENSE_TIMEOUT_MS", "150"))
MODEL = os.environ.get("KHMS_DENSE_MODEL", "bge")  # bge-m3 won the eval: xling r@1 65% vs 20%
SOCKET = P.soft("KHMS_DENSE_SOCKET") or os.path.join(P.soft("SHARED_STATE", ""), f"embed-{MODEL}.sock")

# I5 (owner question 2026-09-04: "are linked cards loaded too, to some depth?"):
# depth-1 links of the top hits, titles only. Depth 1 and top-N only — the point
# is to see that a hit has been superseded/refuted, not to walk the graph.
LINK_TOPN = int(os.environ.get("KHMS_LINK_TOPN", "3"))
LINK_MAX = int(os.environ.get("KHMS_LINK_MAX", "6"))     # lines per card

# Half 2 of EMBSTALE (#168). How many cards this end will embed in-line before
# it stops trying and just shouts. A recall is a HABIT; it may spend a few
# hundred ms on the warm daemon, never minutes on a model load.
FRESH_MAX = int(os.environ.get("KHMS_RECALL_REFRESH_MAX", "64"))


def dense_query(text, topn=CAND, timeout_ms=TIMEOUT_MS, sock_path=SOCKET):
    """Returns (hits, err, ms). hits = [(card_id, cosine)] or [] on any failure."""
    t0 = time.time()
    deadline = t0 + timeout_ms / 1000.0
    s = None
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(max(0.001, deadline - time.time()))
        s.connect(sock_path)
        s.sendall((json.dumps({"q": text, "topn": topn}) + "\n").encode("utf-8"))
        buf = b""
        while b"\n" not in buf:
            s.settimeout(max(0.001, deadline - time.time()))
            chunk = s.recv(65536)
            if not chunk:
                return [], "eof", (time.time() - t0) * 1000
            buf += chunk
        resp = json.loads(buf.split(b"\n", 1)[0].decode("utf-8"))
        if not resp.get("ok"):
            return [], resp.get("err", "not-ok"), (time.time() - t0) * 1000
        return [(h[0], float(h[1])) for h in resp["hits"]], None, (time.time() - t0) * 1000
    except (OSError, socket.timeout, ValueError, KeyError) as e:
        return [], f"{type(e).__name__}", (time.time() - t0) * 1000
    finally:
        if s is not None:
            try:
                s.close()
            except OSError:
                pass


def rrf(lex_ids, dense_ids, k=RRF_K):
    """lex_ids/dense_ids: ranked card-id lists. Returns {id: fused_score}."""
    fused = {}
    for rank, cid in enumerate(lex_ids, 1):
        fused[cid] = fused.get(cid, 0.0) + 1.0 / (k + rank)
    for rank, cid in enumerate(dense_ids, 1):
        fused[cid] = fused.get(cid, 0.0) + 1.0 / (k + rank)
    return fused


def hybrid_rank(query_raw, cards=None, topn=8, exclude_meta=False,
                timeout_ms=TIMEOUT_MS, sock_path=SOCKET, rrf_k=RRF_K, cand=CAND):
    """Returns (ranked, meta). ranked = [(fused_score, matched_tokens, card)],
    same shape khms_search.search returns, so printers/consumers are unchanged."""
    if cards is None:
        cards = ks.load_cards()
    expanded, added = ks.expand_query(query_raw, ks.load_glossary())
    lex, _ = ks.search(expanded, cards=cards, exclude_meta=exclude_meta, topn=cand)
    dense, err, ms = dense_query(query_raw, topn=cand, timeout_ms=timeout_ms,
                                 sock_path=sock_path)
    meta = {"glossary": added, "dense_ms": ms, "dense_err": err,
            "dense_n": len(dense), "mode": "lexical" if err else "hybrid"}
    if err or not dense:
        return lex[:topn], meta

    by_id = {c["id"]: c for c in cards}
    pool = {c["id"]: (s, m) for s, m, c in lex}
    fused = rrf([c["id"] for _s, _m, c in lex], [d[0] for d in dense], k=rrf_k)
    out = []
    for cid, fs in sorted(fused.items(), key=lambda kv: -kv[1]):
        c = by_id.get(cid)
        if c is None:
            continue
        if exclude_meta and cid not in pool and ({"khms", "core"} & set(c["tags"])):
            continue
        out.append((fs, pool.get(cid, (0.0, []))[1], c))
        if len(out) >= topn:
            break
    return out, meta


def lexical_rank(query_raw, cards=None, topn=8, exclude_meta=False):
    """The opt-out channel, with the same return shape as hybrid_rank."""
    if cards is None:
        cards = ks.load_cards()
    expanded, added = ks.expand_query(query_raw, ks.load_glossary())
    lex, _ = ks.search(expanded, cards=cards, exclude_meta=exclude_meta, topn=topn)
    meta = {"glossary": added, "dense_ms": 0.0, "dense_err": "opt-out",
            "dense_n": 0, "mode": "lexical"}
    return lex, meta


# ------------------------------------------------------------- depth-1 links
def link_maps(cards):
    """Reverse edges the frontmatter does not carry: who supersedes / who
    contradicts this card. One pass over the loaded cards, no file I/O."""
    superseded_by, contradicted_by = {}, {}
    for c in cards:
        links = c.get("links") or {}
        sup = links.get("supersedes")
        for tgt in (sup if isinstance(sup, list) else [sup] if sup else []):
            superseded_by.setdefault(tgt, []).append(c["id"])
        for tgt in (links.get("contradicts") or []):
            contradicted_by.setdefault(tgt, []).append(c["id"])
    return superseded_by, contradicted_by


def link_lines(c, by_id, superseded_by, contradicted_by, limit=LINK_MAX):
    """Depth-1 links of one card as printable lines, warnings first."""
    links = c.get("links") or {}
    sup = links.get("supersedes")
    kinds = [
        ("refuted_by", links.get("refuted_by") or []),
        ("superseded_by", superseded_by.get(c["id"], [])),
        ("contradicted_by", contradicted_by.get(c["id"], [])),
        ("supersedes", sup if isinstance(sup, list) else [sup] if sup else []),
        ("contradicts", links.get("contradicts") or []),
        ("derived_from", links.get("derived_from") or []),
        ("supports", links.get("supports") or []),
    ]
    out, seen = [], set()
    for kind, ids in kinds:
        for kid in ids:
            if not kid or kid == c["id"] or (kind, kid) in seen:
                continue
            seen.add((kind, kid))
            other = by_id.get(kid)
            title = other["first"][:100] if other else M.msg("NOT_IN_BASE")
            out.append(f"    ↪ {kind} {kid}: {title}")
            if len(out) >= limit:
                return out
    return out


# ------------------------------------------------------------ freshness gate
def freshness_lines(cards, model=MODEL, max_cards=FRESH_MAX):
    """-> lines to print BEFORE the answer, [] when the index is level.

    WHY (EMBSTALE #168, K-02856 + K-02868, three occurrences in two days): the
    lexical channel is a stateless scan and cannot go stale, but the dense half
    reads a FILE. When that file is behind — a crash, a half-finished refresh, a
    card written by hand — recall still answers, cleanly, confidently and
    without the newest cards in it. An answer that looks identical whether or
    not the thing it depends on is healthy is not an answer (K-00585), and
    "remember to rebuild the index" is not a measure (K-00500).

    So: compare the index against the cards on disk (one sha1 each over cards
    already loaded for the lexical channel, ~30 ms), and then REFRESH — through
    the warm daemon, capped — or say KHMS-INDEX-STALE where nobody can read past
    it. The marker is one unmistakable token on purpose: it can be grepped for
    in a transcript, and it appears in no card body.
    """
    try:
        import embed_index as ei
    except Exception as e:                                        # noqa: BLE001
        return [f"⚠⚠ KHMS-INDEX-STALE (unknown): cannot check whether the dense "
                f"index is current — {type(e).__name__}: {e}"]
    res = ei.refresh(model, cards=cards, via="daemon", max_cards=max_cards)
    if res["status"] == "fresh":
        return []
    if res.get("readonly"):
        return [f"(KHMS-INDEX-STALE: {res['missing']} card(s) missing / {res['dropped']} ghost(s) in "
                f"the shared dense index; a read-only deployment does not refresh it -- this answer "
                f"uses the lexical channel only)"]
    if res["status"] == "ok":
        return [f"(⚠ KHMS-INDEX-STALE: {res['missing']} card(s) missing / "
                f"{res['dropped']} ghost(s) in the dense index — refreshed in "
                f"{res['seconds']:.1f} s, {res['total']} vectors, the answer "
                f"below is complete)"]
    newest = ", ".join(sorted(res["ids"])[-3:]) if res["ids"] else "?"
    return [
        "⚠⚠ KHMS-INDEX-STALE — THIS ANSWER MAY BE INCOMPLETE ⚠⚠",
        f"   {res['missing']} card(s) on disk are NOT in the semantic (dense) "
        f"channel, {res['dropped']} deleted card(s) still are; index built "
        f"{res['built']}",
        f"   newest missing: {newest}",
        f"   auto-refresh failed: {res['err']}",
        f"   fix: {ei.VENV_PY} {P.CODE}/embed_index.py --model {model}",
        "   until then `recall.sh --lexical` is the half that cannot go stale.",
    ]


# ---------------------------------------------------------------- CLI output
def print_results(results, cards, channel, meta, show_links=True):
    fused = channel == "hybrid"
    by_id = {c["id"]: c for c in cards}
    superseded_by, contradicted_by = link_maps(cards) if show_links else ({}, {})
    df = {t: sum(1 for c in cards if t in c["nbody"])
          for r in results for t in r[1]}
    for i, (score, matched, c) in enumerate(results):
        flags = []
        if c["status"] in ("refuted", "challenged"):
            flags.append("⛔" + c["status"].upper())
        elif c["status"] != "active":
            flags.append(c["status"])
        if "gotcha" in c["tags"]:
            flags.append("⚠gotcha")
        if c.get("errata"):
            flags.append("✎ERRATUM")     # text corrected in place; see the card's ERRATUM block
        if c["fog"]:
            flags.append("fog")
        flag = (" [" + ",".join(flags) + "]") if flags else ""
        val = f"{score:.4f}" if fused else f"{score:.1f}"
        print(f"{c['id']}{flag} ({val}): {c['first'][:110]}")
        rare = min(sorted(matched), key=lambda t: df.get(t, 0)) if matched else None
        if rare:
            for line in c["body"].splitlines()[1:]:
                if rare in ks.norm(line):
                    print(f"    ↳ {line.strip()[:150]}")
                    break
        if show_links and i < LINK_TOPN:
            for ln in link_lines(c, by_id, superseded_by, contradicted_by):
                print(ln)


def parse_argv(argv, env=None):
    """(query_words, lexical_only). `--dense` is accepted and ignored."""
    env = os.environ if env is None else env
    lexical_only = env.get("KHMS_DENSE", "") == "0"
    words = []
    for a in argv:
        if a == "--dense":
            continue
        if a in ("--lexical", "--no-dense"):
            lexical_only = True
            continue
        words.append(a)
    return words, lexical_only


def main(argv):
    words, lexical_only = parse_argv(argv)
    query_raw = " ".join(words)
    if not query_raw.strip():
        print("usage: recall.sh [--lexical] <symptom/error/identifier ...>",
              file=sys.stderr)
        return 2
    cards = ks.load_cards()

    if lexical_only:
        results, meta = lexical_rank(query_raw, cards=cards)
        channel, src = "lexical(opt-out)", "cli:lexical:optout"
    else:
        if not (P.get("KHMS_EMBED_PY") or os.path.exists(SOCKET)):
            # Dense retrieval is an optional feature this deployment has not set up: no freshness
            # check (there is no index to be stale), one labelled lexical answer.
            results, meta = lexical_rank(query_raw, cards=cards)
            meta.update(dense_err="not-configured")
            channel, src = "lexical(dense not configured)", "cli:lexical:nodense"
            return _finish(query_raw, results, cards, channel, src, meta)
        # BEFORE the query, never after: a refresh here is what the query then
        # reads (the daemon re-checks the index's mtime on every request).
        stale = freshness_lines(cards)
        for line in stale:
            print(line)
        if P.read_only() and any("a read-only deployment does not refresh it" in s for s in stale):
            # A stale shared index the reader may not refresh: the dense half would answer without
            # the newest cards, so the answer is lexical and says so.
            results, meta = lexical_rank(query_raw, cards=cards)
            meta.update(dense_err="stale-index-read-only", dense_ms=0.0)
        else:
            results, meta = hybrid_rank(query_raw, cards=cards)
        if meta["dense_err"] == "stale-index-read-only":
            channel, src = "lexical(fallback: stale index, read-only)", "cli:lexical:stale-ro"
        elif meta["dense_err"]:
            channel, src = "lexical(fallback: daemon down)", "cli:lexical:fallback"
        else:
            channel, src = "hybrid", f"cli:hybrid:{MODEL}"
    return _finish(query_raw, results, cards, channel, src, meta)


def _finish(query_raw, results, cards, channel, src, meta):
    ks.log_query(query_raw, results, src=src)

    if meta["glossary"]:
        print(M.msg("GLOSSARY_LABEL", terms=" ".join(meta["glossary"])[:120]))
    if channel == "hybrid":
        print(f"(channel=hybrid | dense {MODEL} {meta['dense_ms']:.0f} ms "
              f"| RRF k={rrf_k_str()})")
    elif channel.startswith("lexical(fallback"):
        print(f"(channel={channel} "
              f"[{meta['dense_err']}, {meta['dense_ms']:.0f} ms])")
    elif channel == "lexical(dense not configured)":
        print("(channel=lexical(dense not configured: KHMS_EMBED_PY unset, no daemon socket))")
    else:
        print("(channel=lexical(opt-out: --lexical / KHMS_DENSE=0))")

    if not ks.tokens(query_raw):
        print("recall: query has no searchable tokens")
        return 2
    if not results:
        print(f"recall: nothing on record for: {query_raw}")
        return 1
    print_results(results, cards, "hybrid" if channel == "hybrid" else "lexical", meta)
    return 0


def rrf_k_str():
    return f"{RRF_K:g}"


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
