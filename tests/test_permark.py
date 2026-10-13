"""The per-mark evaluator (#237): one packet assembled with 0 tokens, one call without tools, one decision.

Every fixture is synthetic. The packet tests pin what the model is shown (journal context, the cards the mark
names, the lexical top k, tonight's candidates, a transcript excerpt only when the mark names a session); the
decision tests pin how each of the decision kinds becomes today's output format; the run tests drive the real
script with a stub agent CLI (resume by id, the deadline, the argv without tools, an unparsable answer).
"""
import datetime as dt
import json
import os
import re
import subprocess
import sys

import pytest

TOOLS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools")
sys.path.insert(0, TOOLS)
import khms_search as ks  # noqa: E402
import permark as PM  # noqa: E402

CA, CB, CC = "K-%05d" % 101, "K-%05d" % 102, "K-%05d" % 103


def card(cid, body, status="active"):
    return {"id": cid, "status": status, "body": body, "nbody": ks.norm(body),
            "first": body.splitlines()[0], "ntags": "", "tags": [], "fog": False}


CARDS = [card(CA, "the widget bus voltage sags under load\nmeasured 47.1 V at 30 A on the bench\nline three\n"
                  "line four\n" + "padding " * 900),
         card(CB, "gearbox backlash is two degrees\nmeasured on the left wheel\n"),
         card(CC, "an older card about the widget bus\nsuperseded long ago\n", status="superseded")]


def retrieval():
    return PM.Retrieval(cards=[c for c in CARDS if c["status"] == "active"], hy=None, glossary={},
                        all_cards=CARDS)


def journal_root(tmp_path):
    root = tmp_path / "base"
    (root / "journal").mkdir(parents=True)
    lines = [f"- 09:{i:02d} plain line {i}" for i in range(1, 30)]
    lines[19] = "- 09:20 MARK[finding] the widget bus voltage sags to 46.0 V at 40 A " + "x" * 50
    lines[17] = "- 09:18 neighbour line " + "y" * 900                        # clipped to 600 characters
    (root / "journal" / "2099-03-04.md").write_text("\n".join(lines) + "\n")
    return root


def mark(text, mid="M-0000aaaa", loc="journal/2099-03-04.md:20", date="2099-03-04", time="09:20"):
    line = f"- {mid} | {date} {time} | finding | {loc} | {text}"
    return PM.parse_queue(line)[0]


# --- the packet -------------------------------------------------------------------------------------------
def test_the_packet_holds_the_journal_context_the_named_and_the_nearest_cards(tmp_path):
    root = journal_root(tmp_path)
    mk = mark(f"the widget bus voltage sags to 46.0 V at 40 A, see {CC} and {CB}")
    T = PM.Transcripts(dirs=[], slug="")
    packet, shown, _labs = PM.build_packet(mk, str(root), retrieval(), PM.Doc(), T)
    ctx = packet.split("## JOURNAL CONTEXT")[1].split("## NEAREST CARDS")[0]
    assert ">> 20: - 09:20 MARK[finding] the widget bus" in ctx                   # its own line, whole
    assert "   14: - 09:14" in ctx and "   23: - 09:23" in ctx                    # line-6 .. line+3
    assert "   13:" not in ctx and "   24:" not in ctx
    assert "more chars]" in ctx.split("   18:")[1].split("\n")[0]                 # a long neighbour is clipped
    # the named cards come first, whatever their status; the lexical channel finds the voltage card
    assert shown[:2] == [CC, CB] and CA in shown
    assert f"{CC} [superseded] (shown whole)" in packet
    assert f"{CA} [active] (shown whole)" in packet                              # lexical top 1, whole
    assert "hybrid top 8: unavailable tonight" in packet
    assert "## SOURCE EXCERPT" not in packet, "no session named, no excerpt"
    assert "TONIGHT'S CANDIDATES\nnone yet tonight" in packet


def test_tonights_candidates_are_shown_whole_with_their_marks_line():
    doc = PM.Doc()
    doc.cands.append(["M1", "### M1: widget bus voltage sag\nWHAT: sags\nMARKS: M-0000bbbb\n"])
    doc.cands.append(["M2", "### M2: gearbox backlash\nWHAT: two degrees\nMARKS: M-0000cccc\n"])
    block, labs = PM.tonight_block(doc, "widget bus voltage again")
    assert labs[0] == "M1" and "M2" in labs                                       # closest + the latest
    assert "### M1: widget bus voltage sag" in block and "MARKS: M-0000bbbb" in block


TRANSCRIPT = [
    {"type": "user", "timestamp": "2099-07-04T07:55:00Z", "message": {"content": "too early, outside"}},
    {"type": "user", "timestamp": "2099-07-04T08:05:00Z",
     "message": {"content": "<channel source=\"chat\">\nkeep the widget bus above 45 V\nsecond line"}},
    {"type": "assistant", "timestamp": "2099-07-04T08:10:00Z",
     "message": {"content": [{"type": "text", "text": "noted, measuring now"},
                             {"type": "tool_use", "name": "Bash", "input": {}}]}},
    {"type": "user", "timestamp": "2099-07-04T08:21:00Z", "message": {"content": "inside the +2 min edge"}},
    {"type": "user", "timestamp": "2099-07-04T08:30:00Z", "message": {"content": "too late, outside"}},
]


@pytest.fixture
def prague(monkeypatch):
    import time
    monkeypatch.setenv("TZ", "Europe/Prague")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def test_a_transcript_excerpt_only_for_a_mark_naming_a_session_cut_in_utc(tmp_path, prague):
    proj = tmp_path / "transcripts" / "proj"
    proj.mkdir(parents=True)
    (proj / "abcd1234-0000-0000-0000-000000000000.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in TRANSCRIPT))
    T = PM.Transcripts(dirs=[str(tmp_path / "transcripts")], slug="")
    root = journal_root(tmp_path)
    # the journal's clock is local: 10:19 in July in Prague is 08:19 UTC, and the digest stamps are UTC
    mk = mark("the owner wants the widget bus above 45 V (sess abcd1234)", date="2099-07-04", time="10:19")
    assert PM.local_to_utc("2099-07-04", "10:19").strftime("%H:%M") == "08:19"
    packet, _s, _l = PM.build_packet(mk, str(root), retrieval(), PM.Doc(), T)
    ex = packet.split("## SOURCE EXCERPT")[1]
    assert "07:59–08:21 UTC" in ex
    assert "keep the widget bus above 45 V" in ex and "second line" in ex       # continuation lines kept
    assert "noted, measuring now" in ex and "inside the +2 min edge" in ex
    assert "TOOL" not in ex and "too early" not in ex and "too late" not in ex
    # the same mark without its session id gets no excerpt
    mk2 = mark("the owner wants the widget bus above 45 V", date="2099-07-04", time="10:19")
    assert "## SOURCE EXCERPT" not in PM.build_packet(mk2, str(root), retrieval(), PM.Doc(), T)[0]


def test_the_excerpt_keeps_the_end_nearest_the_mark():
    digest = "".join(f"[2099-01-01T10:{m:02d}] USER: message {m} " + "z" * 400 + "\n" for m in range(0, 21))
    ex = PM.excerpt(digest, dt.datetime(2099, 1, 1, 10, 20, tzinfo=dt.timezone.utc), max_chars=1000)
    assert ex.startswith("[… earlier records cut]") and "message 20" in ex and "message 1 " not in ex


# --- the decisions ------------------------------------------------------------------------------------------
CAND = ("### M1: widget bus sags to 46.0 V at 40 A\ntype: fact\nlevel: observation\ntags: [power]\n"
        "WHAT: the bus sags to 46.0 V at 40 A\nRELATION: supports {ca}\n**QUOTES:**\n"
        "- src=journal :: the widget bus voltage sags to 46.0 V at 40 A\n").format(ca=CA)


@pytest.mark.parametrize("answer,kind,arg", [
    ("DECISION: not-carded\n- M-0000aaaa :: not know-how (K-93929): a status line", "notcarded", ""),
    ("DECISION: new\n" + CAND, "new", ""),
    ("DECISION: belongs M1", "belongs", "M1"),
    (f"DECISION: carded {CA}\n- M-0000aaaa :: already carded {CA}: \"the widget bus voltage sags under load\"",
     "carded", CA),
    (f"DECISION: supersede {CA}\n" + CAND.replace(f"supports {CA}", f"supersedes {CA} BECAUSE re-measured"),
     "supersede", CA),
    (f"DECISION: contradict {CA}\n" + CAND.replace("supports", "contradicts"), "contradict", CA),
    (f"DECISION: erratum {CA}\n- M-0000aaaa :: erratum proposed {CA}: \"measured 47.1 V at 30 A on the bench\" "
     f"-> 47.1 V at 20 A (source: bench log)", "erratum", CA),
])
def test_every_decision_kind_parses_and_lands_in_todays_format(answer, kind, arg):
    doc = PM.Doc()
    doc.cands.append(["M1", "### M1: earlier\nWHAT: x\nMARKS: M-0000bbbb\n"])
    dec = PM.parse_answer("```\n" + answer + "\n```", "M-0000aaaa")
    assert dec["kind"] == kind and (dec.get("arg") or "") == arg, dec
    got, target, lab = PM.apply(doc, "M-0000aaaa", dec, [CA, CB])
    assert got == kind
    text = doc.render()
    assert "M-0000aaaa" in doc.decided()
    if kind in ("new", "supersede", "contradict"):
        assert lab == "M2" and "### M2: widget bus sags" in text
        block = doc.find("M2")[1]
        assert re.search(r"(?m)^MARKS: M-0000aaaa$", block) and f"NEAREST: {CA}, {CB}" in block
    elif kind == "belongs":
        assert re.search(r"(?m)^MARKS: M-0000bbbb M-0000aaaa$", doc.find("M1")[1])
    else:
        nc = text.split("## MARKS NOT CARDED")[1]
        assert re.search(r"(?m)^- M-0000aaaa :: ", nc)
        if kind == "carded":
            assert f"already carded {CA}: \"the widget bus" in nc
        if kind == "erratum":
            assert f"erratum proposed {CA}:" in nc


@pytest.mark.parametrize("answer", [
    "I think this is know-how.",                                   # no decision line
    "DECISION: new\nno candidate here",                            # new without a candidate
    "DECISION: carded\n- M-0000aaaa :: already carded",            # no card id
    "DECISION: not-carded\n- M-9999ffff :: not know-how: another id",   # a verdict for another id
    "DECISION: belongs\n",                                         # no label
])
def test_an_answer_without_a_block_for_this_id_is_unparsed(answer):
    assert PM.parse_answer(answer, "M-0000aaaa")["kind"] == "unparsed"


def test_belongs_with_a_revision_that_drops_an_old_id_falls_back_to_append():
    doc = PM.Doc()
    doc.cands.append(["M1", "### M1: earlier\nWHAT: x\nMARKS: M-0000bbbb M-0000cccc\nNEAREST: K-00101\n"])
    rev = "DECISION: belongs M1\n### M1: revised\nWHAT: y with 46.0 V\nMARKS: M-0000bbbb\n"
    PM.apply(doc, "M-0000aaaa", PM.parse_answer(rev, "M-0000aaaa"), [CB])
    block = doc.find("M1")[1]
    assert "WHAT: x" in block and "revised" not in block
    assert re.search(r"(?m)^MARKS: M-0000bbbb M-0000cccc M-0000aaaa$", block)
    # a revision that keeps every id replaces the candidate, its NEAREST the union
    rev2 = "DECISION: belongs M1\n### M1: revised\nWHAT: y with 46.0 V\nMARKS: M-0000bbbb M-0000cccc M-0000aaaa\n"
    PM.apply(doc, "M-0000dddd", PM.parse_answer(rev2, "M-0000dddd"), [CB])
    block = doc.find("M1")[1]
    assert "revised" in block and re.search(r"(?m)^MARKS: M-0000bbbb M-0000cccc M-0000aaaa M-0000dddd$", block)
    assert re.search(rf"(?m)^NEAREST: K-00101, {CB}$", block)


def test_the_second_mark_sees_the_first_marks_candidate_in_its_packet(tmp_path):
    """The in-night duplicate rule: marks are evaluated in order, and each packet shows tonight's candidates."""
    root = journal_root(tmp_path)
    doc = PM.Doc()
    first = mark("the widget bus voltage sags to 46.0 V at 40 A", mid="M-0000aaaa")
    PM.apply(doc, first["id"], PM.parse_answer("DECISION: new\n" + CAND, first["id"]), [CA])
    second = mark("again: widget bus voltage sag at 40 A", mid="M-0000eeee")
    packet, _s, labs = PM.build_packet(second, str(root), retrieval(), doc, PM.Transcripts(dirs=[], slug=""))
    assert labs == ["M1"]
    assert "### M1: widget bus sags to 46.0 V" in packet.split("## TONIGHT'S CANDIDATES")[1]
    PM.apply(doc, second["id"], PM.parse_answer("DECISION: belongs M1", second["id"]), [CA])
    assert re.search(r"(?m)^MARKS: M-0000aaaa M-0000eeee$", doc.render())
    assert len(doc.cands) == 1


# --- the run, with a stub agent ------------------------------------------------------------------------------
STUB = r'''#!/usr/bin/env python3
import json, os, re, sys
args = sys.argv[1:]
packet = sys.stdin.read()
with open(os.environ["STUB_LOG"], "a") as fh:
    fh.write(json.dumps({"args": [a if len(a) < 200 else "<long>" for a in args]}) + "\n")
mid = re.search(r"^- ([MG]-[0-9a-f]{8}) \|", packet, re.M).group(1)
if mid in os.environ.get("STUB_GARBLE", ""):
    ans = "no decision at all"
else:
    ans = f"DECISION: not-carded\n- {mid} :: not know-how (K-93929): fixture"
if os.environ.get("STUB_SLEEP"):
    import time; time.sleep(float(os.environ["STUB_SLEEP"]))
print(json.dumps({"type": "result", "result": ans, "is_error": False, "total_cost_usd": 0.002, "num_turns": 1,
                  "modelUsage": {"stub-model": {}}, "usage": {"input_tokens": 10, "output_tokens": 5,
                  "cache_read_input_tokens": 3, "cache_creation_input_tokens": 1}}))
'''


def run_base(tmp_path, n=5):
    root = tmp_path / "base"
    for d in ("journal", "memory/know", "memory/views", "memory/inbox"):
        (root / d).mkdir(parents=True, exist_ok=True)
    (root / "memory/know" / f"{CA}.md").write_text(
        f"---\nid: {CA}\ntype: fact\nlevel: observation\nstatus: active\ntags: [power]\n---\n"
        "the widget bus voltage sags under load\n")
    (root / "memory/views/tags.md").write_text("# tags\n- power\n")
    q = tmp_path / "queue.md"
    q.write_text("# MARK QUEUE\n" + "".join(
        f"- M-0000{i:04x} | 2099-03-04 09:{i:02d} | finding | journal/2099-03-04.md:{i} | widget fact {i}\n"
        for i in range(1, n + 1)))
    stub = tmp_path / "stub.py"
    stub.write_text(STUB)
    stub.chmod(0o755)
    return root, q, stub


def run_pm(tmp_path, root, q, stub, *args, **env):
    e = {k: v for k, v in os.environ.items() if not k.startswith("KHMS_")}
    e.update(KHMS_ROOT=str(root), STUB_LOG=str(tmp_path / "stub.jsonl"))
    e.update({k: str(v) for k, v in env.items()})
    return subprocess.run([sys.executable, os.path.join(TOOLS, "permark.py"), "--queue", str(q),
                           "--out", str(tmp_path / "out.md"), "--agent", str(stub), "--no-dense",
                           "--jsonl", str(tmp_path / "rows.jsonl"), *map(str, args)],
                          env=e, capture_output=True, text=True, timeout=120)


def stub_calls(tmp_path):
    p = tmp_path / "stub.jsonl"
    return [json.loads(ln) for ln in p.read_text().splitlines()] if p.exists() else []


def test_one_call_per_mark_without_tools_and_a_resume_skips_what_is_decided(tmp_path):
    root, q, stub = run_base(tmp_path)
    r = run_pm(tmp_path, root, q, stub, "--limit", "2")
    assert r.returncode == 0, r.stdout + r.stderr
    cs = stub_calls(tmp_path)
    assert len(cs) == 2
    a = cs[0]["args"]
    assert a[a.index("--tools") + 1] == "" and "--system-prompt" in a and "--no-session-persistence" in a
    assert not {"Read", "Grep", "Bash", "Write", "Edit"} & set(a) and "--allowedTools" not in a
    assert a[a.index("--effort") + 1] == "high" and a[a.index("--model") + 1] == "sonnet"
    out = (tmp_path / "out.md").read_text()
    assert out.count(":: not know-how") == 2
    r = run_pm(tmp_path, root, q, stub)
    assert r.returncode == 0, r.stderr
    assert len(stub_calls(tmp_path)) == 5, "the resumed run evaluates only the 3 undecided marks"
    assert (tmp_path / "out.md").read_text().count(":: not know-how") == 5
    rows = [json.loads(ln) for ln in (tmp_path / "rows.jsonl").read_text().splitlines()]
    assert rows[0]["total_cost_usd"] == 0.002 and rows[0]["usage"]["cache_read"] == 3
    assert rows[0]["models"] == ["stub-model"] and rows[0]["decision"] == "notcarded"
    assert "=== marks === permark: 3 mark(s)" in r.stdout and "notcarded=3" in r.stdout


def test_no_mark_starts_after_the_deadline(tmp_path):
    root, q, stub = run_base(tmp_path)
    import time
    r = run_pm(tmp_path, root, q, stub, "--deadline", time.time() + 1.5, STUB_SLEEP="1")
    assert r.returncode == 0, r.stderr
    n = len(stub_calls(tmp_path))
    assert 1 <= n <= 2, n
    assert f"deadline reached — {5 - n} mark(s) left OPEN" in r.stdout


def test_an_unparsable_answer_leaves_the_mark_out_and_counts_it(tmp_path):
    root, q, stub = run_base(tmp_path, n=3)
    r = run_pm(tmp_path, root, q, stub, STUB_GARBLE="M-00000002")
    assert r.returncode == 0, r.stderr
    out = (tmp_path / "out.md").read_text()
    assert "M-00000002" not in out and "M-00000001" in out and "M-00000003" in out
    assert "unparsed=1" in r.stdout


def test_split_run_ready_commits_each_batch_with_its_durable_output(tmp_path):
    src = tmp_path / "out.md"
    src.write_text("# x\n\n### M1: a\nWHAT: a\nMARKS: M-00000001\n\n### M2: b\nWHAT: b\nMARKS: M-00000002\n\n"
                   "## MARKS NOT CARDED\n- M-00000003 :: not know-how (K-93929): x\n")
    carry = tmp_path / "carry"
    carry.mkdir()
    r = subprocess.run([sys.executable, os.path.join(TOOLS, "nightly_batch.py"), "split-run", str(src),
                        "--size", "1", "--carry", str(carry), "--run", "N-m01", "--ready"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    names = sorted(p.name for p in (carry / "N-m01").iterdir())
    assert names == ["cons-01.md", "cons-01.out.md", "cons-02.md", "cons-02.out.md", "notcarded.md"], names
    assert "### M2: b" in (carry / "N-m01/cons-02.out.md").read_text()
    assert not (carry / "N-m01/WINDOW").exists()


def test_the_mark_budget_is_150_minutes():
    text = open(os.path.join(TOOLS, "nightly_distill.sh"), encoding="utf-8").read()
    assert re.search(r"(?m)^MARK_BUDGET_MIN=\$\{MARK_BUDGET_MIN:-150\}$", text)
    assert "MARK_CHUNK" not in re.sub(r"(?m)^#.*$", "", text), "the 60-mark chunks are retired"
