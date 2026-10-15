#!/usr/bin/env python3
"""card_validity.py -- does an approved card still hold against today's trees? Finds, never applies.

Approved cards are immutable, and the systems they describe keep moving: a card that names a path, a
value or a mechanism can be untrue today with nothing in the base noticing. This tool asks the question
card by card, in two halves, and only ever PROPOSES:

  1. MECHANICAL (no model, deterministic, seconds for a whole base): every concrete anchor a card names --
     file paths, path:line, script and file names, systemd unit names, git shas, snake_case keys -- is
     classified against read-only copies of today's trees:
       exists     the path (or name / key / sha) is there
       moved      the path is gone but the same file name exists elsewhere (where = the new places)
       gone       the path is gone and the name is nowhere (or too common to say where it went)
       line-gone  the file exists but is shorter than the cited line
       absent     a bare name / unit / key / sha that no tree contains
       external   a path outside every configured tree (/etc, a host directory, a journal file ...)
       unchecked  a sha with no git repository configured
     A gone anchor is a HINT, not a verdict: a card that states a mechanism can survive a moved file.

  2. MODEL per card, "as for a dummy": a fixed step list (claim -> sub-claims -> one command each -> quote
     the output -> verdict holds / no-longer-holds / cannot-check-offline), the step-1 anchors as input and a
     fixed JSON answer. The checker then RE-RUNS every evidence command itself, in the same read-only
     folder, through an allowlist with no shell (cat grep head tail ls wc, relative paths only), and
     compares exit code and quoted output. A schema error, a quote that is not in the real output, a
     verdict without evidence or a false_sentence that is not in the card goes back to the model with the
     exact error, at most --max-retries times; then the card is `unverified`. What a model says about its
     own work is not evidence; the re-run is.

Two model legs share the prompt, the schema and the re-run:
  qwen    the local model through the Copilot CLI harness (KHMS_COPILOT_BIN, KHMS_COPILOT_URL = an
          OpenAI-compatible base URL, KHMS_LLM_MODEL). The harness is UNTRUSTED: it runs with an empty
          environment and its own HOME, in a new session killed as a group at the deadline, with only
          read tools (bash scoped to the allowlist, view, grep, glob) inside a chmod-a-w copy of the trees;
          a manifest of that copy is compared after every call and any change aborts the leg (ESCAPE).
  sonnet  `claude -p` (KHMS_AGENT, model alias --sonnet-model) with Read/Grep/Glob and Bash scoped to the
          same allowlist, no settings sources, no MCP; its own `total_cost_usd` is the price.

Subcommands:
  sandbox    --out DIR --tree NAME=git:REPO@REV | NAME=dir:PATH ...   read-only copy of the trees
  mechanical --know DIR --sandbox DIR [--out FILE]                     anchors of every active card
  calibrate  --know DIR --sandbox DIR --out DIR [--n 50 --seed 219]    sample, both legs, report
  report     --out DIR                                                 rebuild the report from the legs
  backfill   --know DIR --calib sonnet.jsonl --date D                  ledger lines for a calibration
  sample     --know DIR --seed S [--n 100 --against sample.json]       draw past the ledger, judge nothing
  run        --know DIR --sandbox DIR --out DIR --seed S [--n 100]     draw, mechanical, value flag, judge
  proposals  --know DIR --out FILE --results LABEL=FILE ...            K-05297 routes + commands

THE CHECKED LEDGER (`run`, `sample`, `backfill`; default <know>/../ledger/validity.jsonl): one append-only
line per judged card -- id, sha256 of the card file as judged, date, judge, prompt version, verdict,
knowhow, reason, evidence, the repo-value flag. A draw (uniform, seeded, statuses active+challenged by
default) skips every card whose CURRENT hash has a line, so a card that changed since is eligible again.
Prompt v2 asks, in the same call, whether the card is know-how (would a future agent act differently?)
or only status/progress. `run` judges with the paid leg only, at most 4 calls in flight, and stops
submitting once its spend passes --max-usd (exit 3). The base itself (cards, inbox, status) is never
written; only the ledger and --out are.

Outputs of `calibrate` (all in --out): mechanical.jsonl, mechanical-summary.json, sample.json,
qwen.jsonl, sonnet.jsonl, calibration.md, summary.json and proposals.md -- one line per no-longer-holds
both legs agree on (or the paid leg alone, flagged SONNET-ONLY) with the sentence, the evidence and the
suggested path (erratum wrong-record / dated-pointer / supersede). Nothing is written into the base.
"""
import argparse
import concurrent.futures
import datetime
import hashlib
import json
import math
import os
import random
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import threading
import time
from collections import Counter, defaultdict

import yaml

ACTIVE_TYPES = ("fact", "action→outcome", "problem→solution")
VERDICTS = ("holds", "no-longer-holds", "cannot-check-offline")
KNOWHOW = ("yes", "no")
PROMPT_VERSION = "v2"           # v1 = the 2026-10-11 calibration prompt; v2 adds the know-how question
DRAW_STATUSES = ("active", "challenged")
PATHS = ("none", "erratum-wrong-record", "dated-pointer", "supersede")
STATUSES = ("exists", "moved", "gone", "line-gone", "absent", "external", "unchecked")

# --------------------------------------------------------------------------------------------- cards


def parse_card(text, name):
    """{id, type, status, date, body, text, name} of one card file; ValueError on a card with no header."""
    if not text.startswith("---"):
        raise ValueError(f"{name}: no front matter")
    end = text.find("\n---", 3)
    if end < 0:
        raise ValueError(f"{name}: unterminated front matter")
    meta = yaml.safe_load(text[3:end]) or {}
    body = text[end + 4:].lstrip("\n")
    cid = str(meta.get("id") or os.path.splitext(os.path.basename(name))[0])
    return {"id": cid, "type": str(meta.get("type") or ""), "status": str(meta.get("status") or ""),
            "date": str(meta.get("date") or ""), "body": body, "text": text, "name": name, "meta": meta}


def load_cards(know):
    """Every parseable card of `know`; each carries `sha256` of the file's bytes as read -- the content the
    judge sees is the content the ledger names."""
    out = []
    for f in sorted(os.listdir(know)):
        if not re.fullmatch(r"K-\d+\.md", f):
            continue
        with open(os.path.join(know, f), "rb") as fh:
            raw = fh.read()
        try:
            card = parse_card(raw.decode("utf-8", errors="replace"), f)
        except (ValueError, yaml.YAMLError):
            continue
        card["sha256"] = hashlib.sha256(raw).hexdigest()
        out.append(card)
    return out


# ------------------------------------------------------------------------------------------- anchors

EXT = {"py", "sh", "bash", "yaml", "yml", "json", "jsonl", "md", "txt", "xacro", "urdf", "launch", "cpp",
       "hpp", "h", "c", "cc", "toml", "cfg", "ini", "rules", "env", "csv", "conf", "service", "timer",
       "socket", "target", "mount", "xml", "sdf", "usd", "usda", "rviz", "lua", "in", "j2"}
UNIT_EXT = {"service", "timer", "socket", "target", "mount"}
EXTERNAL_PREFIXES = ("/etc/", "/opt/", "/usr/", "/dev/", "/sys/", "/proc/", "/var/", "/run/", "/tmp/",
                     "/lib/", "/boot/", "/mnt/", "/media/", "/srv/", "/root/", "/snap/", "/bin/", "/sbin/")
_URL = re.compile(r"\b[a-z][a-z0-9+.-]*://\S+", re.I)
_TOKEN = re.compile(r"[~\w@+./-]+(?::\d+)?")
_BACKTICK = re.compile(r"`([^`\n]{1,300})`")
_IDENT = re.compile(r"(?<![\w.-])([A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+)(?![\w-])")
_SHA_CTX = re.compile(r"(?:\b(?:commit|commits|sha|merge|merged|cherry-picked|revert|reverted|HEAD|at)\b"
                      r"[\s:=]{1,3}|@)([0-9a-f]{7,40})\b")
_SHA_TICK = re.compile(r"`([0-9a-f]{7,40})`")
_CARD_FILE = re.compile(r"^K-\d+\.md$")


def _ext(seg):
    parts = seg.split(".")
    return parts[-1].lower() if len(parts) > 1 and parts[0] else ""


def _is_sha(s):
    return bool(re.search(r"[a-f]", s) and re.search(r"\d", s))


def _pathlike(tok):
    """(kind, value, path, line) for a path/name token, else None."""
    tok = tok.rstrip(".,;:)'\"")
    m = re.fullmatch(r"(.+?):(\d+)", tok)
    path, line = (m.group(1), int(m.group(2))) if m else (tok, None)
    if not path or path in (".", "..", "/", "~"):
        return None
    if "/" in path:
        segs = [s for s in path.split("/") if s not in ("", ".", "~")]
        if not segs:
            return None
        is_dir = path.endswith("/")
        if not is_dir and _ext(segs[-1]) not in EXT:
            return None
        if is_dir and (len(segs) < 2 or not all(re.fullmatch(r"[\w@+.-]+", s) for s in segs)):
            return None
        if any(re.fullmatch(r"\d+([.,]\d+)?", s) for s in segs):       # 1/2, 3.5/10 -- not paths
            return None
        value = tok if m else path
        return ("path:line" if line else "path", value, path, line)
    ext = _ext(path)
    if ext not in EXT or _CARD_FILE.match(path) or not re.search(r"[A-Za-z]", path.rsplit(".", 1)[0]):
        return None
    if line:
        return ("path:line", tok, path, line)
    return ("unit" if ext in UNIT_EXT else "name", path, path, None)


def extract_anchors(text):
    """Anchors of a card text, in text order, de-duplicated by (kind, value). Deterministic."""
    clean = _URL.sub(" ", text)
    found = []

    def add(kind, value, path=None, line=None):
        found.append({"kind": kind, "value": value, "path": path, "line": line})

    for m in _TOKEN.finditer(clean):
        r = _pathlike(m.group(0))
        if r:
            kind, value, path, line = r
            add(kind, value, path, line)
            if kind == "path:line" and "/" in path:          # the file itself is an anchor too
                add("path", path, path, None)
    for rx in (_SHA_CTX, _SHA_TICK):
        for m in rx.finditer(clean):
            if _is_sha(m.group(1)):
                add("sha", m.group(1))
    for m in _BACKTICK.finditer(clean):
        span = " ".join(t for t in m.group(1).split() if not _pathlike(t) and "/" not in t)
        for im in _IDENT.finditer(span):
            ident = im.group(1)
            if len(ident) >= 5 and not _is_sha(ident.replace("_", "")):
                add("key", ident)
    out, seen = [], set()
    for a in found:
        k = (a["kind"], a["value"])
        if k not in seen:
            seen.add(k)
            out.append(a)
    return out


# ---------------------------------------------------------------------------------------- tree index

TEXT_EXT = EXT | {"hh", "hxx", "cxx", "ts", "js", "service", "ini", "cmake", "msg", "srv", "action"}
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".pytest_cache"}


class TreeIndex:
    """Paths, names and identifiers of read-only trees; `check(anchor)` classifies one anchor."""

    def __init__(self, trees, git_repos=()):
        self.trees = dict(trees)
        self.git_repos = [r for r in git_repos if r]
        self.files = {}                 # "tree/rel" -> absolute path
        self.suffix = defaultdict(list)   # path suffix (by segments) -> ["tree/rel", ...]
        self.dsuffix = set()              # directory suffixes
        self.dirnames = set()
        self.base = defaultdict(list)     # basename -> ["tree/rel", ...]
        for name, root in self.trees.items():
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
                reld = os.path.relpath(dirpath, root)
                dsegs = [name] + ([] if reld == "." else reld.split(os.sep))
                for k in range(1, len(dsegs) + 1):
                    self.dsuffix.add("/".join(dsegs[-k:]))
                self.dirnames.update(dsegs)
                for f in sorted(filenames):
                    segs = dsegs + [f]
                    key = "/".join(segs)
                    self.files[key] = os.path.join(dirpath, f)
                    self.base[f].append(key)
                    for k in range(1, len(segs) + 1):
                        self.suffix["/".join(segs[-k:])].append(key)
        self._idents = None
        self._lines = {}
        self._sha = {}

    # identifiers are indexed on first use: a key check needs them, a path check does not
    def idents(self):
        if self._idents is None:
            s = set()
            for key, p in self.files.items():
                s.update(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", key))
                if _ext(os.path.basename(p)) not in TEXT_EXT and "." in os.path.basename(p):
                    continue
                try:
                    if os.path.getsize(p) > 2_000_000:
                        continue
                    with open(p, encoding="utf-8", errors="replace") as fh:
                        s.update(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", fh.read()))
                except OSError:
                    continue
            self._idents = s
        return self._idents

    def nlines(self, key):
        if key not in self._lines:
            try:
                with open(self.files[key], "rb") as fh:
                    self._lines[key] = sum(1 for _ in fh)
            except OSError:
                self._lines[key] = 0
        return self._lines[key]

    def sha_exists(self, sha):
        if sha not in self._sha:
            hit = False
            for repo in self.git_repos:
                r = subprocess.run(["git", "-C", repo, "rev-parse", "--verify", "--quiet", sha + "^{commit}"],
                                   capture_output=True, text=True)
                amb = "ambiguous" in r.stderr
                if r.returncode == 0 or amb:
                    hit = True
                    break
            self._sha[sha] = hit
        return self._sha[sha]

    def _lookup(self, segs, absolute):
        """Matches of the longest accepted suffix: the whole relative path, or for an absolute path any
        suffix of at least two segments (its leading part names a host directory, not a tree)."""
        lo = min(2, len(segs)) if absolute else len(segs)
        for k in range(len(segs), lo - 1, -1):
            hit = self.suffix.get("/".join(segs[-k:]))
            if hit:
                return hit
        return []

    def check(self, a):
        kind, value = a["kind"], a["value"]
        res = {"kind": kind, "value": value, "status": None, "where": []}
        if kind == "sha":
            res["status"] = ("exists" if self.sha_exists(value) else "absent") if self.git_repos else "unchecked"
            return res
        if kind == "key":
            res["status"] = "exists" if value in self.idents() else "absent"
            return res
        if kind in ("name", "unit"):
            hit = self.base.get(value, [])
            res.update(status="exists" if hit else "absent", where=hit[:3])
            return res
        path, line = a.get("path") or value, a.get("line")
        absolute = path.startswith("/") or path.startswith("~")
        if path.startswith("/") and path.startswith(EXTERNAL_PREFIXES):
            res["status"] = "external"
            return res
        is_dir = path.endswith("/")
        segs = [s for s in path.split("/") if s not in ("", ".", "~", "..")]
        if not segs:
            res["status"] = "external"
            return res
        if is_dir:
            lo = min(2, len(segs)) if absolute else len(segs)
            ok = any("/".join(segs[-k:]) in self.dsuffix for k in range(len(segs), lo - 1, -1))
            res["status"] = "exists" if ok else ("gone" if set(segs[:-1]) & self.dirnames else "external")
            return res
        hit = self._lookup(segs, absolute) if len(segs) > 1 else self.base.get(segs[0], [])
        if hit:
            res["where"] = hit[:3]
            if line and all(self.nlines(k) < line for k in hit):
                res["status"] = "line-gone"
            else:
                res["status"] = "exists"
            return res
        elsewhere = self.base.get(segs[-1], [])
        if 1 <= len(elsewhere) <= 3:
            res.update(status="moved", where=elsewhere)
        elif elsewhere or (set(segs[:-1]) & self.dirnames):
            res["status"] = "gone"
        else:
            res["status"] = "external"
        return res


def mechanical_pass(cards, idx, statuses=("active",)):
    t0 = time.time()
    rows = []
    by_status = Counter({s: 0 for s in STATUSES})
    by_kind = defaultdict(Counter)
    flagged = 0
    for c in cards:
        if c["status"] not in statuses:
            continue
        anchors = [idx.check(a) for a in extract_anchors(c["body"])]
        for r in anchors:
            by_status[r["status"]] += 1
            by_kind[r["kind"]][r["status"]] += 1
        bad = any(r["status"] in ("gone", "moved", "line-gone") for r in anchors)
        flagged += bad
        rows.append({"id": c["id"], "type": c["type"], "date": c["date"], "anchors": anchors})
    summary = {"cards": len(rows), "anchors": sum(by_status.values()), "by_status": dict(by_status),
               "by_kind": {k: dict(v) for k, v in sorted(by_kind.items())},
               "cards_with_gone_or_moved": flagged,
               "cards_with_anchors": sum(1 for r in rows if r["anchors"]),
               "runtime_s": round(time.time() - t0, 2)}
    return rows, summary


# ----------------------------------------------------------------------------------- evidence re-run

ALLOWED_COMMANDS = ("cat", "grep", "head", "tail", "ls", "wc")
_GREP_VALUE_OPTS = {"-e", "-m", "-A", "-B", "-C", "--max-count", "--regexp"}
_GREP_FORBIDDEN = {"-f", "--file"}
_N_VALUE_OPTS = {"-n", "-c"}
_OPERATORS = set("();<>|&")
EVIDENCE_TIMEOUT = 30
_OUT_CAP = 400_000


def _norm(s):
    return " ".join(str(s).split())


def _quote_in(quote, out):
    """Every non-empty line of the quote appears, whitespace-normalised and character for character, in the
    real output: lines may be skipped, none may be reworded."""
    o = _norm(out)
    lines = [_norm(x) for x in quote.splitlines() if x.strip()]
    return bool(lines) and all(x in o for x in lines)


def _prose(s):
    """A card sentence compared without its markdown emphasis: backticks and asterisks are typography."""
    return _norm(re.sub(r"[`*]", "", str(s)))


def evidence_argv(cmd):
    """argv of an allowed evidence command, or raise ValueError('not allowed: ...')."""
    try:
        lex = shlex.shlex(cmd, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        argv = list(lex)
    except ValueError as e:
        raise ValueError(f"not allowed: the command does not parse ({e})")
    if not argv:
        raise ValueError("not allowed: empty command")
    for t in argv:
        if t and set(t) <= _OPERATORS:
            raise ValueError(f"not allowed: shell operator {t!r} -- one plain command, no pipes, ; && > or cd")
    if argv[0] not in ALLOWED_COMMANDS:
        raise ValueError(f"not allowed: command {argv[0]!r} -- use only {', '.join(ALLOWED_COMMANDS)}")
    paths = []
    args = argv[1:]
    if argv[0] == "grep":
        pattern_given, i = False, 0
        while i < len(args):
            t = args[i]
            if t in _GREP_FORBIDDEN or t.startswith("--file="):
                raise ValueError("not allowed: grep -f reads patterns from a file")
            if t == "--":
                rest = args[i + 1:]
                if not pattern_given and rest:
                    rest = rest[1:]
                paths += rest
                break
            if t in _GREP_VALUE_OPTS:
                pattern_given = pattern_given or t in ("-e", "--regexp")
                i += 2
                continue
            if t.startswith("-") and len(t) > 1:
                if t.startswith("--regexp="):
                    pattern_given = True
                i += 1
                continue
            if not pattern_given:
                pattern_given = True
            else:
                paths.append(t)
            i += 1
    else:
        i = 0
        while i < len(args):
            t = args[i]
            if argv[0] in ("head", "tail") and t in _N_VALUE_OPTS:
                i += 2
                continue
            if t.startswith("-") and len(t) > 1:
                i += 1
                continue
            paths.append(t)
            i += 1
    for p in paths:
        if p.startswith("/") or p.startswith("~"):
            raise ValueError(f"not allowed: absolute path {p!r} -- use a path relative to the folder root")
        if os.path.normpath(p).split(os.sep)[0] == "..":
            raise ValueError(f"not allowed: path {p!r} leaves the folder")
    return argv


def run_evidence(cmd, root):
    """(exit code, stdout+stderr) of an allowed command run in `root` with no shell."""
    argv = evidence_argv(cmd)
    try:
        cp = subprocess.run(argv, cwd=root, capture_output=True, text=True, errors="replace",
                            timeout=EVIDENCE_TIMEOUT, stdin=subprocess.DEVNULL,
                            env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"})
    except subprocess.TimeoutExpired:
        return 124, f"(timed out after {EVIDENCE_TIMEOUT}s)"
    return cp.returncode, ((cp.stdout or "") + (cp.stderr or ""))[:_OUT_CAP]


def rerun_evidence(ev, root):
    """None when the evidence reproduces, else one error line naming what differs."""
    cmd = ev.get("command") if isinstance(ev, dict) else None
    if not isinstance(cmd, str) or not cmd.strip():
        return "evidence item has no command string"
    try:
        rc, out = run_evidence(cmd, root)
    except ValueError as e:
        return f"evidence `{cmd}`: {e}"
    quote = ev.get("output_quote")
    want_rc = ev.get("exit_code")
    errs = []
    if isinstance(want_rc, bool) or not isinstance(want_rc, int):
        errs.append(f"exit_code must be an integer, got {want_rc!r}")
    elif want_rc != rc:
        errs.append(f"you wrote exit_code {want_rc}, the real exit code is {rc}")
    if not isinstance(quote, str):
        errs.append(f"output_quote must be a string, got {quote!r}")
    elif quote.strip() == "":
        if out.strip():
            errs.append(f"output_quote is empty but the real output is not: {_norm(out)[:300]!r}")
    elif not _quote_in(quote, out):
        errs.append(f"output_quote {quote[:300]!r} not found in the real output; the real output begins: "
                    f"{_norm(out)[:300]!r}")
    return f"evidence `{cmd}`: " + "; ".join(errs) if errs else None


# ------------------------------------------------------------------------------------ answer schema

ANSWER_EXAMPLE = {
    "card_id": "K-00000",
    "subclaims": [{
        "claim": "the script dumpee/tools/example.sh exists",
        "verdict": "no-longer-holds",
        "evidence": [{"command": "ls dumpee/tools/example.sh", "exit_code": 2,
                      "output_quote": "No such file or directory"}],
        "reason": "ls cannot find the file",
    }],
    "verdict": "no-longer-holds",
    "false_sentence": "copy one sentence of the card here, exactly as written",
    "suggested_path": "dated-pointer",
    "knowhow": "yes",
    "knowhow_reason": "it names a value a future agent would otherwise get wrong",
}
MAX_SUBCLAIMS = 8
MAX_EVIDENCE = 16


def parse_answer(text):
    """The JSON object of a model answer (a ```json fence, else the outermost braces); ValueError if none."""
    text = text or ""
    fences = re.findall(r"```(?:json)?\s*\n(.*?)```", text, re.S)
    candidates = [f.strip() for f in reversed(fences)]
    i, j = text.find("{"), text.rfind("}")
    if 0 <= i < j:
        candidates.append(text[i:j + 1])
    for c in candidates:
        try:
            obj = json.loads(c)
        except ValueError:
            continue
        if isinstance(obj, dict):
            return obj
    raise ValueError("no JSON object found in your answer -- answer with ONLY the JSON object")


def derived_verdict(subs):
    vs = [s.get("verdict") for s in subs]
    if "no-longer-holds" in vs:
        return "no-longer-holds"
    if "holds" in vs:
        return "holds"
    return "cannot-check-offline"


def check_answer(ans, card, root):
    """Every reason the answer cannot be accepted, as error lines; [] = accepted."""
    errs = []
    if not isinstance(ans, dict):
        return ["the answer is not a JSON object"]
    for key, typ in (("card_id", str), ("subclaims", list), ("verdict", str), ("false_sentence", str),
                     ("suggested_path", str)):
        if key not in ans:
            errs.append(f"missing key {key!r}")
        elif not isinstance(ans[key], typ):
            errs.append(f"key {key!r} must be a {typ.__name__}, got {type(ans[key]).__name__}")
    if any(not isinstance(ans.get(k), t) for k, t in (("subclaims", list), ("verdict", str))):
        return errs
    ans = dict(ans)
    ans.setdefault("card_id", card["id"]); ans.setdefault("false_sentence", ""); ans.setdefault("suggested_path", "none")
    for k in ("card_id", "false_sentence", "suggested_path"):
        if not isinstance(ans[k], str):
            ans[k] = str(ans[k])
    if ans["card_id"] != card["id"]:
        errs.append(f"card_id is {ans['card_id']!r}, the card is {card['id']!r}")
    if ans["verdict"] not in VERDICTS:
        errs.append(f"verdict {ans['verdict']!r} is not one of {', '.join(VERDICTS)}")
    if "suggested_path" in ans and ans["suggested_path"] not in PATHS:
        errs.append(f"suggested_path {ans['suggested_path']!r} is not one of {', '.join(PATHS)}")
    subs = ans["subclaims"]
    if not 1 <= len(subs) <= MAX_SUBCLAIMS:
        errs.append(f"subclaims must hold 1 to {MAX_SUBCLAIMS} items, got {len(subs)}")
    n_ev = 0
    for k, s in enumerate(subs, 1):
        if not isinstance(s, dict):
            errs.append(f"subclaim {k} is not an object")
            continue
        for key, typ in (("claim", str), ("verdict", str), ("evidence", list), ("reason", str)):
            if not isinstance(s.get(key), typ):
                errs.append(f"subclaim {k}: key {key!r} missing or not a {typ.__name__}")
        v = s.get("verdict")
        if isinstance(v, str) and v not in VERDICTS:
            errs.append(f"subclaim {k}: verdict {v!r} is not one of {', '.join(VERDICTS)}")
        evs = s.get("evidence") if isinstance(s.get("evidence"), list) else []
        if v in ("holds", "no-longer-holds") and not evs:
            errs.append(f"subclaim {k}: verdict {v!r} without evidence -- run a command and quote its output")
        for ev in evs:
            n_ev += 1
            if n_ev > MAX_EVIDENCE:
                errs.append(f"more than {MAX_EVIDENCE} evidence items")
                break
            if not isinstance(ev, dict):
                errs.append(f"subclaim {k}: an evidence item is not an object")
                continue
            e = rerun_evidence(ev, root)
            if e:
                errs.append(f"subclaim {k}: {e}")
    if ans["verdict"] in VERDICTS and all(isinstance(s, dict) for s in subs) and subs:
        d = derived_verdict(subs)
        if d != ans["verdict"]:
            errs.append(f"verdict {ans['verdict']!r} does not follow from the subclaims: the rule in step 6 "
                        f"gives {d!r}")
    if ans["verdict"] == "no-longer-holds":
        fs = ans["false_sentence"]
        if not fs.strip():
            errs.append("false_sentence is empty: copy the sentence of the card that no longer holds")
        elif _prose(fs) not in _prose(card["body"]):
            errs.append(f"false_sentence {fs[:200]!r} is not in the card: copy it exactly, character for "
                        f"character")
        if ans["suggested_path"] == "none":
            errs.append("suggested_path is 'none' for a no-longer-holds card: pick dated-pointer, "
                        "erratum-wrong-record or supersede")
    kh = ans.get("knowhow")
    if kh not in KNOWHOW:
        errs.append(f"knowhow must be \"yes\" or \"no\" (step 8), got {kh!r}")
    if "knowhow_reason" in ans and not isinstance(ans["knowhow_reason"], str):
        errs.append("knowhow_reason must be a string")
    return errs


# ------------------------------------------------------------------------------------------- prompt

PROMPT = """You are checking ONE knowledge card against today's files. Work slowly and exactly. Follow the
steps in order. Do not skip a step.

THE FOLDER. Your current directory holds read-only copies of today's trees:
{trees}
You cannot see the robot, the network, other machines, or anything that is running. You cannot change
any file.

THE CARD {card_id} (written on {date}):
<<<CARD
{body}
CARD>>>

WHAT A SCRIPT ALREADY FOUND. These are the files, names and keys the card mentions, checked against the
folder ("gone" = no such path any more, "moved" = the same file name exists at another path, "absent" =
no tree contains it, "external" = outside the folder, not checkable):
{anchors}
A gone or moved anchor is a HINT, not a verdict: a card that explains a mechanism can still hold after a
file moved. Check the claim itself.

STEPS
1. Read the card. Decide what it claims is true TODAY.
2. Split that into 1 to 5 sub-claims. Each sub-claim is one short sentence. Good sub-claims name a file,
   a value, a function or a behaviour that a file in the folder can confirm or refute.
3. For each sub-claim that a file can show, run ONE command (two at most) from this list:
   cat, grep, head, tail, ls, wc. Run it from the folder root. Use paths relative to the folder root,
   for example: grep -rn "min_turning_radius" dumpee/dumpee_navigation/config
   Never use pipes (|), ;, &&, >, cd, or an absolute path. Other commands are refused.
4. Copy ONE line of the output you got (or a part of one line) into output_quote, EXACTLY, character for
   character. Do not add a file name, a period or a word; do not join lines; do not retype from memory.
   If the command printed nothing, write "". Write the exit code the command really ended with
   (0 = success; grep with no match = 1; ls of a missing file = 2). Do not add 2>/dev/null.
5. Give each sub-claim one verdict:
   - "holds": the output shows the sub-claim is still true today.
   - "no-longer-holds": the output shows the sub-claim is false today: the file is gone, the value is
     different, the code does something else.
   - "cannot-check-offline": no file in the folder can show it. This is the verdict for: the running
     robot or any live system state, measurements, timings, hardware, past events ("on 2026-08-03 the
     test failed"), a person's decision or words, and anything outside the folder. Say why in reason.
   A past event that happened is NOT no-longer-holds. A value that changed later IS no-longer-holds.
6. The card verdict: "no-longer-holds" if any sub-claim is no-longer-holds; otherwise "holds" if any
   sub-claim holds; otherwise "cannot-check-offline".
7. If the card verdict is "no-longer-holds": copy into false_sentence ONE sentence of the card, exactly
   as written between <<<CARD and CARD>>>, that is not true today, and pick suggested_path:
   - "dated-pointer": the sentence states a VALUE (a number, a path, a version, a setting) that was true
     when written and has since changed in the repository.
   - "supersede": the sentence describes a mechanism or behaviour that has since been replaced.
   - "erratum-wrong-record": the sentence was already wrong on the day it was written.
   Otherwise write false_sentence "" and suggested_path "none".
8. KNOW-HOW (a separate question about the whole card, whatever steps 1-7 found): would a future agent
   ACT DIFFERENTLY knowing this card? "yes" for a finding that holds beyond its day, a refutation, a
   measured fact with its number, a decision WITH its reason, a mistake WITH the measure that stops it,
   a rule or a policy. "no" when the card only records status or progress: something was launched,
   finished, passed, committed, pushed, tagged, merged, deployed or moved on a board, or what the next
   step is. Write "yes" or "no" into knowhow and one short sentence into knowhow_reason.

ANSWER with ONLY this JSON object (same keys, your values), nothing before or after it:
{example}

HOW YOUR ANSWER IS CHECKED, so you know when you did it right: a script re-runs every command in your
evidence in the same folder and checks that the exit code matches and that output_quote appears in the
real output. A changed quote, a forbidden command, a "holds" or "no-longer-holds" without evidence, a
card verdict that does not follow step 6, or a false_sentence that is not in the card sends your answer
back with the exact error. An answer that passes every check is accepted.
"""

FEEDBACK = """

YOUR PREVIOUS ANSWER WAS REJECTED.
Your previous answer was:
<<<ANSWER
{answer}
ANSWER>>>
It was REJECTED for these reasons:
{errors}
Fix exactly these problems. Re-run a command if you need its real output. Then answer again with the
complete JSON object and nothing else.
"""


def anchor_lines(anchors, cap=40):
    if not anchors:
        return "  (none)"
    out = []
    for a in anchors[:cap]:
        where = f" -> {', '.join(a.get('where') or [])}" if a.get("where") and a.get("status") != "exists" else ""
        out.append(f"  - {a['kind']} {a['value']}: {a['status']}{where}")
    if len(anchors) > cap:
        out.append(f"  ... and {len(anchors) - cap} more")
    return "\n".join(out)


def tree_lines(root):
    desc = {}
    try:
        with open(os.path.join(root, ".trees.json"), encoding="utf-8") as fh:
            desc = json.load(fh)
    except (OSError, ValueError):
        pass
    names = sorted(d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d)) and not d.startswith("."))
    return "\n".join(f"  {n}/  {desc.get(n, {}).get('description', '')}".rstrip() for n in names) or "  (empty)"


def build_prompt(card, anchors, root):
    return PROMPT.format(trees=tree_lines(root), card_id=card["id"], date=card["date"] or "unknown",
                         body=card["body"].strip(), anchors=anchor_lines(anchors),
                         example=json.dumps(dict(ANSWER_EXAMPLE, card_id=card["id"]), indent=2,
                                            ensure_ascii=False))


def judge_card(card, model, root, anchors, max_retries=2, deadline_s=None):
    """Ask `model(prompt) -> (text, meta)` about one card, re-run its evidence, feed errors back.
    Returns {verdict, attempts, answer, errors, wall_s, cost_usd, meta}; verdict 'unverified' when no answer
    passed within 1 + max_retries attempts (or within deadline_s of wall clock across attempts)."""
    first = build_prompt(card, anchors, root)
    prompt, attempts, history, metas = first, 0, [], []
    t0 = time.time()
    while attempts <= max_retries:
        if attempts and deadline_s and time.time() - t0 >= deadline_s:
            history.append({"attempt": attempts + 1, "errors": [f"card deadline {deadline_s}s spent"]})
            break
        attempts += 1
        text = ""
        try:
            text, meta = model(prompt)
            metas.append(meta or {})
            try:
                ans = parse_answer(text)
            except ValueError as e:
                errs, ans = [str(e)], None
            else:
                errs = check_answer(ans, card, root)
        except EscapeError:
            raise
        except Exception as e:  # noqa: BLE001 -- a failed call is an attempt, not a crash
            errs, ans = [f"model call failed: {type(e).__name__}: {e}"], None
            metas.append({"error": str(e)})
        if not errs:
            return _result(ans["verdict"], attempts, ans, history, metas, t0)
        history.append({"attempt": attempts, "errors": errs, "answer_text": (text or "")[:8000]})
        prompt = first + FEEDBACK.format(answer=(text or "(no answer)")[:6000],
                                         errors="\n".join(f"- {e}" for e in errs))
    return _result("unverified", attempts, None, history, metas, t0)


def _result(verdict, attempts, ans, history, metas, t0):
    return {"verdict": verdict, "attempts": attempts, "answer": ans, "errors": history,
            "wall_s": round(time.time() - t0, 1),
            "cost_usd": round(sum(float(m.get("cost_usd") or 0) for m in metas), 6), "meta": metas}


# ------------------------------------------------------------------------------------- model legs

class EscapeError(RuntimeError):
    """The sandbox changed during a harness call: the harness wrote where it must not."""


def manifest(root):
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for f in sorted(filenames) + [d for d in dirnames if os.path.islink(os.path.join(dirpath, d))]:
            p = os.path.join(dirpath, f)
            try:
                st = os.lstat(p)
            except OSError:
                continue
            out.append((os.path.relpath(p, root), st.st_size, st.st_mtime_ns))
    return out


def _kill_group(proc):
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except OSError:
        return
    for _ in range(20):
        if proc.poll() is not None:
            break
        time.sleep(0.25)
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        pass


def _run_group(argv, *, cwd, env, deadline, stdin_text=None, stdout_path=None):
    """Run argv in its own session; kill the whole group at the deadline. (rc, stdout, wall, timed_out)."""
    t0 = time.time()
    out_fh = open(stdout_path, "w", encoding="utf-8") if stdout_path else subprocess.PIPE
    try:
        proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.PIPE if stdin_text is not None
                                else subprocess.DEVNULL, stdout=out_fh, stderr=subprocess.PIPE, text=True,
                                start_new_session=True)
        try:
            out, err = proc.communicate(input=stdin_text, timeout=deadline)
            timed_out = False
        except subprocess.TimeoutExpired:
            _kill_group(proc)
            out, err = proc.communicate()
            timed_out = True
        _kill_group(proc)            # anything the harness left behind in its group
    finally:
        if stdout_path:
            out_fh.close()
    if stdout_path:
        with open(stdout_path, encoding="utf-8", errors="replace") as fh:
            out = fh.read()
    return proc.returncode, out or "", (err or "")[-4000:], round(time.time() - t0, 1), timed_out


class CopilotModel:
    """qwen through the Copilot CLI harness, confined as described in the module docstring."""
    SHELL_ALLOW = [f"shell({c}:*)" for c in ALLOWED_COMMANDS]

    def __init__(self, bin_path, base_url, model, root, workdir, deadline=420):
        self.bin, self.base_url, self.model = bin_path, base_url, model
        self.root, self.workdir, self.deadline = root, workdir, deadline
        self.home = os.path.join(workdir, "copilot-home")
        self.tmp = os.path.join(workdir, "copilot-tmp")
        os.makedirs(self.home, exist_ok=True)
        os.makedirs(self.tmp, exist_ok=True)
        self.calls = 0
        self.lock = threading.Lock()

    def argv(self, prompt):
        a = [self.bin, "-C", self.root, "-p", prompt, "--available-tools", "bash", "view", "grep", "glob",
             "--allow-tool", "view", "--allow-tool", "grep", "--allow-tool", "glob"]
        for s in self.SHELL_ALLOW:
            a += ["--allow-tool", s]
        a += ["--deny-tool", "write", "--no-ask-user", "--no-auto-update", "--no-color",
              "--no-custom-instructions", "--disable-builtin-mcps", "--disallow-temp-dir",
              "--output-format", "json", "--log-level", "error",
              "--log-dir", os.path.join(self.workdir, "copilot-logs")]
        return a

    def env(self):
        npm_bin = os.path.dirname(self.bin)
        return {"HOME": self.home, "TMPDIR": self.tmp, "TERM": "dumb", "LANG": "C.UTF-8",
                "PATH": f"{npm_bin}:/usr/local/bin:/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1",
                "COPILOT_PROVIDER_BASE_URL": self.base_url, "COPILOT_PROVIDER_TYPE": "openai",
                "COPILOT_MODEL": self.model, "COPILOT_OFFLINE": "true", "COPILOT_PROVIDER_API_KEY": "local"}

    def __call__(self, prompt):
        with self.lock:
            self.calls += 1
            n = self.calls
            before = manifest(self.root)
            log = os.path.join(self.workdir, "copilot-runs", f"{n:04d}.jsonl")
            os.makedirs(os.path.dirname(log), exist_ok=True)
            rc, out, err, wall, timed_out = _run_group(self.argv(prompt), cwd=self.root, env=self.env(),
                                                       deadline=self.deadline, stdout_path=log)
            if manifest(self.root) != before:
                raise EscapeError(f"the sandbox {self.root} changed during copilot call {n} (log {log})")
        text, tools, out_tok = "", 0, 0
        for line in out.splitlines():
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if ev.get("type") == "assistant.message":
                d = ev.get("data") or {}
                out_tok += int(d.get("outputTokens") or 0)
                tools += len(d.get("toolRequests") or [])
                if d.get("content") and not d.get("toolRequests"):
                    text = d["content"]
                elif d.get("content"):
                    text = d["content"]
        if timed_out:
            raise TimeoutError(f"copilot call {n} hit the {self.deadline}s deadline (log {log})")
        if rc != 0 and not text:
            raise RuntimeError(f"copilot call {n} rc={rc}: {err[-300:]!r} (log {log})")
        return text, {"wall_s": wall, "tool_calls": tools, "output_tokens": out_tok, "log": log, "rc": rc}


class ClaudeModel:
    """The paid leg: `claude -p` with read tools and Bash scoped to the evidence allowlist."""
    SYSTEM = ("You are a careful auditor. Follow the instructions in the user message exactly and output "
              "only what it asks for.")

    def __init__(self, bin_path, model, root, deadline=600, max_budget_usd=1.0):
        self.bin, self.model, self.root = bin_path, model, root
        self.deadline, self.max_budget = deadline, max_budget_usd

    def argv(self):
        allowed = ["Read", "Grep", "Glob"] + [f"Bash({c} *)" for c in ALLOWED_COMMANDS] + \
                  [f"Bash({c})" for c in ("ls",)]
        return [self.bin, "-p", "--model", self.model, "--output-format", "json",
                "--tools", "Bash,Read,Grep,Glob", "--allowedTools", *allowed,
                "--permission-mode", "dontAsk", "--system-prompt", self.SYSTEM, "--setting-sources", "",
                "--strict-mcp-config", "--no-session-persistence",
                "--max-budget-usd", str(self.max_budget)]

    def __call__(self, prompt):
        env = dict(os.environ)
        rc, out, err, wall, timed_out = _run_group(self.argv(), cwd=self.root, env=env, deadline=self.deadline,
                                                   stdin_text=prompt)
        if timed_out:
            raise TimeoutError(f"claude -p hit the {self.deadline}s deadline")
        try:
            r = json.loads(out)
        except ValueError:
            raise RuntimeError(f"claude -p rc={rc}: no JSON on stdout ({out[:200]!r}; {err[-200:]!r})")
        meta = {"wall_s": wall, "cost_usd": float(r.get("total_cost_usd") or 0.0),
                "num_turns": r.get("num_turns"), "usage": r.get("usage"), "rc": rc,
                "is_error": r.get("is_error")}
        if r.get("is_error") and not r.get("result"):
            raise RuntimeError(f"claude -p is_error: {str(r)[:300]}")
        return str(r.get("result") or ""), meta


# --------------------------------------------------------------------------------- sample / report

def select_sample(cards, n, seed, types=ACTIVE_TYPES):
    """n active cards of `types`, stratified by month (largest-remainder allocation), random with `seed`."""
    pool = defaultdict(list)
    for c in cards:
        if c["status"] == "active" and c["type"] in types:
            pool[c["date"][:7] or "unknown"].append(c)
    total = sum(len(v) for v in pool.values())
    if not total:
        return []
    n = min(n, total)
    months = sorted(pool)
    quota = {m: n * len(pool[m]) / total for m in months}
    alloc = {m: int(quota[m]) for m in months}
    rest = n - sum(alloc.values())
    for m in sorted(months, key=lambda m: (-(quota[m] - alloc[m]), m))[:rest]:
        alloc[m] += 1
    rng = random.Random(seed)
    out = []
    for m in months:
        group = sorted(pool[m], key=lambda c: c["id"])
        out += rng.sample(group, alloc[m])
    return out


def wilson(k, n, z=1.96):
    if n <= 0:
        return (0.0, 0.0)
    p = k / n
    den = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return (max(0.0, centre - half), min(1.0, centre + half))


def _first_nlh_evidence(ans):
    for s in (ans or {}).get("subclaims") or []:
        if isinstance(s, dict) and s.get("verdict") == "no-longer-holds":
            return [e for e in s.get("evidence") or [] if isinstance(e, dict)]
    return []


def proposal_lines(rows):
    """One line per card the paid leg found no-longer-holds; flagged SONNET-ONLY when qwen disagrees."""
    out = []
    for r in rows:
        s, q = r.get("sonnet") or {}, r.get("qwen") or {}
        if s.get("verdict") != "no-longer-holds":
            continue
        ans = s.get("answer") or {}
        flag = "" if q.get("verdict") == "no-longer-holds" else " [SONNET-ONLY]"
        ev = "; ".join(f"`{e.get('command')}` -> rc={e.get('exit_code')} {_norm(e.get('output_quote'))[:160]!r}"
                       for e in _first_nlh_evidence(ans)[:3])
        out.append(f"- {r['id']}{flag} path={ans.get('suggested_path')} | sentence: "
                   f"{_norm(ans.get('false_sentence'))[:300]!r} | evidence: {ev}")
    return out


def _median(xs):
    xs = sorted(xs)
    if not xs:
        return 0.0
    m = len(xs) // 2
    return xs[m] if len(xs) % 2 else (xs[m - 1] + xs[m]) / 2


def summarise(rows, population):
    n = len(rows)
    qv = Counter((r.get("qwen") or {}).get("verdict", "missing") for r in rows)
    sv = Counter((r.get("sonnet") or {}).get("verdict", "missing") for r in rows)
    both = [r for r in rows if (r.get("qwen") or {}).get("verdict") in VERDICTS
            and (r.get("sonnet") or {}).get("verdict") in VERDICTS]
    agree_all = sum(1 for r in rows if (r.get("qwen") or {}).get("verdict") == (r.get("sonnet") or {}).get("verdict"))
    agree_both = sum(1 for r in both if r["qwen"]["verdict"] == r["sonnet"]["verdict"])
    nlh_any = [r for r in rows if "no-longer-holds" in ((r.get("qwen") or {}).get("verdict"),
                                                        (r.get("sonnet") or {}).get("verdict"))]
    nlh_both = sum(1 for r in nlh_any if r["qwen"]["verdict"] == r["sonnet"]["verdict"] == "no-longer-holds")
    s_ok = [r for r in rows if (r.get("sonnet") or {}).get("verdict") in VERDICTS]
    k = sum(1 for r in s_ok if r["sonnet"]["verdict"] == "no-longer-holds")
    lo, hi = wilson(k, len(s_ok))
    share = k / len(s_ok) if s_ok else 0.0
    qt = [float((r.get("qwen") or {}).get("wall_s") or 0) for r in rows if r.get("qwen")]
    s_cost = [float((r.get("sonnet") or {}).get("cost_usd") or 0) for r in rows if r.get("sonnet")]
    return {
        "cards": n,
        "qwen_verdicts": dict(qv), "sonnet_verdicts": dict(sv),
        "agreement_all": {"agree": agree_all, "of": n},
        "agreement_both_verified": {"agree": agree_both, "of": len(both)},
        "agreement_no_longer_holds": {"both": nlh_both, "either": len(nlh_any),
                                      "qwen_only": sum(1 for r in nlh_any if r["qwen"]["verdict"] == "no-longer-holds"
                                                       and r["sonnet"]["verdict"] != "no-longer-holds"),
                                      "sonnet_only": sum(1 for r in nlh_any if r["sonnet"]["verdict"] == "no-longer-holds"
                                                         and r["qwen"]["verdict"] != "no-longer-holds")},
        "qwen_wall_s": {"median": round(_median(qt), 1), "mean": round(sum(qt) / len(qt), 1) if qt else 0.0,
                        "max": round(max(qt), 1) if qt else 0.0, "total": round(sum(qt), 1)},
        "qwen_attempts": dict(Counter((r.get("qwen") or {}).get("attempts") for r in rows if r.get("qwen"))),
        "sonnet_attempts": dict(Counter((r.get("sonnet") or {}).get("attempts") for r in rows if r.get("sonnet"))),
        "sonnet_usd": {"total": round(sum(s_cost), 4), "per_card": round(sum(s_cost) / len(s_cost), 4) if s_cost else 0.0},
        "no_longer_holds_share": {"judge": "sonnet", "k": k, "n": len(s_ok), "share": round(share, 4),
                                  "ci95": [round(lo, 4), round(hi, 4)]},
        "extrapolation_estimate": {"population": population, "point": round(share * population),
                                   "ci95": [round(lo * population), round(hi * population)],
                                   "note": "ESTIMATE: sample share x population; assumes the sample "
                                           "represents the population and the paid leg is right"},
    }


def _cell(r, leg):
    x = r.get(leg) or {}
    v = x.get("verdict", "-")
    return f"{v} ({x.get('attempts', '-')}x)"


def write_report(out, rows, mech_by_id, summary):
    lines = ["# Card validity calibration", "",
             f"Generated {datetime.datetime.now().isoformat(timespec='seconds')}. "
             f"{summary['cards']} cards. Nothing was applied to the base.", "",
             "| card | type | date | anchors gone/moved | qwen | sonnet | agree |",
             "|---|---|---|---|---|---|---|"]
    for r in rows:
        an = mech_by_id.get(r["id"], [])
        gm = sum(1 for a in an if a["status"] in ("gone", "moved", "line-gone"))
        agree = "yes" if (r.get("qwen") or {}).get("verdict") == (r.get("sonnet") or {}).get("verdict") else "NO"
        lines.append(f"| {r['id']} | {r.get('type', '')} | {r.get('date', '')} | {gm}/{len(an)} | "
                     f"{_cell(r, 'qwen')} | {_cell(r, 'sonnet')} | {agree} |")
    lines += ["", "## Summary", "", "```json", json.dumps(summary, indent=2, ensure_ascii=False), "```", ""]
    with open(os.path.join(out, "calibration.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    with open(os.path.join(out, "summary.json"), "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2, ensure_ascii=False)
    props = proposal_lines(rows)
    with open(os.path.join(out, "proposals.md"), "w", encoding="utf-8") as fh:
        fh.write("# Card validity proposals -- for the coordinator to approve and apply; NOTHING applied\n\n"
                 "One line per card the paid leg found no-longer-holds after the evidence re-run. "
                 "[SONNET-ONLY] = the local leg did not agree. path = the suggested K-05297 route "
                 "(erratum-wrong-record / dated-pointer / supersede).\n\n")
        fh.write("\n".join(props) + ("\n" if props else "(none)\n"))
    return props


# ------------------------------------------------------------------------------------ checked ledger
#
# One append-only JSON line per judged card. A line names the card AND the sha256 of the card file the
# judge saw, so "already checked" means "checked at this content": a card changed since (an erratum, a
# repair, a status edge) has no line for its current hash and is eligible again. Lines are never rewritten.

def read_ledger(path):
    rows = []
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if isinstance(d, dict) and d.get("id"):
                    rows.append(d)
    return rows


def append_ledger(path, lines, lock=None):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)

    def _write():
        with open(path, "a", encoding="utf-8") as fh:
            for d in lines:
                fh.write(json.dumps(d, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
    if lock is None:
        _write()
    else:
        with lock:
            _write()


def _decisive(ans, verdict):
    subs = [s for s in (ans or {}).get("subclaims") or [] if isinstance(s, dict)]
    for s in subs:
        if s.get("verdict") == verdict:
            return s
    return subs[0] if subs else {}


def ledger_line(card, result, judge, prompt_version, date, value_flag=None, run=None):
    """The ledger line of one judged card (`result` as judge_card returns it)."""
    ans = result.get("answer") or {}
    verdict = result.get("verdict")
    sub = _decisive(ans, verdict)
    ev = [{"command": e.get("command"), "exit_code": e.get("exit_code"),
           "output_quote": _norm(e.get("output_quote"))[:200]}
          for e in (sub.get("evidence") or [])[:3] if isinstance(e, dict)]
    reason = sub.get("reason") or ""
    if verdict == "unverified":
        errs = (result.get("errors") or [{}])[-1].get("errors") or []
        reason = "no verified answer: " + (errs[0] if errs else "?")
    return {"id": card["id"], "sha256": card["sha256"], "date": date, "judge": judge,
            "prompt_version": prompt_version, "verdict": verdict, "knowhow": ans.get("knowhow"),
            "knowhow_reason": _norm(ans.get("knowhow_reason") or "")[:300] or None,
            "reason": _norm(reason)[:300], "evidence": ev,
            "false_sentence": ans.get("false_sentence") or None,
            "suggested_path": ans.get("suggested_path") or None,
            "value_flag": value_flag, "cost_usd": result.get("cost_usd"), "run": run}


def eligible_cards(cards, ledger_rows, statuses=DRAW_STATUSES):
    """Cards of `statuses` whose CURRENT file hash has no ledger line."""
    seen = {(r.get("id"), r.get("sha256")) for r in ledger_rows}
    return [c for c in cards if c["status"] in statuses and (c["id"], c.get("sha256")) not in seen]


def select_uniform(cards, n, seed):
    """n cards drawn uniformly at random with `seed` (every card the same chance -- no strata)."""
    pool = sorted(cards, key=lambda c: c["id"])
    return random.Random(seed).sample(pool, min(n, len(pool)))


def backfill(ledger_path, calib_path, cards, date, judge="sonnet", prompt_version="v1-calib"):
    """Ledger lines for the cards a calibration run already judged, so a draw does not repeat them. The
    hash is the card's hash NOW (the calibration did not record one). Idempotent: a card that already has
    a `prompt_version` line is skipped. Returns the number of lines appended."""
    have = {r["id"] for r in read_ledger(ledger_path) if r.get("prompt_version") == prompt_version}
    by_id = {c["id"]: c for c in cards}
    lines = []
    for cid, row in read_jsonl(calib_path).items():
        if cid in have or cid not in by_id:
            continue
        lines.append(ledger_line(by_id[cid], row, judge, prompt_version, date,
                                 run=f"backfill from {os.path.basename(calib_path)}; sha256 taken at backfill"))
        have.add(cid)
    if lines:
        append_ledger(ledger_path, lines)
    return len(lines)


def judge_many(cards, model, root, mech, workers, max_usd, on_result, max_retries=2, deadline_s=None,
               spent=0.0):
    """Judge `cards` with at most `workers` calls in flight; stop SUBMITTING once the spend passes
    `max_usd` (calls in flight finish). on_result(card, result) runs in the caller's thread.
    Returns ({id: result}, stopped_at_cap)."""
    done, stopped = {}, False
    todo = list(cards)
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futs = {}
        while todo or futs:
            while todo and len(futs) < max(1, workers) and not stopped:
                if spent > max_usd:
                    stopped = True
                    break
                c = todo.pop(0)
                futs[ex.submit(judge_card, c, model, root, mech.get(c["id"], []), max_retries,
                               deadline_s)] = c
            if stopped and todo:
                todo = []
            if not futs:
                break
            fin, _ = concurrent.futures.wait(futs, return_when=concurrent.futures.FIRST_COMPLETED)
            for f in fin:
                c = futs.pop(f)
                r = f.result()
                spent += float(r.get("cost_usd") or 0)
                done[c["id"]] = r
                on_result(c, r)
        if spent > max_usd and todo:
            stopped = True
    return done, stopped


def verdict_line(results):
    v = Counter(r.get("verdict") for r in results)
    kn = sum(1 for r in results if (r.get("answer") or {}).get("knowhow") == "no")
    cost = sum(float(r.get("cost_usd") or 0) for r in results)
    return (f"VERDICTS holds={v['holds']} no-longer-holds={v['no-longer-holds']} "
            f"cannot-check={v['cannot-check-offline']} unverified={v['unverified']} knowhow-no={kn} "
            f"cost=${cost:.4f}")


# ---------------------------------------------------------------------------- proposals (K-05297)

_FLAG_SOURCE = re.compile(r"([A-Za-z_][\w.]*) — (?:card says .*?, repo says .*?\(([^():\s]+):\d+\)|"
                          r"value lives in ([^():\s]+):\d+)")


def _retire_label():
    try:
        import khms_messages
        return khms_messages.msg("VALIDITY_RETIRE_CANDIDATE")
    except Exception:  # noqa: BLE001 -- wording is optional, the route is not
        return "retire candidate (a retired status is not designed yet)"


def _pointer_source(value_flag, evidence, repo_trees=()):
    """'<key> in <file>' for a dated-pointer erratum: the repo-value flag's hit first, else the pattern and
    the file of the first grep evidence (a sandbox path loses its leading git-tree name: the erratum names
    the file as the repository has it)."""
    m = _FLAG_SOURCE.search(value_flag or "")
    if m:
        return f"{m.group(1)} in {m.group(2) or m.group(3)}"
    strip = lambda f: next((f[len(t) + 1:] for t in repo_trees if f.startswith(t + "/")), f)
    for e in evidence:
        try:
            argv = evidence_argv(e.get("command") or "")
        except ValueError:
            continue
        if argv[0] == "grep":
            rest = [t for t in argv[1:] if not t.startswith("-")]
            if len(rest) >= 2:
                return f"{rest[0]} in {strip(rest[-1])}"
    return None


def verbatim_sentence(body, sentence):
    """The card's own wording of `sentence` (the judge may drop backticks and asterisks -- check_answer
    compares without them; an erratum needs the card's characters), whitespace-collapsed; else `sentence`."""
    toks = _prose(sentence).split()
    if not body or not toks:
        return _norm(sentence)
    tok = lambda t: "[`*]*" + "".join(re.escape(ch) + "[`*]*" for ch in t)
    m = re.search(r"\s+".join(tok(t) for t in toks), body)
    return _norm(m.group(0)) if m else _norm(sentence)


def proposal_row(card_id, result, value_flag=None, origin=None, body=None, repo_trees=()):
    """One proposal for a card whose verdict is not "holds" or whose knowhow is "no"; None otherwise.
    {id, verdict, knowhow, route, sentence, evidence, absence_only, command, origin, value_flag}. `body`
    (the card text) recovers the sentence verbatim. Nothing is run."""
    ans = result.get("answer") or {}
    verdict, knowhow = result.get("verdict"), ans.get("knowhow")
    if verdict == "holds" and knowhow != "no":
        return None
    sub = _decisive(ans, verdict)
    evs = [e for e in sub.get("evidence") or [] if isinstance(e, dict)]
    ev_text = "; ".join(f"`{e.get('command')}` -> rc={e.get('exit_code')} {_norm(e.get('output_quote'))[:160]!r}"
                        for e in evs[:3])
    sentence = verbatim_sentence(body, ans.get("false_sentence") or "")
    path = ans.get("suggested_path") or "none"
    q = shlex.quote
    evidence_arg = f"{ev_text} ({origin or 'card-validity'})"
    row = {"id": card_id, "verdict": verdict, "knowhow": knowhow, "sentence": sentence, "evidence": ev_text,
           "origin": origin, "value_flag": value_flag, "reason": _norm(sub.get("reason") or ""),
           # a verdict resting only on "not found" (grep rc=1, ls rc=2) needs a broad re-search first
           "absence_only": bool(evs) and all(e.get("exit_code") not in (0, None) for e in evs)}
    if knowhow == "no":
        row["route"] = "retire-candidate"
        note = f" -- validity: {verdict}" + (f", would be {path}" if verdict == "no-longer-holds" else "")
        row["command"] = f"{_retire_label()}{note}; reason: {_norm(ans.get('knowhow_reason') or '')[:200]}"
    elif verdict == "no-longer-holds" and path == "erratum-wrong-record":
        row["route"] = "erratum wrong-record"
        row["command"] = (f"tools/khms_erratum.py declare --card {card_id} --was {q(sentence)} "
                          f"--evidence {q(evidence_arg)} --by coordinator --kind wrong-record")
    elif verdict == "no-longer-holds" and path == "dated-pointer":
        src = _pointer_source(value_flag, evs, repo_trees) or "<key> in <repo file>"
        row["route"] = "erratum dated-pointer"
        row["command"] = (f"tools/khms_erratum.py declare --card {card_id} --was {q(sentence)} "
                          f"--evidence {q(evidence_arg)} --by coordinator --kind dated-pointer --source {q(src)}")
    elif verdict == "no-longer-holds":
        row["route"] = "supersede"
        today = "; ".join(f"`{e.get('command')}` -> {_norm(e.get('output_quote'))[:160]!r}" for e in evs[:2])
        row["command"] = (f"DRAFT new card (through approve_inbox.py): <what holds today, from: {today}> -- "
                          f"SUPERSEDES {card_id} BECAUSE: {_norm(sub.get('reason') or '')[:240]} "
                          f"(the card says: {sentence[:200]!r})")
    elif verdict == "cannot-check-offline":
        row["route"] = "none"
        row["command"] = "keep -- no file can show it (cannot-check-offline); nothing to correct"
    else:
        row["route"] = "re-judge"
        row["command"] = f"keep; the judge gave no verified answer -- check by hand ({row['reason'][:200]})"
    return row


def dry_run_errata(rows, erratum_argv=None, env=None):
    """Run every erratum row's command with --dry-run (declare writes nothing then: no tombstone id, no
    ledger, no card) and keep its last line in row["dry_run"] -- a reviewer sees which ones the rails
    (verbatim match, R1 lint, the classifier) would refuse before deciding."""
    base = erratum_argv or [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                          "khms_erratum.py")]
    for r in rows:
        if not r["command"].startswith("tools/khms_erratum.py "):
            continue
        argv = base + shlex.split(r["command"])[1:] + ["--dry-run"]
        cp = subprocess.run(argv, capture_output=True, text=True, env=env, timeout=120)
        out = [x for x in ((cp.stdout or "") + (cp.stderr or "")).splitlines() if x.strip()]
        r["dry_run"] = f"rc={cp.returncode}: " + (out[-1] if out else "(no output)")
    return rows


def _md_cell(text):
    return str(text if text is not None else "").replace("|", "\\|").replace("\n", " ")


def proposal_table(rows):
    """A table (one row per card) and, under it, each card's command or draft verbatim in a code block."""
    lines = ["| card | origin | verdict | knowhow | value flag | route | evidence | sentence / reason |",
             "|---|---|---|---|---|---|---|---|"]
    for r in rows:
        flag = "yes" if r.get("value_flag") else "no"
        kind = "absence-only" if r.get("absence_only") else ("positive" if r.get("evidence") else "none")
        what = r["sentence"] or r.get("reason") or ""
        lines.append(f"| {r['id']} | {r.get('origin') or ''} | {r['verdict']} | {r.get('knowhow') or 'n/a'} | "
                     f"{flag} | {r['route']} | {kind} | {_md_cell(what[:300])} |")
    lines += ["", "## Commands and drafts", ""]
    for r in rows:
        lines += [f"### {r['id']} -- {r['route']}", ""]
        if r.get("value_flag"):
            lines += [f"value flag: {r['value_flag']}", ""]
        if r.get("evidence"):
            lines += [f"evidence: {r['evidence']}" + ("  (ABSENCE-ONLY: search broadly before acting)"
                                                       if r.get("absence_only") else ""), ""]
        lines += ["```sh" if r["command"].startswith("tools/") else "```text", r["command"], "```", ""]
        if r.get("dry_run"):
            lines += [f"dry-run: {r['dry_run']}", ""]
    return lines


def value_flags(cards, index=None):
    """{id: flag message or None} from the approval repo-value flag (khms_lint.check_repo_values) -- the
    mechanical rule, no model."""
    import khms_lint
    if index is None:
        import khms_repo_values
        index = khms_repo_values.load_index()
    return {c["id"]: khms_lint.check_repo_values(c["id"], c.get("meta") or {}, c["body"], index) for c in cards}


# ------------------------------------------------------------------------------------------- CLI

def read_jsonl(path):
    out = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                out[d["id"]] = d
    return out


def _append(path, obj, lock):
    with lock:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(obj, ensure_ascii=False) + "\n")


def cmd_sandbox(a):
    out = os.path.abspath(a.out)
    if os.path.exists(out):
        print(f"sandbox: {out} exists; remove it first (chmod -R u+w, then rm -rf)", file=sys.stderr)
        return 2
    os.makedirs(out)
    desc = {}
    for spec in a.tree:
        name, _, src = spec.partition("=")
        kind, _, loc = src.partition(":")
        dest = os.path.join(out, name)
        if kind == "git":
            repo, _, rev = loc.rpartition("@")
            sha = subprocess.run(["git", "-C", repo, "rev-parse", "--short", rev], check=True,
                                 capture_output=True, text=True).stdout.strip()
            os.makedirs(dest)
            p = subprocess.Popen(["git", "-C", repo, "archive", "--format=tar", rev], stdout=subprocess.PIPE)
            with tarfile.open(fileobj=p.stdout, mode="r|") as tf:
                tf.extractall(dest, filter="tar") if hasattr(tarfile, "data_filter") else tf.extractall(dest)
            if p.wait() != 0:
                raise SystemExit(f"git archive failed for {repo}@{rev}")
            desc[name] = {"kind": "git", "repo": repo, "rev": rev, "sha": sha,
                          "description": f"read-only copy of a git repository at {rev} ({sha})"}
        elif kind == "dir":
            shutil.copytree(loc, dest, symlinks=True,
                            ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.pyc"))
            desc[name] = {"kind": "dir", "path": loc, "description": "read-only copy of an operator tools directory"
                          if name == "tools" else "read-only copy of a directory"}
        else:
            print(f"sandbox: bad --tree {spec!r} (NAME=git:REPO@REV or NAME=dir:PATH)", file=sys.stderr)
            return 2
    with open(os.path.join(out, ".trees.json"), "w", encoding="utf-8") as fh:
        json.dump(desc, fh, indent=2)
    for dirpath, dirnames, filenames in os.walk(out, topdown=False):
        for f in filenames:
            p = os.path.join(dirpath, f)
            if not os.path.islink(p):
                os.chmod(p, os.stat(p).st_mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
        os.chmod(dirpath, os.stat(dirpath).st_mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
    n = sum(len(f) for _d, _s, f in os.walk(out))
    print(f"sandbox: {out} with {len(desc)} tree(s), {n} files, read-only")
    return 0


def _index_for(sandbox):
    try:
        with open(os.path.join(sandbox, ".trees.json"), encoding="utf-8") as fh:
            desc = json.load(fh)
    except (OSError, ValueError):
        desc = {}
    trees = {d: os.path.join(sandbox, d) for d in sorted(os.listdir(sandbox))
             if os.path.isdir(os.path.join(sandbox, d)) and not d.startswith(".")}
    repos = [v["repo"] for v in desc.values() if v.get("kind") == "git"]
    return TreeIndex(trees, git_repos=repos)


def run_mechanical(know, sandbox, out_path=None):
    t0 = time.time()
    cards = load_cards(know)
    idx = _index_for(sandbox)
    t_idx = time.time() - t0
    rows, summary = mechanical_pass(cards, idx)
    idx.idents()
    summary["index_s"] = round(t_idx, 2)
    summary["total_s"] = round(time.time() - t0, 2)
    typed = [r for r in rows if r["type"] in ACTIVE_TYPES]
    summary["typed_cards"] = len(typed)
    summary["typed_cards_with_gone_or_moved"] = sum(
        1 for r in typed if any(x["status"] in ("gone", "moved", "line-gone") for x in r["anchors"]))
    if out_path:
        with open(out_path, "w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    return cards, rows, summary


def cmd_mechanical(a):
    _cards, _rows, summary = run_mechanical(a.know, a.sandbox, a.out)
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


def cmd_calibrate(a):
    os.makedirs(a.out, exist_ok=True)
    sandbox = os.path.abspath(a.sandbox)
    cards, mrows, msum = run_mechanical(a.know, sandbox, os.path.join(a.out, "mechanical.jsonl"))
    with open(os.path.join(a.out, "mechanical-summary.json"), "w", encoding="utf-8") as fh:
        json.dump(msum, fh, indent=2)
    print(f"mechanical: {json.dumps(msum)}", flush=True)
    mech = {r["id"]: r["anchors"] for r in mrows}
    sample = select_sample(cards, a.n, a.seed)
    with open(os.path.join(a.out, "sample.json"), "w", encoding="utf-8") as fh:
        json.dump([{"id": c["id"], "type": c["type"], "date": c["date"]} for c in sample], fh, indent=1)
    print(f"sample: {len(sample)} cards (seed {a.seed})", flush=True)
    lock = threading.Lock()

    def leg(name, model, workers):
        path = os.path.join(a.out, f"{name}.jsonl")
        done = read_jsonl(path)
        todo = [c for c in sample if c["id"] not in done]

        def one(c):
            r = judge_card(c, model, sandbox, mech.get(c["id"], []), max_retries=a.max_retries,
                           deadline_s=a.card_deadline)
            r["id"] = c["id"]
            _append(path, r, lock)
            print(f"{name} {c['id']} -> {r['verdict']} ({r['attempts']}x, {r['wall_s']}s, ${r['cost_usd']})",
                  flush=True)
            return r

        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
            for f in [ex.submit(one, c) for c in todo]:
                f.result()

    legs = []
    if a.qwen:
        legs.append(("qwen", CopilotModel(a.copilot_bin, a.copilot_url, a.llm_model, sandbox,
                                          os.path.join(os.path.abspath(a.workdir), "qwen"),
                                          deadline=a.qwen_deadline), 1))
    if a.sonnet:
        legs.append(("sonnet", ClaudeModel(a.claude_bin, a.sonnet_model, sandbox, deadline=a.sonnet_deadline,
                                           max_budget_usd=a.sonnet_max_usd), a.sonnet_workers))
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(legs))) as ex:
        futs = [ex.submit(leg, *x) for x in legs]
        for f in futs:
            f.result()
    return cmd_report(a)


def cmd_report(a):
    sample = json.load(open(os.path.join(a.out, "sample.json"), encoding="utf-8"))
    q = read_jsonl(os.path.join(a.out, "qwen.jsonl"))
    s = read_jsonl(os.path.join(a.out, "sonnet.jsonl"))
    mech = {r["id"]: r["anchors"] for r in read_jsonl(os.path.join(a.out, "mechanical.jsonl")).values()}
    rows = [dict(c, qwen=q.get(c["id"]), sonnet=s.get(c["id"])) for c in sample]
    summary = summarise(rows, a.population)
    try:
        summary["mechanical"] = json.load(open(os.path.join(a.out, "mechanical-summary.json"), encoding="utf-8"))
    except (OSError, ValueError):
        pass
    props = write_report(a.out, rows, mech, summary)
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    print(f"proposals: {len(props)} line(s) -> {os.path.join(a.out, 'proposals.md')}")
    return 0


def _ledger_path(a):
    return a.ledger or os.path.join(os.path.dirname(os.path.abspath(a.know)), "ledger", "validity.jsonl")


def _statuses(a):
    return tuple(s.strip() for s in a.statuses.split(",") if s.strip())


def cmd_backfill(a):
    n = backfill(_ledger_path(a), a.calib, load_cards(a.know), date=a.date)
    print(f"BACKFILL {n} line(s) -> {_ledger_path(a)}")
    return 0


def cmd_sample(a):
    """Draw without judging: the eligible pool after the ledger, and the overlap with an earlier draw."""
    cards = load_cards(a.know)
    pool = eligible_cards(cards, read_ledger(_ledger_path(a)), _statuses(a))
    drawn = select_uniform(pool, a.n, a.seed)
    print(f"ELIGIBLE {len(pool)} of {sum(1 for c in cards if c['status'] in _statuses(a))} "
          f"({','.join(_statuses(a))})")
    print(f"SAMPLED {len(drawn)} seed={a.seed}")
    if a.against:
        with open(a.against, encoding="utf-8") as fh:
            before = set(json.load(fh)["ids"])
        print(f"OVERLAP {len({c['id'] for c in drawn} & before)} with {a.against}")
    if a.ids:
        print(" ".join(c["id"] for c in drawn))
    return 0


def cmd_run(a):
    """Draw n cards past the ledger, mechanical pass, value flag, judge (paid leg), ledger + report."""
    os.makedirs(a.out, exist_ok=True)
    sandbox = os.path.abspath(a.sandbox)
    ledger = _ledger_path(a)
    statuses = _statuses(a)
    cards = load_cards(a.know)
    by_id = {c["id"]: c for c in cards}
    sample_path = os.path.join(a.out, "sample.json")
    if os.path.exists(sample_path):                    # a resumed run keeps its draw
        with open(sample_path, encoding="utf-8") as fh:
            meta = json.load(fh)
        if meta.get("seed") != a.seed:
            print(f"run: {sample_path} was drawn with seed {meta.get('seed')}, not {a.seed}", file=sys.stderr)
            return 2
        sample = [by_id[i] for i in meta["ids"] if i in by_id]
        print(f"RESUMED {len(sample)} seed={a.seed}", flush=True)
    else:
        pool = eligible_cards(cards, read_ledger(ledger), statuses)
        sample = select_uniform(pool, a.n, a.seed)
        meta = {"seed": a.seed, "n": a.n, "statuses": list(statuses), "eligible": len(pool),
                "population": sum(1 for c in cards if c["status"] in statuses),
                "drawn_at": datetime.datetime.now().isoformat(timespec="seconds"),
                "ids": [c["id"] for c in sample],
                "cards": [{"id": c["id"], "type": c["type"], "status": c["status"], "date": c["date"],
                           "sha256": c["sha256"]} for c in sample]}
        with open(sample_path, "w", encoding="utf-8") as fh:
            json.dump(meta, fh, indent=1)
    print(f"SAMPLED {len(sample)} seed={a.seed}", flush=True)
    idx = _index_for(sandbox)
    mrows, msum = mechanical_pass(cards, idx, statuses=statuses)
    with open(os.path.join(a.out, "mechanical.jsonl"), "w", encoding="utf-8") as fh:
        for r in mrows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    with open(os.path.join(a.out, "mechanical-summary.json"), "w", encoding="utf-8") as fh:
        json.dump(msum, fh, indent=2)
    print(f"mechanical: {json.dumps(msum)}", flush=True)
    mech = {r["id"]: r["anchors"] for r in mrows}
    flags_path = os.path.join(a.out, "value-flags.json")
    if os.path.exists(flags_path):
        with open(flags_path, encoding="utf-8") as fh:
            flags = json.load(fh)
    else:
        flags = value_flags(sample)
        with open(flags_path, "w", encoding="utf-8") as fh:
            json.dump(flags, fh, indent=1, ensure_ascii=False)
    print(f"value flag: {sum(1 for v in flags.values() if v)} of {len(sample)} sampled cards", flush=True)
    res_path = os.path.join(a.out, "sonnet.jsonl")
    done = read_jsonl(res_path)
    spent = sum(float(r.get("cost_usd") or 0) for r in done.values())
    todo = [c for c in sample if c["id"] not in done]
    model = ClaudeModel(a.claude_bin, a.sonnet_model, sandbox, deadline=a.sonnet_deadline,
                        max_budget_usd=a.sonnet_max_usd)
    run_tag = f"card-validity {PROMPT_VERSION} seed={a.seed} {os.path.basename(os.path.abspath(a.out))}"
    today = datetime.date.today().isoformat()
    total = [spent]

    def on_result(c, r):
        r = dict(r, id=c["id"], sha256=c["sha256"])
        append_ledger(res_path, [r])
        append_ledger(ledger, [ledger_line(c, r, f"claude -p --model {a.sonnet_model}", PROMPT_VERSION, today,
                                           value_flag=flags.get(c["id"]), run=run_tag)])
        total[0] += float(r.get("cost_usd") or 0)
        kh = (r.get("answer") or {}).get("knowhow")
        print(f"sonnet {c['id']} -> {r['verdict']} knowhow={kh} ({r['attempts']}x, {r['wall_s']}s, "
              f"${r['cost_usd']}) total=${total[0]:.4f}", flush=True)

    _done, stopped = judge_many(todo, model, sandbox, mech, min(a.workers, 4), a.max_usd, on_result,
                                max_retries=a.max_retries, deadline_s=a.card_deadline, spent=spent)
    results = list(read_jsonl(res_path).values())
    line = verdict_line(results)
    if stopped:
        print(f"STOPPED at the ${a.max_usd} cap after {len(results)} of {len(sample)} cards", flush=True)
    print(line, flush=True)
    with open(os.path.join(a.out, "summary.json"), "w", encoding="utf-8") as fh:
        json.dump({"verdicts": line, "judged": len(results), "sampled": len(sample), "stopped_at_cap": stopped,
                   "seed": a.seed, "prompt_version": PROMPT_VERSION, "ledger": ledger}, fh, indent=2)
    return 3 if stopped else 0


def cmd_proposals(a):
    """proposals.md from one or more result files (jsonl of judge_card results with `id`). Writes only --out."""
    cards = {c["id"]: c for c in load_cards(a.know)}
    rows, results = [], []
    repo_trees = ()
    if a.sandbox:
        with open(os.path.join(a.sandbox, ".trees.json"), encoding="utf-8") as fh:
            repo_trees = tuple(n for n, d in json.load(fh).items() if d.get("kind") == "git")
    for spec in a.results:
        label, _, path = spec.partition("=")
        res = read_jsonl(path)
        flags_file = os.path.join(os.path.dirname(path), "value-flags.json")
        flags = {}
        if os.path.exists(flags_file):
            with open(flags_file, encoding="utf-8") as fh:
                flags = json.load(fh)
        missing = [c for i, c in cards.items() if i in res and i not in flags]
        if missing:
            flags.update(value_flags(missing))
        if label == a.summary_of:
            results = list(res.values())
        for cid, r in res.items():
            keep = [v for lab, _, v in (x.partition(":") for x in a.only_verdict or []) if lab == label]
            if keep and r.get("verdict") not in keep:
                continue
            row = proposal_row(cid, r, value_flag=flags.get(cid), origin=label,
                               body=(cards.get(cid) or {}).get("body"), repo_trees=repo_trees)
            if row:
                rows.append(row)
    lines = ["# Card validity proposals -- for the coordinator to review and apply; NOTHING applied", "",
             "One row per card whose verdict is not `holds` or whose `knowhow` is `no`. Routes follow the three "
             "kinds of untrue: wrong record -> erratum wrong-record; a moved repo value -> erratum dated-pointer; "
             "an overtaken mechanism -> a new superseding card (draft) or a status edge; not know-how -> retire "
             "candidate. Commands are printed, never run; a `--dry-run` first is the reviewer's choice.", ""]
    if results:
        lines += [verdict_line(results), ""]
    if a.dry_run_errata:
        dry_run_errata(rows)
    lines += proposal_table(rows)
    with open(a.out, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    print(f"PROPOSALS {len(rows)} row(s) -> {a.out}")
    if results:
        print(verdict_line(results))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sb = sub.add_parser("sandbox")
    sb.add_argument("--out", required=True)
    sb.add_argument("--tree", action="append", required=True)
    me = sub.add_parser("mechanical")
    me.add_argument("--know", required=True)
    me.add_argument("--sandbox", required=True)
    me.add_argument("--out")
    for p in (sub.add_parser("calibrate"), sub.add_parser("report")):
        p.add_argument("--out", required=True)
        p.add_argument("--population", type=int, default=0, help="active cards of the checked types (0 = count)")
        if p.prog.endswith("calibrate"):
            p.add_argument("--know", required=True)
            p.add_argument("--sandbox", required=True)
            p.add_argument("--workdir", required=True, help="harness scratch (HOME, logs), outside the base")
            p.add_argument("--n", type=int, default=50)
            p.add_argument("--seed", type=int, default=219)
            p.add_argument("--max-retries", type=int, default=2)
            p.add_argument("--card-deadline", type=float, default=1200)
            p.add_argument("--qwen", action=argparse.BooleanOptionalAction, default=True)
            p.add_argument("--sonnet", action=argparse.BooleanOptionalAction, default=True)
            p.add_argument("--copilot-bin", default=os.environ.get("KHMS_COPILOT_BIN", "copilot"))
            p.add_argument("--copilot-url", default=os.environ.get("KHMS_COPILOT_URL", ""))
            p.add_argument("--llm-model", default=os.environ.get("KHMS_LLM_MODEL", "qwen3-coder-next:latest"))
            p.add_argument("--qwen-deadline", type=float, default=420)
            p.add_argument("--claude-bin", default=os.environ.get("KHMS_AGENT", "claude"))
            p.add_argument("--sonnet-model", default="sonnet")
            p.add_argument("--sonnet-deadline", type=float, default=600)
            p.add_argument("--sonnet-max-usd", type=float, default=1.0)
            p.add_argument("--sonnet-workers", type=int, default=3)
    bf = sub.add_parser("backfill", help="ledger lines for an earlier calibration's verdicts (idempotent)")
    bf.add_argument("--know", required=True)
    bf.add_argument("--ledger", help="default: <know>/../ledger/validity.jsonl")
    bf.add_argument("--calib", required=True, help="the calibration's sonnet.jsonl")
    bf.add_argument("--date", required=True, help="the calibration's date (ISO)")
    sa = sub.add_parser("sample", help="draw past the ledger without judging")
    ru = sub.add_parser("run", help="draw past the ledger, mechanical pass, value flag, paid judge, ledger")
    for p in (sa, ru):
        p.add_argument("--know", required=True)
        p.add_argument("--ledger", help="default: <know>/../ledger/validity.jsonl")
        p.add_argument("--n", type=int, default=100)
        p.add_argument("--seed", type=int, required=True)
        p.add_argument("--statuses", default=",".join(DRAW_STATUSES))
    sa.add_argument("--against", help="an earlier run's sample.json: print the overlap")
    sa.add_argument("--ids", action="store_true")
    ru.add_argument("--sandbox", required=True)
    ru.add_argument("--out", required=True)
    ru.add_argument("--workers", type=int, default=4, choices=range(1, 5))
    ru.add_argument("--max-usd", type=float, default=15.0, help="stop submitting once the spend passes it")
    ru.add_argument("--max-retries", type=int, default=2)
    ru.add_argument("--card-deadline", type=float, default=1200)
    ru.add_argument("--claude-bin", default=os.environ.get("KHMS_AGENT", "claude"))
    ru.add_argument("--sonnet-model", default="sonnet")
    ru.add_argument("--sonnet-deadline", type=float, default=600)
    ru.add_argument("--sonnet-max-usd", type=float, default=1.0)
    pr = sub.add_parser("proposals", help="proposal rows (K-05297 routes + commands) from result files")
    pr.add_argument("--know", required=True)
    pr.add_argument("--out", required=True)
    pr.add_argument("--results", action="append", required=True, help="LABEL=path/to/results.jsonl")
    pr.add_argument("--sandbox", help="the run's sandbox: its git trees' names are stripped from --source files")
    pr.add_argument("--dry-run-errata", action="store_true",
                    help="run each erratum command with --dry-run and show its verdict line")
    pr.add_argument("--summary-of", help="the LABEL whose results the VERDICTS line counts")
    pr.add_argument("--only-verdict", action="append", help="LABEL:VERDICT -- keep only that verdict of that "
                    "file, e.g. calib:no-longer-holds")
    a = ap.parse_args(argv)
    if a.cmd in ("calibrate", "report") and not a.population and getattr(a, "know", None):
        a.population = sum(1 for c in load_cards(a.know) if c["status"] == "active" and c["type"] in ACTIVE_TYPES)
    if a.cmd == "calibrate" and a.qwen and not a.copilot_url:
        ap.error("the qwen leg needs --copilot-url or KHMS_COPILOT_URL (an OpenAI-compatible base URL)")
    return {"sandbox": cmd_sandbox, "mechanical": cmd_mechanical, "calibrate": cmd_calibrate,
            "report": cmd_report, "backfill": cmd_backfill, "sample": cmd_sample, "run": cmd_run,
            "proposals": cmd_proposals}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
