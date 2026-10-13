#!/usr/bin/env python3
"""permark.py — the nightly MARK stage as a per-mark evaluator: one packet, one call, one decision (#237).

Usage:
  permark.py --queue Q --out OUT [--deadline EPOCH] [--limit N] [--agent BIN] [--model sonnet]
             [--log LOG] [--jsonl J] [--served-file F] [--limit-flag F] [--timeout 180]
             [--packets DIR] [--dry]

  Q     the ledger's queue file (`mark_ledger.py queue --out Q`): `- <id> | <date time> | <kind> | <where> | <text>`,
        journal MARKs (`M-`) and generated lines (`G-`, #237 step 2) on one time axis.
  OUT   the night's output file, in the MARK stage's existing format (candidates `### M<n>:` with
        `MARKS:` lines, then `## MARKS NOT CARDED`).  It is rewritten ATOMICALLY after every mark; a run
        with the same OUT skips every id already decided in it (resume by id).
  --deadline  no NEW mark is started at or after this epoch (the stage's budget).
  --limit     at most N marks this run (a smoke or test run).
  --packets   also write every packet to DIR/<id>.txt (forensics, the hand check).
  --dry       build the packets only (implies --packets when given), no model call, OUT untouched.
  exit 0 done / deadline / limit; 75 the account's usage limit answered (the flag file is written);
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
     (khms_recall_hybrid, when the dense daemon answers).  The named cards and the top-1 of each channel
     are shown WHOLE (4,000 characters at most), the rest by id, status, title and 3 body lines;
  4. TONIGHT'S CANDIDATES — the 3 of OUT's candidates the lexical channel ranks closest plus the most
     recently written one, whole, 8,000 characters in all;
  5. SOURCE EXCERPT — `(sess <8 hex>)` in a journal mark or `src=digest sess <id>` in a generated line: the
     transcript's USER/ASSISTANT records of [t-20 min, t+2 min] (t = the mark's local time in UTC; TOOL
     records dropped, continuation lines kept, the last 6,000 characters); `src=gitlog <sha>`: the
     commit's `git show --stat`, 4,000 characters.

THE DECISIONS (parse_answer / apply): not-carded, new, belongs <label>, carded K-x, supersede K-x,
contradict K-x, erratum K-x.  An answer without a parseable block for THIS id is not written: the mark
stays OPEN for the next night and is counted `unparsed`.  `belongs` with a revision replaces the candidate
only when the revision keeps every id it had; otherwise the id is appended to the old one.  Every
candidate gets a `NEAREST:` line with the ids the packet showed, so verify_relations judges a
`RELATION: new` against exactly what the model saw.

LOGGED, NEVER GATED: `quotes-unproven` (an "already carded" quote mark_ledger.quote_in_card does not find
in the card) and `unseen-nearest` (a new candidate whose own nearest top 3 hold an id the packet lacked).
"""
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
NEAREST_LINE_RE = re.compile(r"^NEAREST:.*$\n?", re.M)
DECISION_RE = re.compile(r"^\s*\**DECISION:\**\s*(?P<kind>not-carded|new|belongs|carded|supersede|"
                         r"contradict|erratum)\b[ \t]*(?P<arg>[^\s`*]+)?", re.M | re.I)
FENCE_RE = re.compile(r"^\s*```[a-z]*\s*$", re.M)
DIGEST_REC_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2}T\d{2}:\d{2})\] ([A-Z-]+):")

CTX_BEFORE, CTX_AFTER, CTX_CLIP = 6, 3, 600
TOPK = 8
NAMED_MAX = 3
SUMMARY_CHARS = 500
WHOLE_CHARS = 4000
TONIGHT_CHARS = 8000
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


def journal_context(root, loc):
    m = re.match(r"^(journal|generated)/(.+):(\d+)$", loc)
    if not m or m.group(1) != "journal":
        return ""
    try:
        lines = open(os.path.join(root, "journal", m.group(2)), encoding="utf-8",
                     errors="replace").read().splitlines()
    except OSError:
        return ""
    n = int(m.group(3))
    out = []
    for i in range(max(1, n - CTX_BEFORE), min(len(lines), n + CTX_AFTER) + 1):
        t = lines[i - 1]
        out.append(f"{'>>' if i == n else '  '} {i}: {t if i == n else clip(t, CTX_CLIP)}")
    return "\n".join(out)


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


def nearest(text, R):
    """-> (named cards, lexical, hybrid, ordered shown ids, ids shown whole)."""
    named = [R.any_by_id[cid] for cid in dict.fromkeys(CARD_ID_RE.findall(text)) if cid in R.any_by_id]
    q = query_of(text)
    lex, hyb = R.lexical(q), R.hybrid(q)
    shown = list(dict.fromkeys([c["id"] for c in named] + [c["id"] for c in lex] + [c["id"] for c in hyb]))
    whole = set(c["id"] for c in named[:NAMED_MAX])
    if lex:
        whole.add(lex[0]["id"])
    if hyb:
        whole.add(hyb[0]["id"])
    return named, lex, hyb, shown, whole


def tonight_block(doc, text):
    if not doc.cands:
        return "", []
    pseudo = []
    for lab, block in doc.cands:
        body = NEAREST_LINE_RE.sub("", block)
        first = body.splitlines()[0] if body else ""
        pseudo.append({"id": lab, "body": body, "nbody": ks.norm(body), "first": first, "ntags": "",
                       "tags": [], "fog": False})
    res, _ = ks.search(query_of(text), cards=pseudo, topn=3)
    labs = [c["id"] for _s, _m, c in res]
    last = doc.cands[-1][0]
    if last not in labs:
        labs.append(last)
    out, used = [], 0
    for lab in labs:
        block = doc.find(lab)[1].rstrip("\n")
        if used + len(block) > TONIGHT_CHARS and out:
            break
        out.append(block)
        used += len(block)
    return "\n\n".join(out), labs


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


def build_packet(mk, root, R, doc, T, repos=None):
    """-> (packet text, shown card ids, tonight's labels shown)."""
    parts = ["## THE MARK", mk["line"], ""]
    ctx = journal_context(root, mk["loc"])
    if ctx:
        parts += ["## JOURNAL CONTEXT (`>>` = the mark's own line)", ctx, ""]
    named, lex, hyb, shown, whole = nearest(mk["text"], R)
    parts.append("## NEAREST CARDS")
    hyb_ids = "unavailable tonight" if R.hy is None else (", ".join(c["id"] for c in hyb) or "none")
    parts.append(f"named by the mark: {', '.join(c['id'] for c in named) or 'none'}; "
                 f"lexical top {TOPK}: {', '.join(c['id'] for c in lex) or 'none'}; "
                 f"hybrid top {TOPK}: {hyb_ids}")
    for cid in shown:
        c = R.any_by_id.get(cid) or R.by_id[cid]
        parts += ["", "---", card_whole(c) if cid in whole else card_summary(c)]
    parts.append("")
    tb, labs = tonight_block(doc, mk["text"])
    parts += ["## TONIGHT'S CANDIDATES", tb or "none yet tonight", ""]
    head, ex = source_excerpt(mk, T, repos)
    if head:
        parts += ["## SOURCE EXCERPT — " + head, ex, ""]
    parts.append(f"Answer with exactly one decision block for {mk['id']}.")
    return "\n".join(parts) + "\n", shown, labs


# --- the rules (system prompt) --------------------------------------------------------------------------
def spec_sections(path):
    try:
        text = open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        return ""
    m = re.search(r"(?ms)^## 4\b.*?(?=^## 6\b|\Z)", text)
    return m.group(0).strip() if m else ""


def render_rules(root):
    import khms_prompt
    rules = khms_prompt.render("nightly-marks")
    spec = P.get("KHMS_SPEC") or os.path.join(P.REPO, "spec", "khms-spec.md")
    sec = spec_sections(spec)
    try:
        tags = open(os.path.join(root, "memory", "views", "tags.md"), encoding="utf-8").read()
    except OSError:
        tags = "(no tag registry in this base)"
    return (rules.rstrip("\n") + "\n\n# THE CARD SCHEMA — spec sections 4 and 5\n\n" + (sec or "(spec not found)")
            + "\n\n# THE TAG REGISTRY\n\n" + tags.strip() + "\n")


# --- the call -------------------------------------------------------------------------------------------
def claude_cmd(agent, model, rules):
    return [agent, "-p", "--model", model, "--output-format", "json", "--tools", "",
            "--system-prompt", rules, "--setting-sources", "", "--strict-mcp-config",
            "--no-session-persistence", "--effort", "high"]


def call(agent, model, rules, packet, timeout):
    """-> dict(ok, text, usage, cost, turns, models, limit, err, wall)."""
    t0 = time.time()
    res = {"ok": False, "text": "", "usage": {}, "cost": None, "turns": None, "models": [], "limit": False,
           "err": "", "wall": 0.0}
    try:
        cp = RUNNER(claude_cmd(agent, model, rules), input=packet, capture_output=True, text=True,
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
    return {"kind": kind, "arg": arg, "block": block, "verdict": verdict}


def set_label(block, label):
    return HEAD_RE.sub(lambda m: f"### {label}{m.group(4)}", block, count=1)


def ensure_marks(block, ids):
    """`MARKS:` line = its own ids plus `ids` (order kept); added before **QUOTES:** when absent."""
    m = MARKS_LINE_RE.search(block)
    if m:
        cur = ML.ID_RE.findall(m.group(1))
        new = cur + [i for i in ids if i not in cur]
        return block[:m.start()] + "MARKS: " + " ".join(new) + block[m.end():]
    q = block.find("**QUOTES:**")
    line = "MARKS: " + " ".join(ids) + "\n"
    if q >= 0:
        return block[:q] + line + block[q:]
    return block.rstrip("\n") + "\n" + line


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


def apply(doc, mid, dec, shown):
    """Apply one parsed decision to `doc`. -> (decision label for the counters, target, label or None)."""
    k = dec["kind"]
    if k in ("new", "supersede", "contradict"):
        lab = doc.next_label()
        block = with_nearest(ensure_marks(set_label(dec["block"], lab), [mid]), shown)
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
        self.dec = {k: 0 for k in DECISIONS}
        self.unparsed = self.failed = self.unproven = self.unseen = 0
        self.t0 = time.time()

    def stage_line(self):
        return (f"=== marks === permark: {self.n} mark(s) in {(time.time() - self.t0) / 60:.1f} min, "
                f"${self.cost:.4f} (CLI total_cost_usd), {self.turns} turns, {self.cands} candidate(s)")

    def decisions_line(self):
        return ("permark: decisions " + " ".join(f"{k}={v}" for k, v in self.dec.items())
                + f" unparsed={self.unparsed} failed={self.failed} quotes-unproven={self.unproven}"
                f" unseen-nearest={self.unseen}")


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


def run(a):
    root = a.root or P.get("KHMS_ROOT")
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
        rules = render_rules(root)
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
    log_to(a.log, f"permark: {len(queue)} queued mark(s), {len(queue) - len(todo)} already decided in {a.out}; "
                  f"{len(R.cards)} active cards, dense channel {'on' if R.hy is not None else 'OFF'}; model {a.model}")
    rc = 0
    for i, mk in enumerate(todo):
        if a.limit and C.n >= a.limit:
            log_to(a.log, f"permark: limit of {a.limit} mark(s) reached — {len(todo) - i} left OPEN")
            break
        if a.deadline and time.time() >= a.deadline:
            log_to(a.log, f"permark: deadline reached — {len(todo) - i} mark(s) left OPEN for the next night")
            break
        tp = time.time()
        packet, shown, _labs = build_packet(mk, root, R, doc, T)
        prefetch_s = time.time() - tp
        if a.packets:
            with open(os.path.join(a.packets, mk["id"] + ".txt"), "w", encoding="utf-8") as fh:
                fh.write(packet)
        if a.dry:
            C.n += 1
            continue
        res = None
        for attempt in (1, 2):
            res = call(agent, a.model, rules, packet, a.timeout)
            extra = [f"${res['cost']:.4f}" if res["cost"] is not None else "", f"{res['turns']} turns"
                     if res["turns"] is not None else "", res["err"] if res["err"] else ""]
            log_to(a.log, f"permark-{mk['id']} served-by: {','.join(res['models']) or 'UNKNOWN'} "
                          f"(attempt {attempt}/2, rc={'0' if res['ok'] else '1'}"
                          + "".join(", " + e for e in extra if e) + ")")
            if a.served_file:
                with open(a.served_file, "a", encoding="utf-8") as fh:
                    fh.write(f"permark-{mk['id']}\t{','.join(res['models']) or 'UNKNOWN'}\n")
            C.cost += float(res["cost"] or 0)
            C.turns += int(res["turns"] or 0)
            if res["ok"] or res["limit"]:
                break
        C.n += 1
        row = {"id": mk["id"], "kind": mk["kind"], "loc": mk["loc"], "wall_s": round(res["wall"], 2),
               "prefetch_s": round(prefetch_s, 2), "usage": res["usage"], "total_cost_usd": res["cost"],
               "turns": res["turns"], "models": res["models"], "shown": shown}
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
        kind, target, lab = apply(doc, mk["id"], dec, shown)
        if kind is None:
            C.unparsed += 1
            row.update(decision="unparsed", why=target, answer=res["text"][:600])
            _jsonl(a.jsonl, row)
            continue
        C.dec[kind] += 1
        if kind in ("new", "supersede", "contradict"):
            C.cands += 1
            block = doc.find(lab)[1]
            top3 = [c["id"] for c in R.lexical(NC.query_text(block), 3)] + \
                   [c["id"] for c in R.hybrid(NC.query_text(block), 3)]
            miss = [x for x in dict.fromkeys(top3) if x not in shown]
            if miss:
                C.unseen += 1
                row["unseen_nearest"] = miss
        if kind == "carded":
            am = re.search(r"already carded\s+(K-\d{5})\b\s*[:—–-]?\s*(.*)$", dec["verdict"])
            ct = card_text(root, dec["arg"])
            if not (am and ct and ML.quote_in_card(am.group(2), ct)):
                C.unproven += 1
                row["quote_unproven"] = True
        row.update(decision=kind, target=target, label=lab)
        write_atomic(a.out, doc.render())
        _jsonl(a.jsonl, row)
        if C.n % 60 == 0:
            log_to(a.log, C.stage_line())
            log_to(a.log, C.decisions_line())
    if not a.dry:
        if not os.path.exists(a.out):
            write_atomic(a.out, doc.render())
    log_to(a.log, C.stage_line())
    log_to(a.log, C.decisions_line())
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
    a = ap.parse_args(argv)
    return run(a)


if __name__ == "__main__":
    sys.exit(main())
