#!/usr/bin/env python3
"""Deterministic transcript digest for KHMS distillation. Zero LLM tokens.

Reads Claude Code JSONL transcripts (plain or .gz), keeps: user/assistant message
text, tool names + error status, error lines. Drops tool outputs and thinking.
Usage: preprocess_transcripts.py <file-or-glob>... [> digest.txt]

SUBAGENT RUNS (H4 of docs/design/team-rules.md, issue #110, 2026-09-22).
A subagent transcript `<session>/subagents/agent-<id>.jsonl` is headed
`===== SESSION <session>/agent-<id> =====` followed by one line

    AGENT-RUN parent=<session> agent=<id> Issue: #N Role: <role · model · effort> via=<where> [type=<agentType>]

so the sweep can say whose work a card came from.  `Issue:` and `Role:` come
from the first five lines of the BRIEF, looked for in this order:
  1. the first user message (the brief pasted as the prompt)            via=first-message
  2. the tool result of reading a file the first message names          via=brief:<file>
     (the prompt is "read and execute the brief <path>"; measured 2026-09-22
     over 113 recent subagent runs: 22 had the header in the first message,
     60 only in the brief they were told to read)
  3. an earlier part of the same run — a `/clear` in the parent splits a
     subagent's transcript across two session directories              via=part:<session>
Neither found -> `Issue: unattributed` / `Role: unattributed`, each on its own.
The header is read from the whole file, never cut by --since: the brief is the
oldest message of a run that is still going at night.

THE 2000-CHARACTER CUT (step (E) d) does not apply to the agent's LAST message
— its final report, whose tail carries the open items and the MARKs — nor to
any line that starts with `MARK[`: those survive a cut whole.
"""
import gzip
import json
import glob
import os
import re
import sys

CLIP = 2000
HEADER_LINES = 5
UNATTRIBUTED = "unattributed"

# `     1\tIssue: #110` (Read, cat -n) or `1→Issue: #110` (older Read) -> `Issue: #110`
_LINENO = re.compile(r"^\s*\d+(?:\t|→)")
_ISSUE = re.compile(r"^\s*Issue:\s*#(\d+)\b")
_ROLE = re.compile(r"^\s*Role:\s*(\S.*?)\s*$")
_PATH = re.compile(r"(/[^\s`'\"()<>]+\.md)")


def open_any(path):
    return gzip.open(path, "rt", encoding="utf-8", errors="replace") if path.endswith(".gz") \
        else open(path, encoding="utf-8", errors="replace")


def _records(path):
    with open_any(path) as f:
        for line in f:
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                yield obj


def _text_of(content):
    """Message or tool-result content -> plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text") or "" for b in content
                         if isinstance(b, dict) and b.get("type") == "text")
    return ""


def header_of(text):
    """(issue, role) from the first HEADER_LINES lines of a brief; None each
    when absent.  An `Issue:` line without a number is no issue."""
    issue = role = None
    for ln in (text or "").splitlines()[:HEADER_LINES]:
        ln = _LINENO.sub("", ln, count=1)
        m = _ISSUE.match(ln)
        if m and issue is None:
            issue = m.group(1)
        m = _ROLE.match(ln)
        if m and role is None:
            role = m.group(1)
    return issue, role


def agent_layout(path):
    """`…/<session>/subagents/[<x>/]agent-<id>.jsonl[.gz]` -> (session, id);
    anything else -> None."""
    parts = os.path.abspath(path).split(os.sep)
    base = parts[-1]
    if not base.startswith("agent-") or "subagents" not in parts[:-1]:
        return None
    i = len(parts) - 2 - parts[-2::-1].index("subagents")
    if i < 1:
        return None
    return parts[i - 1], base[len("agent-"):].split(".")[0]


def scan_file(path):
    """One pass: -> (issue, role, via, last_text) where last_text is the
    (record index, block index) of the last non-empty assistant text."""
    first = None
    briefs = []
    wanted = {}                   # tool_use id -> brief path it reads
    found = (None, None, None)
    last = None
    for n, obj in enumerate(_records(path)):
        t = obj.get("type")
        msg = obj.get("message") or {}
        content = msg.get("content")
        if t == "user" and first is None:
            first = _text_of(content)
            issue, role = header_of(first)
            if issue or role:
                found = (issue, role, "first-message")
            briefs = _PATH.findall(first)
            continue
        if t == "assistant" and isinstance(content, list):
            for k, blk in enumerate(content):
                if not isinstance(blk, dict):
                    continue
                if blk.get("type") == "text" and (blk.get("text") or "").strip():
                    last = (n, k)
                elif blk.get("type") == "tool_use" and briefs and found[2] is None:
                    inp = blk.get("input") or {}
                    arg = str(inp.get("file_path") or inp.get("command") or "")
                    for b in briefs:
                        if (inp.get("file_path") == b) or (
                                blk.get("name") == "Bash" and b in arg):
                            wanted[blk.get("id")] = b
                            break
        elif t == "user" and isinstance(content, list) and wanted and found[2] is None:
            for blk in content:
                if isinstance(blk, dict) and blk.get("type") == "tool_result" \
                        and blk.get("tool_use_id") in wanted:
                    issue, role = header_of(_text_of(blk.get("content")))
                    if issue or role:
                        found = (issue, role, "brief:" + os.path.basename(
                            wanted[blk.get("tool_use_id")]))
    return found[0], found[1], found[2], last


def _other_parts(path, agent_id):
    """Earlier parts of the same run, oldest first — tok_account.run_parts is
    the one implementation of part discovery."""
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    try:
        import tok_account
    except Exception:
        return []
    me = os.path.realpath(path)
    return [p for p in tok_account.run_parts(me) if p != me]


def attribution(path, layout, scan):
    session, agent_id = layout
    issue, role, via, _ = scan
    if via is None and path.endswith(".jsonl"):
        for p in _other_parts(path, agent_id):
            i2, r2, v2, _ = scan_file(p)
            if v2 is not None:
                lay = agent_layout(p)
                issue, role, via = i2, r2, "part:" + (lay[0] if lay else "?")
                break
    line = "AGENT-RUN parent=%s agent=%s Issue: %s Role: %s via=%s" % (
        session, agent_id, "#" + issue if issue else UNATTRIBUTED,
        role or UNATTRIBUTED, via or "none")
    meta = path[:-len(".jsonl")] + ".meta.json" if path.endswith(".jsonl") else None
    if meta and os.path.exists(meta):
        try:
            with open(meta, encoding="utf-8") as fh:
                atype = (json.load(fh) or {}).get("agentType")
            if atype:
                line += " type=%s" % atype
        except (OSError, ValueError, AttributeError):
            pass
    return line


def clip(txt, keep_whole=False):
    """The CLIP-character cut, except that `MARK[` lines past it survive whole
    (a MARK line the cut splits is moved below the cut, not printed twice)."""
    if keep_whole or len(txt) <= CLIP:
        return txt
    start = txt.rfind("\n", 0, CLIP) + 1         # start of the line the cut hits
    straddle = txt[start:].split("\n", 1)[0]
    head_end = start if straddle.lstrip().startswith("MARK[") else CLIP
    marks = [ln for ln in txt[start:].split("\n") if ln.lstrip().startswith("MARK[")]
    if not marks:
        # A cut says it is one (K209H, REV209 F10): until 2026-10-02 a message without a MARK
        # line ended mid-sentence at CLIP with nothing to tell the digest's reader that more
        # existed — a sweep reading it could take a cut sentence for the whole statement.
        return txt[:CLIP].rstrip("\n") + "\n[… cut at %d of %d chars; the rest is in the raw transcript]" \
            % (CLIP, len(txt))
    return txt[:head_end].rstrip("\n") + "\n[… cut at %d chars; MARK lines kept:]\n" % CLIP \
        + "\n".join(marks)


def digest_file(path, out, since=""):
    layout = agent_layout(path)
    if layout:
        sid = "%s/agent-%s" % layout
    else:
        sid = path.split("/")[-1].split(".")[0].replace("__", "/")
    out.write(f"\n===== SESSION {sid} =====\n")
    last = None
    if layout:
        scan = scan_file(path)
        last = scan[3]
        out.write(attribution(path, layout, scan) + "\n")
    for n, obj in enumerate(_records(path)):
        t = obj.get("type")
        if t not in ("user", "assistant"):
            continue
        # message-level delta filter: skip content already swept by a prior run
        if since and (obj.get("timestamp") or "") and str(obj.get("timestamp")) <= since:
            continue
        msg = obj.get("message") or {}
        content = msg.get("content")
        ts = (obj.get("timestamp") or "")[:16]
        if isinstance(content, str):
            txt = content.strip()
            if txt:
                out.write(f"[{ts}] {t.upper()}: {clip(txt)}\n")
            continue
        if not isinstance(content, list):
            continue
        for k, blk in enumerate(content):
            if not isinstance(blk, dict):
                continue
            bt = blk.get("type")
            if bt == "text":
                txt = (blk.get("text") or "").strip()
                if txt:
                    whole = t == "assistant" and last == (n, k)
                    out.write(f"[{ts}] {t.upper()}: {clip(txt, whole)}\n")
            elif bt == "tool_use":
                name = blk.get("name", "?")
                out.write(f"[{ts}] TOOL: {name}\n")
            elif bt == "tool_result":
                if blk.get("is_error"):
                    c = blk.get("content")
                    snippet = ""
                    if isinstance(c, str):
                        snippet = c
                    elif isinstance(c, list) and c and isinstance(c[0], dict):
                        snippet = c[0].get("text", "")
                    out.write(f"[{ts}] TOOL-ERROR: {snippet[:300]}\n")


def main():
    args = sys.argv[1:]
    since = ""
    if "--since" in args:
        i = args.index("--since")
        since = args[i + 1]
        del args[i:i + 2]
    paths = []
    for pat in args:
        paths.extend(sorted(glob.glob(pat)))
    if not paths:
        print("usage: preprocess_transcripts.py [--since ISO_TS] <file-or-glob>...", file=sys.stderr)
        sys.exit(2)
    for p in paths:
        digest_file(p, sys.stdout, since)


if __name__ == "__main__":
    main()
