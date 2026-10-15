#!/usr/bin/env python3
"""permark.py — the nightly MARK stage as a per-mark evaluator: one packet, one call, one decision (#237).

Usage:
  permark.py --queue Q --out OUT [--deadline EPOCH] [--max-usd X] [--limit N] [--agent BIN] [--model sonnet]
             [--log LOG] [--jsonl J] [--served-file F] [--limit-flag F] [--timeout 180]
             [--packets DIR] [--dry] [--tonight-k 5] [--packet-chars 32000] [--whole-top 3]
             [--links-line 0|1] [--short-answers 0|1] [--covered-rule 0|1] [--effort E] [--format-check 0|1]

  Q     the ledger's queue file (`mark_ledger.py queue --out Q`): `- <id> | <date time> | <kind> | <where> | <text>`,
        journal MARKs (`M-`) and generated lines (`G-`, #237 step 2) on one time axis.
  OUT   the night's output file, in the MARK stage's existing format (candidates `### M<n>:` with
        `MARKS:` lines, then `## MARKS NOT CARDED`).  It is rewritten ATOMICALLY after every mark; a run
        with the same OUT skips every id already decided in it (resume by id).
  --deadline  no NEW mark is started at or after this epoch (the stage's budget).
  --max-usd   no NEW mark is started once what this run charged + the dearest mark so far would pass X
              dollars (the stage's dollar cap, #243; 0 = no cap).  See THE DOLLAR CAP below.
  --limit     at most N marks this run (a smoke or test run).
  --packets   also write every packet to DIR/<id>.txt (forensics, the hand check).
  --dry       build the packets only (implies --packets when given), no model call, OUT untouched.
  exit 0 done / deadline / dollar cap / limit; 75 the account's usage limit answered (the flag file is written);
  2 a setup error (no queue, no agent, no prompt).

WHY.  Until #237 the stage handed the model 60 bare queue lines; the model looked up each mark's journal
context and the base itself, with tools, step by step (44–103 turns per chunk), and the NEAREST cards
were computed only AFTER it, for a consolidate call that then merged its candidates.  Now a script
assembles everything for ONE mark in advance with 0 tokens — the PACKET — and the model answers once,
without tools, with exactly one decision; the driver applies it with 0 tokens.  Its candidate is final
for the night (no consolidate call: `nightly_batch.py split-run --ready`).

THE PACKET (build_packet):
  1. the mark's queue line;
  2. journal context — lines line-6 .. line+3 of its journal file, each clipped to 600 characters, its own
     line whole (journal marks only);
  3. NEAREST CARDS — active, non-fog cards from three sources, de-duplicated: every K- id the mark names
     (at most 3 shown whole), the lexical top 8 (khms_search, glossary-expanded) and the hybrid top 8
     (khms_recall_hybrid, when the dense daemon answers).  The named cards and the top N of each channel
     (KHMS_PERMARK_WHOLE_TOP / --whole-top, default 3) are shown WHOLE (4,000 characters at most), the rest
     by id, status, title and 3 body lines.  With KHMS_PERMARK_LINKS_LINE=1 / --links-line 1 (default 0)
     every shown card carries one more line right under its heading line: its type and the ids of its
     supersedes, superseded_by, contradicts, refuted_by and derived_from links (ids only, nothing followed) —
     card bodies carry no frontmatter, so without it the model sees neither type nor edges;
  4. TONIGHT'S CANDIDATES — the k (default 5, KHMS_PERMARK_TONIGHT_K / --tonight-k) of OUT's candidates
     nearest to the mark by the same retrieval as the cards: the lexical channel (glossary-expanded) fused by
     RRF with the dense channel (the warm daemon embeds each candidate once, op=embed) when it answers.  The
     nearest one is shown whole if it fits in 4,000 characters, the others by their heading, the first 3
     lines and their MARKS line.  So the section stays a few thousand characters on a night of 300
     candidates, and a second mark about the same finding still sees the first one's candidate (the
     in-night duplicate rule);
  5. SOURCE EXCERPT — `(sess <8 hex>)` in a journal mark or `src=digest sess <id>` in a generated line: the
     transcript's USER/ASSISTANT records of [t-20 min, t+2 min] (t = the mark's local time in UTC; TOOL
     records dropped, continuation lines kept, the last 6,000 characters); `src=gitlog <sha>`: the
     commit's `git show --stat`, 4,000 characters.

THE PACKET BUDGET (KHMS_PERMARK_PACKET_CHARS / --packet-chars, default 32,000 characters): when a packet is
longer, the lowest-ranked items go first — cards by their place in the NEAREST order and tonight's
candidates by theirs, a card before a candidate of the same rank — then the source excerpt is cut from its
start (the end nearest the mark stays), then the journal context lines farthest from the mark.  The mark's
queue line and its own journal line are never trimmed.  What was left out is named in the packet, and a
trimmed card is not among the NEAREST ids.  Every call logs `packet_chars=` (the served-by line and the
JSONL row), and the run ends with `permark: packet_chars n= min= median= max= budget= trimmed=`.

THE DECISIONS (parse_answer / apply): not-carded, new, belongs <label>, carded K-x, supersede K-x,
contradict K-x, erratum K-x.  An answer without a parseable block for THIS id is not written: the mark
stays OPEN for the next night and is counted `unparsed`.  `belongs` with a revision replaces the candidate
only when the revision keeps every id it had; otherwise the id is appended to the old one.  Every
candidate gets a `NEAREST:` line with the ids the packet showed, so verify_relations judges a
`RELATION: new` against exactly what the model saw.

SHORT ANSWERS (KHMS_PERMARK_SHORT_ANSWERS / --short-answers 0|1, default 1): the rules gain the section of
prompts/nightly-marks-short.md — a carded answer is its DECISION line, the verbatim quote line and one
`REASON:` line, a not-carded answer its DECISION line and its verdict line, nothing else.  The parser is the
same; a `REASON:` line goes to the JSONL row (`reason`), never into OUT, so the verdict line stays what the
morning's quote check greps.

COVERED RULE (KHMS_PERMARK_COVERED_RULE / --covered-rule 0|1, default 0): picks the variant of the
`<!-- rule:covered=N -->` blocks of prompts/nightly-marks.md and nightly-marks-short.md.  0 = a card with the
topic but not the mark's specific content means a candidate; 1 = a candidate only when the extra content
would change what a future agent does, otherwise the not-carded verdict `covered by K-x: "<verbatim
sentence of K-x>" — <why>`.  That verdict is a NOT CARDED reason: mark_ledger records it as a dismissal
(never coverage) only when its quote is in K-x, like `already carded` (K-05400); without one the mark
stays OPEN.  The row logs its K-x as `target` and the decisions line counts it as `covered=`.

EFFORT (KHMS_PERMARK_EFFORT / --effort low|medium|high|xhigh|max, default high): the `--effort` of the
per-mark call.

FORMAT CHECK (KHMS_PERMARK_FORMAT_CHECK / --format-check 0|1, default 1): a decision that carries a candidate
block (new, supersede, contradict, a belongs revision) is checked, with 0 tokens, by the functions the later
stages run on it (FormatCheck: card_header, verify_relations.judge, convert_inbox2's self-check with
build_views.card_problems, khms_lint.lint_batch) on the block exactly as apply() would write it.  A failure
costs ONE retry: the same packet, then the checks' messages and the rejected answer.  A retry that parses and
passes replaces the decision; otherwise the FIRST answer is applied as before and the later stage drops or
flags it with its reason.  Counted as `format-retry=` (retries made) and `format-failed=` (retries that did not
pass) in the decisions line; the JSONL row gets `format` (problems, retry passed|failed, retry problems) and
both calls' cost.  A check that cannot run (no tag registry, …) is logged once and the candidates go unchecked —
a check failing for its own reason must not cost calls.

LOGGED, NEVER GATED: `quotes-unproven` (an "already carded" or "covered by" quote mark_ledger.quote_in_card does not find
in the card) and `unseen-nearest` (a new candidate whose own nearest top 3 hold an id the packet lacked).
"""
# THE DOLLAR CAP (#243, owner 2026-10-15: "set the night's cap to $100"). Until then the stage had only its time
# budget, and the night of 2026-10-14 spent $63.10 in it (300 min). Every call is charged its CLI
# `total_cost_usd`; a call whose cost is UNKNOWN (a timeout, no JSON result) is charged as the dearest call seen
# so far, never as $0.  Before each NEW mark the RESERVE -- the dearest mark so far, all its calls (attempts and
# the format retry) -- is added to what is charged; if that would pass --max-usd, the stage stops exactly as at
# its deadline: what it decided is committed, the rest stays OPEN for the next night, exit 0, and ONE line
# `MARK stage stopped at the $ cap: $X of $Y, N mark(s) left OPEN` (the morning ping repeats it).  The triage's
# judge bound (khms_contradict.JudgeBudget) estimates each call's worst case from prices and token caps; here
# the measured cost of the run's own marks is the estimate (2026-10-14: median $0.0416, max $0.1291 per call),
# so no price table is read.  Like the judge's, it is an estimated bound, not a guaranteed hard cap: a mark
# dearer than every mark before it can pass the cap by its own excess, once -- it is the last mark of the run.
import argparse
import datetime as dt
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import khms_paths as P  # noqa: E402
import khms_search as ks  # noqa: E402
import mark_ledger as ML  # noqa: E402
import nearest_cards as NC  # noqa: E402

RUNNER = subprocess.run              # replaced by a stub in tests
USAGE_LIMIT_RE = re.compile(r"hit your [a-z0-9 -]*limit|usage limit reached", re.I)
EXIT_USAGE_LIMIT = 75

QUEUE_RE = re.compile(r"^- (?P<id>[MG]-[0-9a-f]{8}) \| (?P<date>\S+) (?P<time>\S+) \| (?P<kind>[^|]+?) \| "
                      r"(?P<loc>[^|]+?) \| (?P<text>.*)$")
CARD_ID_RE = re.compile(r"\bK-\d{5}\b")
SESS_JOURNAL_RE = re.compile(r"\(sess(?:ion)?\s+(?P<sid>[0-9a-f]{8})[^)]*\)")
SESS_GEN_RE = re.compile(r"src=digest\s+sess\s+(?P<sid>[0-9a-f]{8})[0-9a-f-]*(?:/agent-(?P<aid>[\w-]+))?")
GIT_GEN_RE = re.compile(r"src=gitlog\s+(?P<sha>[0-9a-f]{7,40})\b")
HEAD_RE = re.compile(r"^#{2,3} +([A-Z])(\d+)([a-z]?)\b(.*)$", re.M)
MARKS_LINE_RE = re.compile(r"^[ \t]*\*{0,2}MARKS:\*{0,2}[ \t]*(.*)$", re.M)
MARKS_LINE_NL_RE = re.compile(MARKS_LINE_RE.pattern + r"\n?", re.M)   # the line with its newline
NEAREST_LINE_RE = re.compile(r"^NEAREST:.*$\n?", re.M)
DECISION_RE = re.compile(r"^\s*\**DECISION:\**\s*(?P<kind>not-carded|new|belongs|carded|supersede|"
                         r"contradict|erratum)\b[ \t]*(?P<arg>[^\s`*]+)?", re.M | re.I)
FENCE_RE = re.compile(r"^\s*```[a-z]*\s*$", re.M)
DIGEST_REC_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2}T\d{2}:\d{2})\] ([A-Z-]+):")

CTX_BEFORE, CTX_AFTER, CTX_CLIP = 6, 3, 600
TOPK = 8
NAMED_MAX = 3
WHOLE_TOP = 3                 # the top N of each channel shown whole (KHMS_PERMARK_WHOLE_TOP, --whole-top)
LINKS_LINE = 0                # 1 = a type + link-ids line under every shown card (KHMS_PERMARK_LINKS_LINE)
SHORT_ANSWERS = 1             # 1 = three-line carded / not-carded answers (KHMS_PERMARK_SHORT_ANSWERS)
COVERED_RULE = 0              # 1 = the `covered by K-x` not-carded verdict (KHMS_PERMARK_COVERED_RULE)
EFFORT = "high"               # the per-mark call's --effort (KHMS_PERMARK_EFFORT, --effort)
EFFORTS = ("low", "medium", "high", "xhigh", "max")
FORMAT_CHECK = 1              # 1 = the later stages' checks on a candidate + one retry (KHMS_PERMARK_FORMAT_CHECK)
FORMAT_CHECKED = ("new", "supersede", "contradict", "belongs")   # the decisions that carry a candidate block
FORMAT_ANSWER_CHARS = 6000    # the rejected answer shown in the retry, at most
FORMAT_RETRY_HEAD = "## FORMAT CHECK FAILED — ANSWER AGAIN"
RULE_MARK_RE = re.compile(r"^[ \t]*<!-- (?P<end>/?)rule:(?P<name>[a-z-]+)(?:=(?P<val>\d))? -->[ \t]*$")
COVERED_RE = re.compile(r"::\s*covered by\s+(K-\d{5})\b")
LINK_LINE_KEYS = ("supersedes", "superseded_by", "contradicts", "refuted_by", "derived_from")
REASON_RE = re.compile(r"^\s*\**REASON:\**\s*(?P<why>.+?)\s*$", re.M)
SUMMARY_CHARS = 500
WHOLE_CHARS = 4000
TONIGHT_K = 5                 # tonight's candidates shown per mark (KHMS_PERMARK_TONIGHT_K, --tonight-k)
TONIGHT_LINES = 3             # a candidate not shown whole: its heading, these first lines, its MARKS line
PACKET_CHARS = 32000          # the packet budget (KHMS_PERMARK_PACKET_CHARS, --packet-chars)
EMBED_TIMEOUT_S = 10.0        # one op=embed call (a batch of tonight's new candidates, or the mark's query)
EMBED_BATCH = 32
EXCERPT_BEFORE_MIN, EXCERPT_AFTER_MIN, EXCERPT_CHARS = 20, 2, 6000
GIT_CHARS = 4000
DECISIONS = ("new", "belongs", "carded", "notcarded", "supersede", "contradict", "erratum")
NOT_CARDED_HEAD = ML.NOT_CARDED_HEAD


# --- queue and the output document -------------------------------------------------------------------
def parse_queue(text):
    out = []
    for ln in text.splitlines():
        m = QUEUE_RE.match(ln.strip())
        if m:
            d = m.groupdict()
            d["line"] = ln.strip()
            out.append(d)
    return out


class Doc:
    """The night's output file: candidates (label, block) then the NOT CARDED lines."""

    def __init__(self, title="# MARK stage output — per-mark evaluator (#237)"):
        self.title = title
        self.cands = []          # [[label, block]]
        self.notcarded = []      # ["- <id> :: <verdict>"]

    @classmethod
    def parse(cls, text):
        d = cls()
        nc = text.find("\n" + NOT_CARDED_HEAD)
        body, tail = (text, "") if nc < 0 else (text[:nc + 1], text[nc + 1:])
        heads = list(HEAD_RE.finditer(body))
        for i, h in enumerate(heads):
            end = heads[i + 1].start() if i + 1 < len(heads) else len(body)
            d.cands.append([f"{h.group(1)}{h.group(2)}{h.group(3)}", body[h.start():end].rstrip("\n") + "\n"])
        for ln in tail.splitlines()[1:]:
            if re.match(r"^\s*-\s*[MG]-[0-9a-f]{8}\s*::", ln):
                d.notcarded.append(ln.strip())
        return d

    def render(self):
        out = self.title + "\n\n"
        for _lab, block in self.cands:
            out += block.rstrip("\n") + "\n\n"
        out += NOT_CARDED_HEAD + "\n" + "".join(ln + "\n" for ln in self.notcarded)
        return out

    def decided(self):
        ids = set()
        for _lab, block in self.cands:
            for m in MARKS_LINE_RE.finditer(block):
                ids |= set(ML.ID_RE.findall(m.group(1)))
        for ln in self.notcarded:
            ids |= set(ML.ID_RE.findall(ln.split("::", 1)[0]))
        return ids

    def labels(self):
        return [lab for lab, _b in self.cands]

    def next_label(self):
        n = 0
        for lab in self.labels():
            m = re.match(r"^M(\d+)$", lab)
            if m:
                n = max(n, int(m.group(1)))
        return f"M{n + 1}"

    def find(self, label):
        for c in self.cands:
            if c[0].upper() == label.upper():
                return c
        return None


def write_atomic(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


# --- packet pieces -----------------------------------------------------------------------------------
def clip(s, n):
    return s if len(s) <= n else s[:n] + f" […{len(s) - n} more chars]"


def journal_context_lines(root, loc):
    """-> [(distance from the mark's line, rendered line)]; distance 0 is the mark's own line, whole."""
    m = re.match(r"^(journal|generated)/(.+):(\d+)$", loc)
    if not m or m.group(1) != "journal":
        return []
    try:
        lines = open(os.path.join(root, "journal", m.group(2)), encoding="utf-8",
                     errors="replace").read().splitlines()
    except OSError:
        return []
    n = int(m.group(3))
    out = []
    for i in range(max(1, n - CTX_BEFORE), min(len(lines), n + CTX_AFTER) + 1):
        t = lines[i - 1]
        out.append((abs(i - n), f"{'>>' if i == n else '  '} {i}: {t if i == n else clip(t, CTX_CLIP)}"))
    return out


def journal_context(root, loc):
    return "\n".join(t for _d, t in journal_context_lines(root, loc))


def query_of(text):
    q = re.sub(r"\((?:agent|sess|session|diblin)\b[^)]*\)", " ", text)
    q = ML.ID_RE.sub(" ", q)
    return " ".join(ks.tokens(q)[:NC.MAX_QUERY_TOKENS])


def card_summary(c):
    body = c.get("body", "")
    lines = [ln for ln in body.splitlines() if ln.strip()][:4]
    title = lines[0] if lines else ""
    rest = clip("\n".join(lines[1:4]), SUMMARY_CHARS)
    return f"{c['id']} [{c.get('status', '?')}] {clip(title, 300)}\n{rest}".rstrip()


def card_whole(c):
    return f"{c['id']} [{c.get('status', '?')}] (shown whole)\n{clip(c.get('body', ''), WHOLE_CHARS)}"


def _ids(v):
    if not v:
        return []
    return [x for x in (v if isinstance(v, list) else [v]) if CARD_ID_RE.fullmatch(str(x))]


def card_links_line(c, superseded_by=()):
    """`type: <type> · <key>: <ids> · …` — the card's type and the ids of its LINK_LINE_KEYS links, empty keys
    left out; `superseded_by` merges the card's own field with the reverse of other cards' `supersedes`."""
    links = c.get("links") or {}
    parts = [f"type: {c.get('type') or '?'}"]
    for key in LINK_LINE_KEYS:
        if key == "superseded_by":
            ids = list(dict.fromkeys(_ids(c.get("superseded_by")) + _ids(links.get("superseded_by"))
                                     + list(superseded_by)))
        else:
            ids = _ids(links.get(key))
        if ids:
            parts.append(f"{key}: {', '.join(ids)}")
    return " · ".join(parts) + ("" if len(parts) > 1 else " · no links")


def with_links_line(rendered, line):
    """The links line right under the card's heading line (whole and summary alike)."""
    head, _sep, rest = rendered.partition("\n")
    return f"{head}\n{line}" + (f"\n{rest}" if rest else "")


class Retrieval:
    """The base, loaded once per run: active non-fog cards, the glossary, the dense channel if it answers."""

    def __init__(self, cards=None, hy=None, glossary=None, dense=True, timeout_ms=1500, all_cards=None):
        if cards is None:
            all_cards = ks.load_cards()
            cards = [c for c in all_cards if c.get("status") == "active" and not c.get("fog")]
        self.cards = cards
        self.by_id = {c["id"]: c for c in self.cards}
        # a card the mark NAMES is shown whatever its status (an erratum or supersede target, a khms-issue pair)
        self.any_by_id = {c["id"]: c for c in (all_cards or cards)}
        self.glossary = glossary if glossary is not None else ks.load_glossary()
        self.hy = hy if hy is not None else (NC.open_dense(timeout_ms) if dense else None)
        self.timeout_ms = timeout_ms
        # tonight's candidates are not in the card index: the warm daemon embeds them (op=embed), each text
        # once per run.  `embed(texts, kind)` -> vectors; replaced by a fake in tests; None = lexical only.
        self.embed = self._daemon_embed if self.hy is not None else None
        self.vectors = {}
        self.tonight_dense_err = None
        self._rev_sup = None

    def superseded_by(self, cid):
        """Ids of the cards whose `supersedes` names `cid` (one index over every loaded card, built once)."""
        if self._rev_sup is None:
            self._rev_sup = {}
            for c in self.any_by_id.values():
                for tgt in _ids((c.get("links") or {}).get("supersedes")):
                    self._rev_sup.setdefault(tgt, []).append(c["id"])
        return self._rev_sup.get(cid, [])

    def lexical(self, q, topn=TOPK):
        if not q:
            return []
        exp, _ = ks.expand_query(q, self.glossary)
        res, _ = ks.search(exp, cards=self.cards, topn=topn)
        return [c for _s, _m, c in res]

    def hybrid(self, q, topn=TOPK):
        if not q or self.hy is None:
            return []
        try:
            res, meta = self.hy.hybrid_rank(q, cards=self.cards, topn=topn, timeout_ms=self.timeout_ms)
        except Exception:
            return []
        return [c for _s, _m, c in res] if meta.get("mode") == "hybrid" else []

    def _daemon_embed(self, texts, kind="doc"):
        import embed_index as ei
        cfg = ei.MODELS.get(self.hy.MODEL, {})
        pre = cfg.get("query_prefix" if kind == "query" else "doc_prefix", "")
        return ei.daemon_embed([pre + t for t in texts], self.hy.MODEL, timeout_s=EMBED_TIMEOUT_S,
                               sock_path=self.hy.SOCKET)

    def rank_texts(self, q, items, topn):
        """Rank [(key, text)] against the query `q` the way the cards are ranked: the lexical channel
        (glossary-expanded khms_search over the texts) fused by reciprocal rank with the dense channel
        (cosine of the embedded texts) when `self.embed` answers.  -> keys, nearest first, at most topn.
        Lexical alone ranks only texts that share a token with the query; the dense channel ranks every text,
        so with it the k nearest are always k."""
        if not items or not q:
            return []
        pseudo = [{"id": key, "body": text, "nbody": ks.norm(text), "first": text.split("\n", 1)[0],
                   "ntags": "", "tags": [], "fog": False} for key, text in items]
        exp, _ = ks.expand_query(q, self.glossary)
        lex = [c["id"] for _s, _m, c in ks.search(exp, cards=pseudo, topn=len(pseudo))[0]]
        dense = []
        if self.embed is not None and self.tonight_dense_err is None:
            try:
                new = list(dict.fromkeys(t for _k, t in items if t not in self.vectors))
                for j in range(0, len(new), EMBED_BATCH):    # each call bounded by EMBED_TIMEOUT_S
                    for t, v in zip(new[j:j + EMBED_BATCH], self.embed(new[j:j + EMBED_BATCH], "doc")):
                        self.vectors[t] = v
                qv = self.embed([q], "query")[0]
                sims = [(sum(a * b for a, b in zip(qv, self.vectors[t])), i) for i, (_k, t) in enumerate(items)]
                sims.sort(key=lambda x: (-x[0], x[1]))
                dense = [items[i][0] for _sim, i in sims]
            except Exception as exc:       # one failure decides the channel for the run (NC.open_dense's rule)
                self.tonight_dense_err = f"{type(exc).__name__}: {exc}"
                dense = []
        if not dense:
            return lex[:topn]
        import khms_recall_hybrid as HY
        fused = HY.rrf(lex, dense)
        order = {key: i for i, (key, _t) in enumerate(items)}
        return sorted(fused, key=lambda key: (-fused[key], order[key]))[:topn]


def nearest(text, R, whole_top=None):
    """-> (named cards, lexical, hybrid, ordered shown ids, ids shown whole): the named cards (at most
    NAMED_MAX) and the top `whole_top` (default WHOLE_TOP) of each channel are shown whole."""
    n = WHOLE_TOP if whole_top is None else whole_top
    named = [R.any_by_id[cid] for cid in dict.fromkeys(CARD_ID_RE.findall(text)) if cid in R.any_by_id]
    q = query_of(text)
    lex, hyb = R.lexical(q), R.hybrid(q)
    shown = list(dict.fromkeys([c["id"] for c in named] + [c["id"] for c in lex] + [c["id"] for c in hyb]))
    whole = set(c["id"] for c in named[:NAMED_MAX])
    whole |= {c["id"] for c in lex[:max(0, n)]} | {c["id"] for c in hyb[:max(0, n)]}
    return named, lex, hyb, shown, whole


def candidate_summary(block):
    """A candidate not shown whole: its heading, its first TONIGHT_LINES lines and its MARKS line."""
    lines = [ln for ln in block.rstrip("\n").splitlines() if ln.strip()]
    marks = [ln for ln in lines if MARKS_LINE_RE.match(ln)]
    body = [ln for ln in lines[1:] if not MARKS_LINE_RE.match(ln)]
    out = [clip(lines[0], 300)] + [clip(ln, 200) for ln in body[:TONIGHT_LINES]]
    if len(body) > TONIGHT_LINES:
        out.append(f"[… {len(body) - TONIGHT_LINES} more line(s) of this candidate not shown]")
    return "\n".join(out + marks[:1])


def tonight_items(doc, text, R=None, k=None):
    """-> [(label, rendered)] — the k of tonight's candidates nearest to the mark, nearest first; the nearest
    whole when it fits in WHOLE_CHARS, the others summarised."""
    if not doc.cands:
        return []
    k = TONIGHT_K if k is None else k
    items = [(lab, NEAREST_LINE_RE.sub("", block).rstrip("\n")) for lab, block in doc.cands]
    q = query_of(text)
    if R is None:
        R = Retrieval(cards=[], hy=None, glossary={})
    labs = R.rank_texts(q, items, k) if k > 0 else []
    body = dict(items)
    out = []
    for i, lab in enumerate(labs):
        b = body[lab]
        out.append((lab, b if i == 0 and len(b) <= WHOLE_CHARS else candidate_summary(b)))
    return out


def tonight_block(doc, text, R=None, k=None):
    items = tonight_items(doc, text, R, k)
    return "\n\n".join(t for _l, t in items), [lab for lab, _t in items]


def local_to_utc(date, hm):
    try:
        t = dt.datetime.strptime(f"{date} {hm}", "%Y-%m-%d %H:%M")
    except ValueError:
        return None
    return t.astimezone().astimezone(dt.timezone.utc)


def excerpt(digest_text, t_utc, before=EXCERPT_BEFORE_MIN, after=EXCERPT_AFTER_MIN, max_chars=EXCERPT_CHARS):
    """USER/ASSISTANT records of a digest whose stamp lies in [t-before, t+after], continuation lines kept,
    TOOL records dropped, the last `max_chars` characters (the end nearest the mark)."""
    lo = (t_utc - dt.timedelta(minutes=before)).strftime("%Y-%m-%dT%H:%M")
    hi = (t_utc + dt.timedelta(minutes=after)).strftime("%Y-%m-%dT%H:%M")
    out, keep = [], False
    for ln in digest_text.splitlines():
        m = DIGEST_REC_RE.match(ln)
        if m:
            keep = m.group(2) in ("USER", "ASSISTANT") and lo <= m.group(1) <= hi
        elif ln.startswith("===== SESSION") or ln.startswith("AGENT-RUN "):
            keep = False
            continue
        if keep:
            out.append(ln)
    text = "\n".join(out)
    if len(text) > max_chars:
        text = "[… earlier records cut]\n" + text[-max_chars:]
    return text


class Transcripts:
    """Session files by id, digested once per run (preprocess_transcripts.digest_file)."""

    def __init__(self, dirs=None, slug=None):
        if dirs is None:
            dirs = (P.get("KHMS_TRANSCRIPTS") or "").split()
        self.dirs = dirs
        self.slug = slug if slug is not None else (P.get("KHMS_PROJECT_SLUG") or "")
        self.cache = {}

    def find(self, sid, aid=None):
        import glob
        hits = []
        for d in self.dirs:
            if aid:
                pats = [os.path.join(d, "*", sid + "*", "subagents", "**", f"agent-{aid}*.jsonl"),
                        os.path.join(d, sid + "*", "subagents", "**", f"agent-{aid}*.jsonl")]
            else:
                pats = [os.path.join(d, "*", sid + "*.jsonl"), os.path.join(d, sid + "*.jsonl")]
            for pat in pats:
                hits += [h for h in glob.glob(pat, recursive=True) if not self.slug or self.slug in h]
        hits = sorted(set(hits), key=lambda h: -os.path.getsize(h))
        return hits[0] if hits else None

    def digest(self, path):
        if path not in self.cache:
            import preprocess_transcripts as PT
            buf = io.StringIO()
            try:
                PT.digest_file(path, buf)
            except Exception as exc:          # a broken transcript costs its excerpt, not the night
                buf.write(f"[digest failed: {exc}]\n")
            self.cache[path] = buf.getvalue()
        return self.cache[path]


def source_excerpt(mk, T, repos=None):
    """-> (heading, text) or ("", "")."""
    text = mk["text"]
    sid = aid = None
    g = SESS_GEN_RE.search(text)
    if g:
        sid, aid = g.group("sid"), g.group("aid")
    else:
        j = SESS_JOURNAL_RE.search(text)
        if j:
            sid = j.group("sid")
    if sid:
        t = local_to_utc(mk["date"], mk["time"])
        if t is None:
            return "", ""
        path = T.find(sid, aid)
        if not path:
            return f"transcript of session {sid} — not found in the transcript directories", ""
        ex = excerpt(T.digest(path), t)
        lo = (t - dt.timedelta(minutes=EXCERPT_BEFORE_MIN)).strftime("%H:%M")
        hi = (t + dt.timedelta(minutes=EXCERPT_AFTER_MIN)).strftime("%H:%M")
        return (f"transcript {os.path.basename(path)}, USER/ASSISTANT records {lo}–{hi} UTC "
                f"(the mark is at {t.strftime('%H:%M')} UTC)", ex or "(no USER/ASSISTANT record in the window)")
    gm = GIT_GEN_RE.search(text)
    if gm:
        if repos is None:
            repos = (P.get("KHMS_GIT_REPOS") or "").split()
        for repo in repos:
            try:
                cp = subprocess.run(["git", "-C", repo, "show", "--stat", "--date=iso", "--format=fuller",
                                     gm.group("sha")], capture_output=True, text=True, timeout=30)
            except (OSError, subprocess.TimeoutExpired):
                continue
            if cp.returncode == 0 and cp.stdout.strip():
                return f"git show --stat {gm.group('sha')} ({repo})", clip(cp.stdout, GIT_CHARS)
        return f"commit {gm.group('sha')} — not found in the configured repositories", ""
    return "", ""


def build_packet(mk, root, R, doc, T, repos=None, k=None, budget=None, stats=None, whole_top=None, links=None):
    """-> (packet text, shown card ids, tonight's labels shown).  `stats` (a dict) receives `chars` and
    `trimmed` (the items left out for the budget, lowest rank first).  `whole_top` and `links` default to
    WHOLE_TOP and LINKS_LINE."""
    budget = PACKET_CHARS if budget is None else budget
    links = LINKS_LINE if links is None else links
    ctx = journal_context_lines(root, mk["loc"])
    named, lex, hyb, shown, whole = nearest(mk["text"], R, whole_top)
    hyb_ids = "unavailable tonight" if R.hy is None else (", ".join(c["id"] for c in hyb) or "none")
    cards_head = (f"named by the mark: {', '.join(c['id'] for c in named) or 'none'}; "
                  f"lexical top {TOPK}: {', '.join(c['id'] for c in lex) or 'none'}; "
                  f"hybrid top {TOPK}: {hyb_ids}")
    cards = []
    for cid in shown:
        c = R.any_by_id.get(cid) or R.by_id[cid]
        t = card_whole(c) if cid in whole else card_summary(c)
        if links:
            t = with_links_line(t, card_links_line(c, R.superseded_by(cid)))
        cards.append((cid, t))
    tonight = tonight_items(doc, mk["text"], R, k)
    head, ex = source_excerpt(mk, T, repos)
    # the trim order, lowest rank first: rank i of the cards before rank i of tonight's candidates
    ranked = sorted([(i, 1, "card", key) for i, (key, _t) in enumerate(cards)]
                    + [(i, 0, "tonight", key) for i, (key, _t) in enumerate(tonight)], reverse=True)
    drop, trimmed = set(), []
    ex_keep = len(ex)
    ctx_keep = list(ctx)

    def render():
        parts = ["## THE MARK", mk["line"], ""]
        if ctx_keep:
            parts += ["## JOURNAL CONTEXT (`>>` = the mark's own line)", "\n".join(t for _d, t in ctx_keep), ""]
        parts += ["## NEAREST CARDS", cards_head]
        for key, t in cards:
            if ("card", key) not in drop:
                parts += ["", "---", t]
        parts.append("")
        shown_t = [t for key, t in tonight if ("tonight", key) not in drop]
        none = "none yet tonight" if not doc.cands else f"none of tonight's {len(doc.cands)} near this mark"
        parts += ["## TONIGHT'S CANDIDATES", "\n\n".join(shown_t) or none, ""]
        if head and ex_keep > 0:
            body = ex if ex_keep >= len(ex) else "[… cut for the packet budget]\n" + ex[len(ex) - ex_keep:]
            parts += ["## SOURCE EXCERPT — " + head, body, ""]
        elif head and not ex:
            parts += ["## SOURCE EXCERPT — " + head, ex, ""]
        out = trimmed + ([f"{len(ctx) - len(ctx_keep)} journal context line(s)"] if len(ctx_keep) < len(ctx)
                         else [])
        if out:
            parts.append(f"({len(out)} item(s) left out for the packet budget of {budget} characters, "
                         f"lowest rank first: {', '.join(out)})")
        parts.append(f"Answer with exactly one decision block for {mk['id']}.")
        return "\n".join(parts) + "\n"

    text = render()
    for _i, _o, sec, key in ranked:                     # 1. the lowest-ranked cards and candidates
        if len(text) <= budget:
            break
        drop.add((sec, key))
        trimmed.append(key)
        text = render()
    if len(text) > budget and ex_keep > 0:              # 2. the source excerpt, from its start
        over = len(text) - budget + 40
        ex_keep = max(0, ex_keep - over)
        if ex_keep < 200:
            ex_keep = 0
        trimmed.append("source excerpt" if ex_keep == 0 else "the start of the source excerpt")
        text = render()
    for d, t in sorted(ctx, key=lambda x: -x[0]):       # 3. journal context, farthest from the mark first
        if len(text) <= budget or d == 0:
            break
        ctx_keep.remove((d, t))
        text = render()
    if stats is not None:
        stats.update(chars=len(text), trimmed=list(trimmed) + (
            [f"{len(ctx) - len(ctx_keep)} journal context line(s)"] if len(ctx_keep) < len(ctx) else []))
    return (text, [key for key, _t in cards if ("card", key) not in drop],
            [key for key, _t in tonight if ("tonight", key) not in drop])


# --- the rules (system prompt) --------------------------------------------------------------------------
def spec_sections(path):
    try:
        text = open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        return ""
    m = re.search(r"(?ms)^## 4\b.*?(?=^## 6\b|\Z)", text)
    return m.group(0).strip() if m else ""


def pick_rule(text, name, value):
    """Keep the `<!-- rule:<name>=<value> -->` variant of every such block of `text`, drop the others and
    the marker lines.  A text with no block of that name is an error: the switch would silently do nothing."""
    out, cur, seen = [], None, False
    for ln in text.splitlines(keepends=True):
        m = RULE_MARK_RE.match(ln.rstrip("\n"))
        if m and m.group("name") == name:
            seen = True
            cur = None if m.group("end") else m.group("val")
            continue
        if cur is None or cur == str(int(value)):
            out.append(ln)
    if not seen:
        raise ValueError(f"no `rule:{name}` block in the prompt")
    return "".join(out)


def render_rules(root, short=None, covered=None):
    """The system prompt; `short` (default SHORT_ANSWERS) appends prompts/nightly-marks-short.md, `covered`
    (default COVERED_RULE) picks the `rule:covered` variant of both."""
    import khms_prompt
    short = SHORT_ANSWERS if short is None else short
    covered = COVERED_RULE if covered is None else covered
    rules = pick_rule(khms_prompt.render("nightly-marks"), "covered", covered)
    if short:
        rules = (rules.rstrip("\n") + "\n\n"
                 + pick_rule(khms_prompt.render("nightly-marks-short"), "covered", covered))
    spec = P.get("KHMS_SPEC") or os.path.join(P.REPO, "spec", "khms-spec.md")
    sec = spec_sections(spec)
    try:
        tags = open(os.path.join(root, "memory", "views", "tags.md"), encoding="utf-8").read()
    except OSError:
        tags = "(no tag registry in this base)"
    return (rules.rstrip("\n") + "\n\n# THE CARD SCHEMA — spec sections 4 and 5\n\n" + (sec or "(spec not found)")
            + "\n\n# THE TAG REGISTRY\n\n" + tags.strip() + "\n")


# --- the call -------------------------------------------------------------------------------------------
def claude_cmd(agent, model, rules, effort=None):
    return [agent, "-p", "--model", model, "--output-format", "json", "--tools", "",
            "--system-prompt", rules, "--setting-sources", "", "--strict-mcp-config",
            "--no-session-persistence", "--effort", effort or EFFORT]


def call(agent, model, rules, packet, timeout, effort=None):
    """-> dict(ok, text, usage, cost, turns, models, limit, err, wall)."""
    t0 = time.time()
    res = {"ok": False, "text": "", "usage": {}, "cost": None, "turns": None, "models": [], "limit": False,
           "err": "", "wall": 0.0}
    try:
        cp = RUNNER(claude_cmd(agent, model, rules, effort), input=packet, capture_output=True, text=True,
                    timeout=timeout, cwd=tempfile.gettempdir())
    except subprocess.TimeoutExpired:
        res.update(err=f"no answer within {timeout:.0f}s", wall=time.time() - t0)
        return res
    except OSError as exc:
        res.update(err=f"cannot run the agent: {exc}", wall=time.time() - t0)
        return res
    res["wall"] = time.time() - t0
    r = None
    for cand in [cp.stdout.strip()] + [ln.strip() for ln in reversed(cp.stdout.splitlines())]:
        if cand.startswith("{"):
            try:
                r = json.loads(cand)
                break
            except ValueError:
                continue
    if not isinstance(r, dict):
        res["err"] = f"rc={cp.returncode}: no JSON result ({(cp.stdout or '')[:120]!r}; {(cp.stderr or '')[:120]!r})"
        return res
    u = r.get("usage") or {}
    res["usage"] = {"in": u.get("input_tokens"), "out": u.get("output_tokens"),
                    "cache_read": u.get("cache_read_input_tokens"),
                    "cache_write": u.get("cache_creation_input_tokens")}
    res["cost"] = r.get("total_cost_usd")
    res["turns"] = r.get("num_turns")
    mu = r.get("modelUsage")
    if isinstance(mu, dict):
        res["models"] = sorted({(m.get("canonicalModel") if isinstance(m, dict) else None) or k
                                for k, m in mu.items()})
    res["text"] = str(r.get("result") or "")
    bad = cp.returncode != 0 or r.get("is_error")
    if bad and USAGE_LIMIT_RE.search(res["text"]):
        res.update(limit=True, err="usage limit")
        return res
    if bad:
        res["err"] = f"rc={cp.returncode} is_error={bool(r.get('is_error'))}: {res['text'][:160]!r}"
        return res
    res["ok"] = True
    return res


# --- parse and apply ------------------------------------------------------------------------------------
def candidate_of(text):
    """The first candidate block of an answer (from its `### X<n>:` heading), without a model NEAREST line."""
    m = HEAD_RE.search(text)
    if not m:
        return None
    block = FENCE_RE.sub("", text[m.start():])
    block = NEAREST_LINE_RE.sub("", block)
    nc = re.search(r"^## (?!#)", block, re.M)      # a stray trailer section is not part of the candidate
    if nc:
        block = block[:nc.start()]
    return block.strip("\n") + "\n"


def verdict_of(text, mid):
    for ln in text.splitlines():
        m = re.match(r"^\s*-\s*" + re.escape(mid) + r"\s*::\s*(.+?)\s*$", ln.strip().strip("`"))
        if m:
            return f"- {mid} :: {m.group(1)}"
    return None


def parse_answer(text, mid):
    """-> dict(kind, arg, block, verdict) or dict(kind='unparsed', why)."""
    m = DECISION_RE.search(text or "")
    if not m:
        return {"kind": "unparsed", "why": "no DECISION line"}
    kind = m.group("kind").lower()
    arg = (m.group("arg") or "").strip(".,:;")
    rest = text[m.end():]
    block = candidate_of(rest)
    verdict = verdict_of(rest, mid)
    kind = "notcarded" if kind == "not-carded" else kind
    if kind in ("carded", "supersede", "contradict", "erratum") and not CARD_ID_RE.fullmatch(arg):
        return {"kind": "unparsed", "why": f"{kind} without a K- id"}
    if kind in ("new", "supersede", "contradict"):
        if not block:
            return {"kind": "unparsed", "why": f"{kind} without a candidate"}
    elif kind == "belongs":
        if not re.fullmatch(r"[A-Z]\d+[a-z]?", arg or ""):
            return {"kind": "unparsed", "why": "belongs without a candidate label"}
    elif kind == "carded":
        if not verdict or not re.search(r"already carded\s+" + re.escape(arg), verdict):
            return {"kind": "unparsed", "why": "carded without its `already carded` line"}
    elif kind == "erratum":
        if not verdict or "erratum proposed " + arg not in verdict:
            return {"kind": "unparsed", "why": "erratum without its `erratum proposed` line"}
    elif kind == "notcarded":
        if not verdict:
            return {"kind": "unparsed", "why": "not-carded without its verdict line"}
        cm = COVERED_RE.search(verdict)
        arg = cm.group(1) if cm else ""         # `covered by K-x`: the row's target, never coverage
    rm = REASON_RE.search(rest) if kind in ("carded", "notcarded") else None
    return {"kind": kind, "arg": arg, "block": block, "verdict": verdict, "reason": rm.group("why") if rm else ""}


def set_label(block, label):
    return HEAD_RE.sub(lambda m: f"### {label}{m.group(4)}", block, count=1)


def ensure_marks(block, ids):
    """-> the block with exactly ONE `MARKS:` line, before **QUOTES:**: the ids of every MARKS line the block
    carries, then `ids` (order kept, each once).

    convert_inbox2 reads MARKS only from the prose before **QUOTES:** (`split_prose`), so a line the model wrote
    after the quote bullets gave the card no `marks:` (its marks stayed OPEN in the ledger) and a PLUMBING line in
    its body — 6 of 39 candidates on the trial night 2026-10-13-3 (#237).  A line already before the quotes stays
    where it is; one after them, or a second one, is moved there."""
    found = list(MARKS_LINE_NL_RE.finditer(block))
    cur = []
    for m in found:
        cur += [i for i in ML.ID_RE.findall(m.group(1)) if i not in cur]
    line = "MARKS: " + " ".join(cur + [i for i in ids if i not in cur]) + "\n"
    q = block.find("**QUOTES:**")
    keep = found[0] if found and (q < 0 or found[0].start() < q) else None
    parts, pos = [], 0
    for m in found:
        parts += [block[pos:m.start()], line if m is keep else ""]
        pos = m.end()
    rest = "".join(parts) + block[pos:]
    if keep is not None:
        return rest
    q = rest.find("**QUOTES:**")
    if q >= 0:
        return rest[:q] + line + rest[q:]
    return rest.rstrip("\n") + "\n" + line


def marks_of(block):
    m = MARKS_LINE_RE.search(block)
    return ML.ID_RE.findall(m.group(1)) if m else []


def with_nearest(block, ids):
    old = NEAREST_LINE_RE.search(block)
    have = CARD_ID_RE.findall(old.group(0)) if old else []
    allids = list(dict.fromkeys(have + list(ids)))
    block = NEAREST_LINE_RE.sub("", block).rstrip("\n")
    return block + "\n" + ("NEAREST: " + ", ".join(allids) if allids else
                           "NEAREST: (no card scored above zero)") + "\n"


def would_be(doc, mid, dec, shown):
    """-> (label, block): the candidate exactly as apply() writes it into OUT, or None (no block to write)."""
    k = dec["kind"]
    if k in ("new", "supersede", "contradict"):
        lab = doc.next_label()
        return lab, with_nearest(ensure_marks(set_label(dec["block"], lab), [mid]), shown)
    if k == "belongs" and dec.get("block"):
        c = doc.find(dec["arg"])
        if c is None:
            return None
        rev = ensure_marks(set_label(dec["block"], c[0]), [mid])
        return c[0], with_nearest(rev, CARD_ID_RE.findall((NEAREST_LINE_RE.search(c[1]) or [""])[0]) + list(shown))
    return None


class FormatCheck:
    """The checks the LATER stages run on a candidate, run on ONE candidate right after its answer is parsed.

    The same functions, called the way their stage calls them — nothing re-implemented:
      header    card_header.normalise_text + check_text  (publish: `fix`, then a malformed header is quarantined
                under ## DROPPED);
      relation  verify_relations.judge                   (postprocess: a DROP moves the candidate under ## DROPPED);
      schema    convert_inbox2.convert + render + selfcheck, i.e. approve_inbox.parse_cards and
                build_views.card_problems, the one per-card gate  (morning: convert_inbox2's SELF-CHECK);
      audit     convert_inbox2.edge_audit + relation_audit + census_lines' absorbed headings — with the SELF-CHECK
                the whole set that makes the converter refuse its file (a PLUMBING line in the written body, #237);
      lint      khms_lint.lint_batch with known_card_ids + load_retracted  (morning: approve_inbox refuses).
    The block checked is the one apply() writes (label, MARKS and NEAREST lines included), after the header
    normalisation the night applies before publishing.  Non-blocking flags (a verify_relations FLAG,
    khms_lint.flag_batch, the converter's t/e/g/s columns, a heading withheld under a trailer) are not problems.
    """

    def __init__(self, root):
        import card_header
        import convert_inbox2
        import khms_lint
        import verify_relations
        self.CH, self.CI, self.KL, self.VR = card_header, convert_inbox2, khms_lint, verify_relations
        self.know = os.path.join(root, "memory", "know")
        self.archive = os.path.join(root, "memory", "archive", "know")
        tags = os.path.join(root, "memory", "views", "tags.md")
        self.registry = convert_inbox2.load_tags(tags)
        if not self.registry:     # every candidate would fail "no tags" for the registry's reason, not its own
            raise ValueError(f"no tag table in {tags}")
        self.known = khms_lint.known_card_ids(self.know)
        self.retracted = khms_lint.load_retracted(root)
        self.ledger_ids = {t["ts"] for t in self.retracted}

    def problems(self, doc, mk, dec, shown):
        """-> [problem text] for the candidate `dec` would write; [] when it passes or carries no block."""
        wb = would_be(doc, mk["id"], dec, shown)
        if wb is None:
            return []
        label, block = wb
        block, _changed = self.CH.normalise_text(block)
        out = [f"card header: {p}" for p in self.CH.check_text(block)]
        verdict, msg = self.VR.judge(label, block, self.know, self.archive)
        if verdict == "DROP":
            out.append(f"RELATION: {msg}")
        cards, _rows = self.CI.convert(block, mk["date"], self.registry)
        if not cards:
            return out + ["the converter finds no card in the candidate — it starts with `### M<n>: <title>`"]
        fd, path = tempfile.mkstemp(prefix="permark-fmt-", suffix=".md")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(self.CI.render(cards))
            schema, parsed = self.CI.selfcheck(path, len(cards), self.registry)
        finally:
            os.unlink(path)
        out += [f"card schema: {p}" for p in schema]
        # the rest of what makes convert_inbox2 refuse its file (main(): rc 1, "NOT approve-ready"): the EDGE
        # AUDIT, the RELATION AUDIT (PLUMBING, LOST, LEAKED …) and a heading absorbed into the card before it
        lost, _n = self.CI.edge_audit(block, cards)
        split, _n = self.CI.relation_audit(block, cards)
        census, _a, _w = self.CI.census_lines(block, len(cards))
        out += [f"converter audit: {p}" for p in lost + split + [ln for ln in census[1:] if "absorbed" in ln]]
        batch = [(str(m.get("id")), m, b) for m, b in parsed]
        out += [f"approve lint: {p}" for p in
                self.KL.lint_batch(batch, self.known | {i for i, _m, _b in batch}, self.retracted, self.ledger_ids)]
        return out


def retry_packet(packet, answer, problems):
    """The same packet, then the rejected answer and the checks' own messages as a short correction."""
    return (packet.rstrip("\n") + "\n\n" + FORMAT_RETRY_HEAD + "\n"
            "Your answer below proposes a candidate that the checks of the later stages reject; as it stands it "
            "would be dropped or refused at review. Answer again for the same mark, with exactly one DECISION, "
            "and fix what these messages name — keep the content otherwise:\n"
            + "".join(f"- {p}\n" for p in problems)
            + "\nYOUR ANSWER:\n" + clip(answer.strip(), FORMAT_ANSWER_CHARS) + "\n")


def apply(doc, mid, dec, shown):
    """Apply one parsed decision to `doc`. -> (decision label for the counters, target, label or None)."""
    k = dec["kind"]
    if k in ("new", "supersede", "contradict"):
        lab, block = would_be(doc, mid, dec, shown)
        doc.cands.append([lab, block])
        return k, dec.get("arg") or "", lab
    if k == "belongs":
        c = doc.find(dec["arg"])
        if c is None:
            return None, f"belongs to an unknown label {dec['arg']}", None
        old = marks_of(c[1])
        if dec.get("block"):
            rev = ensure_marks(set_label(dec["block"], c[0]), [mid])
            if set(old) <= set(marks_of(rev)):
                c[1] = with_nearest(rev, CARD_ID_RE.findall((NEAREST_LINE_RE.search(c[1]) or [""])[0]) + list(shown))
                return "belongs", c[0], c[0]
        c[1] = ensure_marks(c[1], [mid])
        return "belongs", c[0], c[0]
    doc.notcarded.append(dec["verdict"])
    return k, dec.get("arg") or "", None


# --- the run --------------------------------------------------------------------------------------------
class Counters:
    def __init__(self):
        self.n = self.turns = self.cands = 0
        self.cost = 0.0
        self.charged = self.max_call = self.max_mark = 0.0      # the dollar cap's accounts (#243)
        self.dec = {k: 0 for k in DECISIONS}
        self.unparsed = self.failed = self.unproven = self.unseen = self.covered = 0
        self.fretry = self.ffailed = 0
        self.sizes, self.trimmed = [], 0
        self.dense_err_logged = False
        self.t0 = time.time()

    def stage_line(self):
        return (f"=== marks === permark: {self.n} mark(s) in {(time.time() - self.t0) / 60:.1f} min, "
                f"${self.cost:.4f} (CLI total_cost_usd), {self.turns} turns, {self.cands} candidate(s)")

    def decisions_line(self):
        return ("permark: decisions " + " ".join(f"{k}={v}" for k, v in self.dec.items())
                + f" covered={self.covered} unparsed={self.unparsed} failed={self.failed} quotes-unproven={self.unproven}"
                f" unseen-nearest={self.unseen} format-retry={self.fretry} format-failed={self.ffailed}")

    def packet_line(self, budget):
        z = sorted(self.sizes)
        if not z:
            return f"permark: packet_chars n=0 budget={budget} trimmed=0"
        med = z[len(z) // 2] if len(z) % 2 else (z[len(z) // 2 - 1] + z[len(z) // 2]) // 2
        return (f"permark: packet_chars n={len(z)} min={z[0]} median={med} max={z[-1]} budget={budget} "
                f"trimmed={self.trimmed}")


def format_gate(FC, doc, mk, dec, shown, res, packet, one_call, row, C, log):
    """The format check and its ONE retry. -> (the decision to apply, the usage-limit text or None).

    Passes: the decision as it is.  Fails: one more call with the same packet + the checks' messages; a retry
    that parses and passes replaces the decision (counted format-retry), anything else keeps the FIRST answer,
    i.e. today's path — the later stage drops or flags it with its reason (counted format-failed too)."""
    try:
        probs = FC.problems(doc, mk, dec, shown)
    except Exception as exc:      # the check's own failure: logged, the answer goes on unchecked, no call spent
        row["format"] = {"checked": False, "error": f"{type(exc).__name__}: {exc}"}
        log_to(log, f"permark-{mk['id']} format check could not run ({type(exc).__name__}: {exc}) — unchecked")
        return dec, None
    row["format"] = {"checked": True, "problems": probs}
    if not probs:
        return dec, None
    C.fretry += 1
    log_to(log, f"permark-{mk['id']} format check: {len(probs)} problem(s), one retry — " + " | ".join(probs))
    r2 = one_call(retry_packet(packet, res["text"], probs), "format retry")
    for k in ("in", "out", "cache_read", "cache_write"):
        if isinstance(r2["usage"].get(k), int):
            row["usage"][k] = (row["usage"].get(k) or 0) + r2["usage"][k]
    if r2["cost"] is not None:
        row["total_cost_usd"] = (row.get("total_cost_usd") or 0) + r2["cost"]
    if r2["turns"] is not None:
        row["turns"] = (row.get("turns") or 0) + r2["turns"]
    row["wall_s"] = round(row["wall_s"] + r2["wall"], 2)
    if not r2["ok"]:
        probs2 = [f"the retry call failed: {r2['err']}"]
    else:
        dec2 = parse_answer(r2["text"], mk["id"])
        probs2 = ([f"the retry is unparsed: {dec2['why']}"] if dec2["kind"] == "unparsed" else
                  FC.problems(doc, mk, dec2, shown) if dec2["kind"] in FORMAT_CHECKED else [])
        if not probs2:
            row["format"]["retry"] = "passed"
            log_to(log, f"permark-{mk['id']} format retry passed ({dec2['kind']})")
            return dec2, None
    C.ffailed += 1
    row["format"].update(retry="failed", retry_problems=probs2, retry_answer=r2["text"][:600])
    log_to(log, f"permark-{mk['id']} format check FAILED after the retry — the first answer is kept, the later "
                f"stage drops or flags it: " + " | ".join(probs2))
    return dec, (r2["text"] if r2["limit"] else None)


def log_to(path, line):
    stamp = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    text = line if re.match(r"^\d{4}-\d{2}-\d{2}T", line) else f"{stamp} {line}"
    print(text, flush=True)
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(text + "\n")


def card_text(root, cid):
    for d in ("memory/know", "memory/archive/know"):
        try:
            return open(os.path.join(root, d, cid + ".md"), encoding="utf-8", errors="replace").read()
        except OSError:
            continue
    return None


def config_int(cli, key, default):
    """--flag, else the KHMS_* key (environment, config file), else the module default."""
    if cli is not None:
        return cli
    try:
        v = P.get(key)
    except Exception:
        v = os.environ.get(key)
    try:
        return int(v) if v not in (None, "") else default
    except ValueError:
        raise SystemExit(f"permark: {key}={v!r} is not an integer")


def config_str(cli, key, default):
    if cli is not None:
        return cli
    try:
        v = P.get(key)
    except Exception:
        v = os.environ.get(key)
    return v.strip() if v not in (None, "") and v.strip() else default


def run(a):
    root = a.root or P.get("KHMS_ROOT")
    k = config_int(a.tonight_k, "KHMS_PERMARK_TONIGHT_K", TONIGHT_K)
    budget = config_int(a.packet_chars, "KHMS_PERMARK_PACKET_CHARS", PACKET_CHARS)
    whole_top = config_int(a.whole_top, "KHMS_PERMARK_WHOLE_TOP", WHOLE_TOP)
    links = config_int(a.links_line, "KHMS_PERMARK_LINKS_LINE", LINKS_LINE)
    short = config_int(a.short_answers, "KHMS_PERMARK_SHORT_ANSWERS", SHORT_ANSWERS)
    covered = config_int(a.covered_rule, "KHMS_PERMARK_COVERED_RULE", COVERED_RULE)
    effort = config_str(a.effort, "KHMS_PERMARK_EFFORT", EFFORT)
    fmt_on = config_int(a.format_check, "KHMS_PERMARK_FORMAT_CHECK", FORMAT_CHECK)
    if effort not in EFFORTS:
        print(f"permark: KHMS_PERMARK_EFFORT={effort!r} is not one of {', '.join(EFFORTS)}", file=sys.stderr)
        return 2
    try:
        queue = parse_queue(open(a.queue, encoding="utf-8").read())
    except OSError as exc:
        print(f"permark: no queue: {exc}", file=sys.stderr)
        return 2
    agent = a.agent or os.environ.get("KHMS_CLAUDE_BIN") or P.get("KHMS_AGENT")
    if not a.dry and not (agent and os.path.isabs(agent) and os.access(agent, os.X_OK)):
        print(f"permark: no executable agent CLI ({agent!r}); pass --agent <absolute path>", file=sys.stderr)
        return 2
    try:
        rules = render_rules(root, short=short, covered=covered)
    except Exception as exc:
        print(f"permark: the rules (nightly-marks prompt) cannot be rendered: {exc}", file=sys.stderr)
        return 2
    doc = Doc.parse(open(a.out, encoding="utf-8").read()) if os.path.exists(a.out) else Doc()
    done = doc.decided()
    todo = [mk for mk in queue if mk["id"] not in done]
    R = Retrieval(dense=not a.no_dense)
    T = Transcripts()
    C = Counters()
    if a.packets:
        os.makedirs(a.packets, exist_ok=True)
    FC, fmt_note = None, "off"
    if fmt_on and not a.dry:
        try:
            FC, fmt_note = FormatCheck(root), "on"
        except Exception as exc:   # a check that cannot run must not cost a retry: the candidates go unchecked
            fmt_note = f"UNAVAILABLE ({type(exc).__name__}: {exc}) — candidates go unchecked"
    log_to(a.log, f"permark: {len(queue)} queued mark(s), {len(queue) - len(todo)} already decided in {a.out}; "
                  f"{len(R.cards)} active cards, dense channel {'on' if R.hy is not None else 'OFF'}; model {a.model}; "
                  f"tonight's candidates k={k}, packet budget {budget} characters; whole top {whole_top}, "
                  f"links line {'on' if links else 'off'}, short answers {'on' if short else 'off'}, "
                  f"covered rule {'on' if covered else 'off'}, effort {effort}, format check {fmt_note}")
    rc = 0
    mark_start = None                    # what was charged when the previous mark started (its own charge follows)
    for i, mk in enumerate(todo):
        if mark_start is not None:
            C.max_mark = max(C.max_mark, C.charged - mark_start)
        if a.limit and C.n >= a.limit:
            log_to(a.log, f"permark: limit of {a.limit} mark(s) reached — {len(todo) - i} left OPEN")
            break
        if a.deadline and time.time() >= a.deadline:
            log_to(a.log, f"permark: deadline reached — {len(todo) - i} mark(s) left OPEN for the next night")
            break
        if a.max_usd and C.charged + C.max_mark > a.max_usd + 1e-9:
            log_to(a.log, f"permark: MARK stage stopped at the $ cap: ${C.charged:.2f} of ${a.max_usd:.2f}, "
                          f"{len(todo) - i} mark(s) left OPEN for the next night (reserve ${C.max_mark:.4f} = the "
                          f"dearest mark so far)")
            break
        mark_start = C.charged
        tp = time.time()
        ps = {}
        packet, shown, _labs = build_packet(mk, root, R, doc, T, k=k, budget=budget, stats=ps,
                                            whole_top=whole_top, links=links)
        prefetch_s = time.time() - tp
        C.sizes.append(len(packet))
        C.trimmed += 1 if ps.get("trimmed") else 0
        if R.tonight_dense_err and not C.dense_err_logged:
            log_to(a.log, f"permark: the dense channel for tonight's candidates FAILED ({R.tonight_dense_err}) "
                          "— lexical only for the rest of the run")
            C.dense_err_logged = True
        if a.packets:
            with open(os.path.join(a.packets, mk["id"] + ".txt"), "w", encoding="utf-8") as fh:
                fh.write(packet)
        if a.dry:
            C.n += 1
            log_to(a.log, f"permark-{mk['id']} dry: packet_chars={len(packet)}"
                          + (f", trimmed {len(ps['trimmed'])}" if ps.get("trimmed") else ""))
            continue
        def one_call(pk, tag, mk=mk):
            res = call(agent, a.model, rules, pk, a.timeout, effort)
            extra = [f"${res['cost']:.4f}" if res["cost"] is not None else "", f"{res['turns']} turns"
                     if res["turns"] is not None else "", res["err"] if res["err"] else "",
                     f"packet_chars={len(pk)}"]
            log_to(a.log, f"permark-{mk['id']} served-by: {','.join(res['models']) or 'UNKNOWN'} "
                          f"({tag}, rc={'0' if res['ok'] else '1'}"
                          + "".join(", " + e for e in extra if e) + ")")
            if a.served_file:
                with open(a.served_file, "a", encoding="utf-8") as fh:
                    fh.write(f"permark-{mk['id']}\t{','.join(res['models']) or 'UNKNOWN'}\n")
            C.cost += float(res["cost"] or 0)
            if res["cost"] is not None:
                C.max_call = max(C.max_call, float(res["cost"]))
            C.charged += float(res["cost"]) if res["cost"] is not None else C.max_call   # unknown != $0
            C.turns += int(res["turns"] or 0)
            return res

        res = None
        for attempt in (1, 2):
            res = one_call(packet, f"attempt {attempt}/2")
            if res["ok"] or res["limit"]:
                break
        C.n += 1
        row = {"id": mk["id"], "kind": mk["kind"], "loc": mk["loc"], "wall_s": round(res["wall"], 2),
               "prefetch_s": round(prefetch_s, 2), "usage": res["usage"], "total_cost_usd": res["cost"],
               "turns": res["turns"], "models": res["models"], "shown": shown, "packet_chars": len(packet),
               "trimmed": ps.get("trimmed") or []}
        if res["limit"]:
            row["decision"] = "usage-limit"
            C.n -= 1
            if a.limit_flag:
                with open(a.limit_flag, "w", encoding="utf-8") as fh:
                    fh.write(f"permark {mk['id']}: {res['text'][:200]}\n")
            log_to(a.log, f"permark: USAGE LIMIT at {mk['id']} — this and {len(todo) - i - 1} later mark(s) stay OPEN")
            rc = EXIT_USAGE_LIMIT
            _jsonl(a.jsonl, row)
            break
        if not res["ok"]:
            C.failed += 1
            row.update(decision="failed", why=res["err"])
            _jsonl(a.jsonl, row)
            continue
        dec = parse_answer(res["text"], mk["id"])
        if dec["kind"] == "unparsed":
            C.unparsed += 1
            row.update(decision="unparsed", why=dec["why"], answer=res["text"][:600])
            _jsonl(a.jsonl, row)
            continue
        limit_text = None
        if FC is not None and dec["kind"] in FORMAT_CHECKED:
            dec, limit_text = format_gate(FC, doc, mk, dec, shown, res, packet, one_call, row, C, a.log)
        kind, target, lab = apply(doc, mk["id"], dec, shown)
        if kind is None:
            C.unparsed += 1
            row.update(decision="unparsed", why=target, answer=res["text"][:600])
            _jsonl(a.jsonl, row)
            continue
        C.dec[kind] += 1
        C.covered += 1 if kind == "notcarded" and target else 0
        if kind in ("new", "supersede", "contradict"):
            C.cands += 1
            block = doc.find(lab)[1]
            top3 = [c["id"] for c in R.lexical(NC.query_text(block), 3)] + \
                   [c["id"] for c in R.hybrid(NC.query_text(block), 3)]
            miss = [x for x in dict.fromkeys(top3) if x not in shown]
            if miss:
                C.unseen += 1
                row["unseen_nearest"] = miss
        if kind == "carded" or (kind == "notcarded" and target):
            vq = ML.verdict_quote(re.sub(r"^\s*-\s*[MG]-[0-9a-f]{8}\s*::\s*", "", dec["verdict"]))
            ct = card_text(root, vq[1]) if vq else None
            if not (vq and ct and ML.quote_in_card(vq[2], ct)):
                C.unproven += 1
                row["quote_unproven"] = True
        row.update(decision=kind, target=target, label=lab)
        if dec.get("reason"):
            row["reason"] = dec["reason"]
        write_atomic(a.out, doc.render())
        _jsonl(a.jsonl, row)
        if limit_text is not None:
            if a.limit_flag:
                with open(a.limit_flag, "w", encoding="utf-8") as fh:
                    fh.write(f"permark {mk['id']} (format retry): {limit_text[:200]}\n")
            log_to(a.log, f"permark: USAGE LIMIT at the format retry of {mk['id']} (its first answer is kept) — "
                          f"{len(todo) - i - 1} later mark(s) stay OPEN")
            rc = EXIT_USAGE_LIMIT
            break
        if C.n % 60 == 0:
            log_to(a.log, C.stage_line())
            log_to(a.log, C.decisions_line())
            log_to(a.log, C.packet_line(budget))
    if not a.dry:
        if not os.path.exists(a.out):
            write_atomic(a.out, doc.render())
    log_to(a.log, C.stage_line())
    log_to(a.log, C.decisions_line())
    log_to(a.log, C.packet_line(budget))
    return rc


def _jsonl(path, row):
    if not path:
        return
    row = dict(row, ts=dt.datetime.now().astimezone().isoformat(timespec="seconds"))
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--queue", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--root", default=None)
    ap.add_argument("--deadline", type=float, default=0)
    ap.add_argument("--max-usd", type=float, default=0,
                    help="the stage's dollar cap: no new mark once the charge + the dearest mark would pass it "
                         "(0 = no cap; the night passes MARK_BUDGET_USD)")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--agent", default=None)
    ap.add_argument("--model", default=os.environ.get("KHMS_MARK_MODEL") or "sonnet")
    ap.add_argument("--timeout", type=float, default=180)
    ap.add_argument("--log", default=None)
    ap.add_argument("--jsonl", default=None)
    ap.add_argument("--served-file", default=None)
    ap.add_argument("--limit-flag", default=None)
    ap.add_argument("--packets", default=None)
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--no-dense", action="store_true")
    ap.add_argument("--tonight-k", type=int, default=None,
                    help=f"tonight's candidates shown per mark (KHMS_PERMARK_TONIGHT_K, default {TONIGHT_K})")
    ap.add_argument("--packet-chars", type=int, default=None,
                    help=f"the packet budget in characters (KHMS_PERMARK_PACKET_CHARS, default {PACKET_CHARS})")
    ap.add_argument("--whole-top", type=int, default=None,
                    help=f"the top N of each channel shown whole (KHMS_PERMARK_WHOLE_TOP, default {WHOLE_TOP})")
    ap.add_argument("--links-line", type=int, choices=(0, 1), default=None,
                    help=f"1 = a type + link-ids line under every shown card (KHMS_PERMARK_LINKS_LINE, "
                         f"default {LINKS_LINE})")
    ap.add_argument("--short-answers", type=int, choices=(0, 1), default=None,
                    help=f"1 = three-line carded / not-carded answers (KHMS_PERMARK_SHORT_ANSWERS, "
                         f"default {SHORT_ANSWERS})")
    ap.add_argument("--covered-rule", type=int, choices=(0, 1), default=None,
                    help=f"1 = a card with the topic makes the mark `covered by K-x` (not carded) unless its extra "
                         f"content changes an action (KHMS_PERMARK_COVERED_RULE, default {COVERED_RULE})")
    ap.add_argument("--effort", choices=EFFORTS, default=None,
                    help=f"the per-mark call's --effort (KHMS_PERMARK_EFFORT, default {EFFORT})")
    ap.add_argument("--format-check", type=int, choices=(0, 1), default=None,
                    help=f"1 = the later stages' checks on every candidate, one retry with their message "
                         f"(KHMS_PERMARK_FORMAT_CHECK, default {FORMAT_CHECK})")
    a = ap.parse_args(argv)
    return run(a)


if __name__ == "__main__":
    sys.exit(main())
