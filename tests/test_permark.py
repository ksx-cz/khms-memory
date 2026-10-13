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


def test_tonights_nearest_candidate_is_shown_whole_with_its_marks_line():
    doc = PM.Doc()
    doc.cands.append(["M1", "### M1: widget bus voltage sag\nWHAT: sags\nMARKS: M-0000bbbb\n"])
    doc.cands.append(["M2", "### M2: gearbox backlash\nWHAT: two degrees\nMARKS: M-0000cccc\n"])
    block, labs = PM.tonight_block(doc, "widget bus voltage again")
    assert labs == ["M1"], "lexical alone ranks only what shares a token; the latest is not added"
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
    ("DECISION: not-carded\n- M-0000aaaa :: not know-how: a status line", "notcarded", ""),
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
elif os.environ.get("STUB_COVERED"):
    q = f'"{os.environ["STUB_QUOTE"]}" — ' if os.environ.get("STUB_QUOTE") else ""
    ans = f"DECISION: not-carded\n- {mid} :: covered by {os.environ['STUB_COVERED']}: {q}the extra date changes no action"
else:
    ans = f"DECISION: not-carded\n- {mid} :: not know-how: fixture"
if os.environ.get("STUB_SYS"):                      # the rules the run handed the model
    with open(os.environ["STUB_SYS"], "w") as fh:
        fh.write(args[args.index("--system-prompt") + 1])
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
                   "## MARKS NOT CARDED\n- M-00000003 :: not know-how: x\n")
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


# --- #237 follow-up: tonight's candidates are the k NEAREST, and the whole packet has a character budget -----
WORDS = ["alder", "birch", "cedar", "dogwood", "elm", "fir", "ginkgo", "hazel", "ironwood", "juniper"]


def night_of(n=50, body_lines=40):
    """A synthetic night: n prior candidates, each on its own topic word, every one long enough that showing it
    whole costs ~2,500 characters."""
    doc = PM.Doc()
    for i in range(1, n + 1):
        topic = f"{WORDS[i % 10]}{i:03d}"
        body = "".join(f"filler line {j} about the {topic} grove and its soil\n" for j in range(body_lines))
        doc.cands.append([f"M{i}", f"### M{i}: sensor {topic} reading drifts after warmup\ntype: fact\n"
                                   f"WHAT: the {topic} sensor reading drifts\n{body}MARKS: M-{i:08x}\n"
                                   f"NEAREST: {CA}\n"])
    return doc


def tonight_section(packet):
    return packet.split("## TONIGHT'S CANDIDATES")[1].split("\n## ")[0]


def test_a_night_of_50_candidates_shows_at_most_k_nearest_and_the_duplicate_is_first_and_whole(tmp_path):
    root = journal_root(tmp_path)
    doc = night_of(50)
    mk = mark("sensor hazel037 reading drifts by 3 percent after warmup", mid="M-0000ffff")
    packet, _shown, labs = PM.build_packet(mk, str(root), retrieval(), doc, PM.Transcripts(dirs=[], slug=""))
    sec = tonight_section(packet)
    heads = re.findall(r"(?m)^### (M\d+):", sec)
    assert 1 <= len(heads) <= 5, heads
    assert heads[0] == "M37" and labs[0] == "M37", "the near-duplicate is ranked first"
    m37 = sec.split("### M37:")[1].split("\n### ")[0]
    assert "filler line 39 about the hazel037" in m37, "the nearest one is shown whole when it fits"
    assert "M50" not in heads, "the latest candidate is not attached unless it is near"
    for lab in heads[1:]:                                    # the others: title + the first lines only
        other = sec.split(f"### {lab}:")[1].split("\n### ")[0]
        assert len(other) < 700 and "filler line 39" not in other, (lab, len(other))
    assert len(sec) < 2600 + 4 * 700
    assert PM.TONIGHT_K == 5, "the default k"


def test_k_is_configurable(tmp_path, monkeypatch):
    monkeypatch.setattr(PM, "TONIGHT_K", 2, raising=False)
    packet, _s, _l = PM.build_packet(mark("sensor hazel037 reading drifts", mid="M-0000ffff"),
                                     str(journal_root(tmp_path)), retrieval(), night_of(50),
                                     PM.Transcripts(dirs=[], slug=""))
    assert len(re.findall(r"(?m)^### M\d+:", tonight_section(packet))) == 2


def concept_embedder(texts, kind="doc"):
    """A fake dense channel: a text's vector is the one-hot of the concept it is about, whatever the wording
    (`<word>NNN` in a candidate, the spelled-out number in the mark)."""
    out = []
    for t in texts:
        v = [0.0] * 64
        n = ks.norm(t)
        m = re.search(r"[a-z]+(\d{3})\b", n)
        if "twelfth" in n:
            v[12] = 1.0
        elif m:
            v[int(m.group(1)) % 64] = 1.0
        out.append(v)
    return out


def test_the_dense_channel_finds_a_duplicate_the_lexical_channel_cannot(tmp_path):
    """The cross-language gap: a mark worded differently (another language, in the real base) shares no token with
    tonight's candidate about the same finding."""
    R = retrieval()
    R.embed = concept_embedder
    mk = mark("twelfth probe wanders once heated", mid="M-0000ffff")
    packet, _s, labs = PM.build_packet(mk, str(journal_root(tmp_path)), R, night_of(50),
                                       PM.Transcripts(dirs=[], slug=""))
    assert labs and labs[0] == "M12", labs
    assert "### M12:" in tonight_section(packet)


def test_the_packet_budget_trims_lowest_rank_first_and_keeps_the_mark_and_its_journal_line(tmp_path, monkeypatch):
    root = journal_root(tmp_path)
    doc = night_of(50)
    mk = mark(f"the widget bus voltage sags to 46.0 V at 40 A, sensor hazel037 reading drifts, see {CB}",
              mid="M-0000ffff")
    T = PM.Transcripts(dirs=[], slug="")
    full, full_shown, full_labs = PM.build_packet(mk, str(root), retrieval(), doc, T)
    assert len(full) > 7000 and len(full_shown) >= 2 and len(full_labs) == 5
    monkeypatch.setattr(PM, "PACKET_CHARS", 4000, raising=False)
    packet, shown, labs = PM.build_packet(mk, str(root), retrieval(), doc, T)
    assert len(packet) <= 4000, len(packet)
    assert mk["line"] in packet                                                   # the mark itself
    assert ">> 20: - 09:20 MARK[finding] the widget bus voltage sags" in packet   # its journal line, whole
    assert shown == full_shown[:len(shown)] and labs == full_labs[:len(labs)], "kept = a rank prefix"
    assert len(shown) + len(labs) < len(full_shown) + len(full_labs), "something was trimmed"
    for cid in set(full_shown) - set(shown):
        assert f"{cid} [" not in packet, "a trimmed card is not shown, and not in the NEAREST ids"
    assert "left out for the packet budget" in packet
    # a budget smaller than the mark and its journal line keeps both anyway
    monkeypatch.setattr(PM, "PACKET_CHARS", 50, raising=False)
    tiny, _s, _l = PM.build_packet(mk, str(root), retrieval(), doc, T)
    assert mk["line"] in tiny and ">> 20: - 09:20 MARK[finding]" in tiny


def test_packet_chars_is_logged_per_call_and_summarised(tmp_path):
    root, q, stub = run_base(tmp_path, n=3)
    log = tmp_path / "night.log"
    r = run_pm(tmp_path, root, q, stub, "--log", log, KHMS_PERMARK_PACKET_CHARS="2500")
    assert r.returncode == 0, r.stderr
    served = [ln for ln in log.read_text().splitlines() if " served-by: " in ln]
    assert len(served) == 3
    sizes = [int(re.search(r"\bpacket_chars=(\d+)", ln).group(1)) for ln in served]
    assert all(0 < s <= 2500 for s in sizes), sizes
    rows = [json.loads(ln) for ln in (tmp_path / "rows.jsonl").read_text().splitlines()]
    assert [row["packet_chars"] for row in rows] == sizes
    assert re.search(r"permark: packet_chars n=3 min=\d+ median=\d+ max=\d+ budget=2500 trimmed=\d+",
                     log.read_text())


# --- #237 PWHOLE: more whole cards (A), a type + links line per card (B), short carded/notcarded answers (C) ----
LEX = ["K-%05d" % n for n in range(201, 206)]          # five cards the lexical channel ranks for one query


def lex_retrieval():
    cards = [card(cid, f"orchid{i} the pump seal leaks under pressure\n" + "\n".join(
        f"detail line {j} of card {cid}" for j in range(12)) + "\n" + "body " * (60 - 10 * i))
             for i, cid in enumerate(LEX)]
    return PM.Retrieval(cards=cards, hy=None, glossary={}, all_cards=cards)


def whole_ids(packet):
    return re.findall(r"(?m)^(K-\d{5}) \[[a-z]+\] \(shown whole\)$", packet)


def test_whole_top_shows_the_top_n_of_each_channel_whole(tmp_path):
    T = PM.Transcripts(dirs=[], slug="")
    mk = mark("the pump seal leaks under pressure", mid="M-0000eeee")
    root = str(journal_root(tmp_path))
    packet, shown, _l = PM.build_packet(mk, root, lex_retrieval(), PM.Doc(), T)
    assert PM.WHOLE_TOP == 3, "the default N"
    assert len(shown) == 5
    assert whole_ids(packet) == shown[:3], "the lexical top 3 whole, ranks 4 and 5 summarised"
    for cid in shown[3:]:
        assert re.search(rf"(?m)^{cid} \[active\] orchid", packet), "a summary keeps its title line"
    one, _s, _l = PM.build_packet(mk, root, lex_retrieval(), PM.Doc(), T, whole_top=1)
    assert whole_ids(one) == shown[:1], "N=1 is the old top-1 rule"


def test_whole_top_is_a_config_key_and_a_flag(tmp_path):
    root, q, stub = run_base(tmp_path, n=1)
    for i, cid in enumerate(LEX):
        (root / "memory/know" / f"{cid}.md").write_text(
            f"---\nid: {cid}\ntype: fact\nlevel: observation\nstatus: active\ntags: [power]\n---\n"
            f"widget fact {i} card body\nline two\n")
    for args, env, n in (((), {}, 3), (("--whole-top", "2"), {}, 2), ((), {"KHMS_PERMARK_WHOLE_TOP": "4"}, 4),
                         (("--whole-top", "1"), {"KHMS_PERMARK_WHOLE_TOP": "4"}, 1)):
        pk = tmp_path / f"pk{n}"
        r = run_pm(tmp_path, root, q, stub, "--dry", "--packets", pk, *args, **env)
        assert r.returncode == 0, r.stdout + r.stderr
        assert len(whole_ids((pk / "M-00000001.txt").read_text())) == n, (args, env)


def linked_retrieval():
    base = card("K-00301", "the pump seal leaks under pressure\nmeasured at 6 bar\n")
    base.update(type="fact", links={"derived_from": ["K-00302"], "supports": ["K-00309"],
                                    "contradicts": ["K-00303", "K-00304"], "refuted_by": [],
                                    "supersedes": "K-00305"})
    newer = card("K-00306", "an unrelated newer card\n")
    newer.update(type="policy", links={"supersedes": ["K-00301"]})
    bare = card("K-00307", "the pump seal again, under pressure, no links\n")
    bare.update(type="problem→solution")
    cards = [base, newer, bare]
    return PM.Retrieval(cards=cards, hy=None, glossary={}, all_cards=cards)


def test_the_links_line_names_type_and_link_ids_only_when_on(tmp_path):
    T = PM.Transcripts(dirs=[], slug="")
    mk = mark("the pump seal leaks under pressure", mid="M-0000eeee")
    root = str(journal_root(tmp_path))
    assert PM.LINKS_LINE == 0, "off by default"
    off, _s, _l = PM.build_packet(mk, root, linked_retrieval(), PM.Doc(), T)
    assert "type: fact" not in off and "superseded_by" not in off
    on, shown, _l = PM.build_packet(mk, root, linked_retrieval(), PM.Doc(), T, links=1)
    assert shown[:2] == ["K-00301", "K-00307"]
    # under the card's heading line, whole or summary alike; supports is not one of the five keys
    assert ("K-00301 [active] (shown whole)\ntype: fact · supersedes: K-00305 · superseded_by: K-00306 · "
            "contradicts: K-00303, K-00304 · derived_from: K-00302\n") in on
    assert "K-00309" not in on
    assert "\ntype: problem→solution · no links\n" in on


def test_the_links_line_is_a_config_key_and_a_flag(tmp_path):
    root, q, stub = run_base(tmp_path, n=1)
    (root / "memory/know" / "K-00308.md").write_text(
        "---\nid: K-00308\ntype: policy\nlevel: derived\nstatus: active\ntags: [power]\nlinks:\n"
        "  derived_from: []\n  supports: []\n  contradicts: [K-00101]\n  refuted_by: []\n  supersedes: null\n"
        "---\nwidget fact policy card\n")
    for args, env, on in (((), {}, False), (("--links-line", "1"), {}, True),
                          ((), {"KHMS_PERMARK_LINKS_LINE": "1"}, True),
                          (("--links-line", "0"), {"KHMS_PERMARK_LINKS_LINE": "1"}, False)):
        pk = tmp_path / f"pk{len(args)}{len(env)}"
        r = run_pm(tmp_path, root, q, stub, "--dry", "--packets", pk, *args, **env)
        assert r.returncode == 0, r.stdout + r.stderr
        text = (pk / "M-00000001.txt").read_text()
        assert ("\ntype: policy · contradicts: K-00101\n" in text) is on, (args, env)
        assert ("\ntype: fact · no links\n" in text) is on


def test_the_short_answer_rules_are_rendered_only_when_on(tmp_path):
    root, q, stub = run_base(tmp_path, n=1)
    sysf = tmp_path / "sys.txt"
    seen = {}
    for args, env in (((), {}), (("--short-answers", "0"), {}), ((), {"KHMS_PERMARK_SHORT_ANSWERS": "0"}),
                      (("--short-answers", "1"), {"KHMS_PERMARK_SHORT_ANSWERS": "0"})):
        (tmp_path / "out.md").unlink(missing_ok=True)
        r = run_pm(tmp_path, root, q, stub, *args, STUB_SYS=sysf, **env)
        assert r.returncode == 0, r.stdout + r.stderr
        rules = sysf.read_text()
        seen[(args, tuple(env.items()))] = "SHORT ANSWERS" in rules
        if "SHORT ANSWERS" in rules:
            assert "REASON: <one sentence" in rules and "DECISION: carded K-xxxxx" in rules
            assert rules.index("SHORT ANSWERS") < rules.index("# THE CARD SCHEMA"), "right after the rules"
    assert list(seen.values()) == [True, False, False, True], "on by default (owner 2026-10-13 14:03)"
    assert PM.SHORT_ANSWERS == 1


def test_a_short_carded_answer_parses_keeps_its_quote_and_its_reason():
    ans = (f"DECISION: carded {CA}\n- M-0000aaaa :: already carded {CA}: \"the widget bus voltage sags under load\"\n"
           "REASON: the card holds the sag this mark reports")
    dec = PM.parse_answer(ans, "M-0000aaaa")
    assert dec["kind"] == "carded" and dec["arg"] == CA, dec
    assert dec["verdict"] == f"- M-0000aaaa :: already carded {CA}: \"the widget bus voltage sags under load\""
    assert dec["reason"] == "the card holds the sag this mark reports"
    doc = PM.Doc()
    PM.apply(doc, "M-0000aaaa", dec, [CA])
    nc = doc.render().split("## MARKS NOT CARDED")[1]
    assert "REASON" not in nc, "the reason goes to the JSONL row, the verdict line stays greppable"
    import mark_ledger as ML
    am = re.search(r"already carded\s+(K-\d{5})\b\s*[:—–-]?\s*(.*)$", dec["verdict"])
    assert ML.quote_in_card(am.group(2), "x\nthe widget bus voltage sags under load\n")
    short_nc = PM.parse_answer("DECISION: not-carded\n- M-0000aaaa :: not know-how: a status line", "M-0000aaaa")
    assert short_nc["kind"] == "notcarded" and short_nc["reason"] == ""


def test_the_effort_is_a_config_key_and_a_flag(tmp_path):
    root, q, stub = run_base(tmp_path, n=1)
    got = []
    for args, env in (((), {}), ((), {"KHMS_PERMARK_EFFORT": "medium"}), (("--effort", "low"), {}),
                      (("--effort", "high"), {"KHMS_PERMARK_EFFORT": "medium"})):
        (tmp_path / "out.md").unlink(missing_ok=True)
        r = run_pm(tmp_path, root, q, stub, *args, **env)
        assert r.returncode == 0, r.stdout + r.stderr
        a = stub_calls(tmp_path)[-1]["args"]
        got.append(a[a.index("--effort") + 1])
        assert f"effort {got[-1]}" in r.stdout, "the run's first line names the effort"
    assert got == ["high", "medium", "low", "high"], got
    (tmp_path / "out.md").unlink(missing_ok=True)
    bad = run_pm(tmp_path, root, q, stub, KHMS_PERMARK_EFFORT="hgih")
    assert bad.returncode == 2 and "KHMS_PERMARK_EFFORT" in bad.stderr
    assert len(stub_calls(tmp_path)) == 4, "a mistyped effort makes no call"


def test_the_covered_rule_variant_is_rendered_only_when_on(tmp_path):
    root, q, stub = run_base(tmp_path, n=1)
    sysf = tmp_path / "sys.txt"
    seen = []
    for args, env in (((), {}), (("--covered-rule", "1"), {}), ((), {"KHMS_PERMARK_COVERED_RULE": "1"}),
                      (("--covered-rule", "0"), {"KHMS_PERMARK_COVERED_RULE": "1"}),
                      (("--covered-rule", "1", "--short-answers", "0"), {})):
        (tmp_path / "out.md").unlink(missing_ok=True)
        r = run_pm(tmp_path, root, q, stub, *args, STUB_SYS=sysf, **env)
        assert r.returncode == 0, r.stdout + r.stderr
        rules = sysf.read_text()
        assert "<!--" not in rules and "rule:covered" not in rules, "no marker line reaches the model"
        on = ":: covered by K-xxxxx:" in rules
        seen.append(on)
        assert ("no sentence to quote — write a candidate instead" in rules) is not on, "exactly one variant of the topic rule"
        if on:
            assert "would the mark's extra content make a future agent act" in rules
            assert ("SHORT ANSWERS" in rules) is ("--short-answers" not in args)
            if "SHORT ANSWERS" in rules:
                short = rules[rules.index("SHORT ANSWERS"):]
                assert ":: covered by K-xxxxx:" in short, "the short shape names the covered verdict too"
    assert seen == [False, True, True, False, True], seen
    assert PM.COVERED_RULE == 0, "off by default"


def test_pick_rule_refuses_a_prompt_without_the_block():
    text = "a\n<!-- rule:covered=0 -->\nold\n<!-- rule:covered=1 -->\nnew\n<!-- /rule:covered -->\nz\n"
    assert PM.pick_rule(text, "covered", 0) == "a\nold\nz\n"
    assert PM.pick_rule(text, "covered", 1) == "a\nnew\nz\n"
    with pytest.raises(ValueError):
        PM.pick_rule("a\nb\n", "covered", 1)


def test_a_covered_verdict_is_not_carded_with_its_card_as_the_target(tmp_path):
    ans = f"DECISION: not-carded\n- M-0000aaaa :: covered by {CA}: the second unit's 11.2 V changes no action"
    dec = PM.parse_answer(ans, "M-0000aaaa")
    assert dec["kind"] == "notcarded" and dec["arg"] == CA, dec
    doc = PM.Doc()
    kind, target, lab = PM.apply(doc, "M-0000aaaa", dec, [CA])
    assert (kind, target, lab) == ("notcarded", CA, None)
    nc = doc.render().split("## MARKS NOT CARDED")[1]
    assert f"- M-0000aaaa :: covered by {CA}: the second unit's 11.2 V changes no action" in nc
    plain = PM.parse_answer("DECISION: not-carded\n- M-0000aaaa :: not know-how: a status line", "M-0000aaaa")
    assert plain["arg"] == ""
    # the run: the row names the card, the decisions line counts it apart, the night file keeps the line
    root, q, stub = run_base(tmp_path, n=2)
    r = run_pm(tmp_path, root, q, stub, STUB_COVERED=CA)
    assert r.returncode == 0, r.stdout + r.stderr
    rows = [json.loads(ln) for ln in (tmp_path / "rows.jsonl").read_text().splitlines()]
    assert [(x["decision"], x["target"]) for x in rows] == [("notcarded", CA)] * 2
    assert "notcarded=2" in r.stdout and "covered=2" in r.stdout
    assert (tmp_path / "out.md").read_text().count(f":: covered by {CA}:") == 2


def test_the_covered_verdict_asks_for_a_verbatim_quote_in_both_variants(tmp_path):
    """#237 (owner 2026-10-13 16:37): with the covered rule on, the normal and the short answer shape both ask the
    `covered by K-x` verdict for one sentence copied verbatim from K-x — the shape the carded answer uses."""
    root, q, stub = run_base(tmp_path, n=1)
    sysf = tmp_path / "sys.txt"
    shape = ':: covered by K-xxxxx: "<one sentence copied VERBATIM from K-xxxxx'
    rules = {}
    for short, covered in (("0", "1"), ("1", "1"), ("1", "0")):
        (tmp_path / "out.md").unlink(missing_ok=True)
        r = run_pm(tmp_path, root, q, stub, "--covered-rule", covered, "--short-answers", short, STUB_SYS=sysf)
        assert r.returncode == 0, r.stdout + r.stderr
        rules[short, covered] = sysf.read_text()
    normal = rules["0", "1"]
    assert shape in normal and "needs no quote" not in normal
    short = rules["1", "1"]
    assert shape in short[short.index("SHORT ANSWERS"):], "the short shape asks for the quote too"
    assert "covered by K-xxxxx" not in rules["1", "0"]


def test_a_covered_verdict_without_a_verifiable_quote_is_counted_unproven(tmp_path):
    """The run logs (never gates) a covered verdict whose quote the card does not hold, like `already carded`."""
    root, q, stub = run_base(tmp_path, n=2)
    r = run_pm(tmp_path, root, q, stub, STUB_COVERED=CA)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "covered=2" in r.stdout and "quotes-unproven=2" in r.stdout, r.stdout
    rows = [json.loads(ln) for ln in (tmp_path / "rows.jsonl").read_text().splitlines()]
    assert all(x.get("quote_unproven") for x in rows)
    for f in ("out.md", "rows.jsonl"):
        (tmp_path / f).unlink()
    r = run_pm(tmp_path, root, q, stub, STUB_COVERED=CA, STUB_QUOTE="the widget bus voltage sags under load")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "covered=2" in r.stdout and "quotes-unproven=0" in r.stdout, r.stdout
    rows = [json.loads(ln) for ln in (tmp_path / "rows.jsonl").read_text().splitlines()]
    assert [(x["decision"], x["target"], x.get("quote_unproven")) for x in rows] == [("notcarded", CA, None)] * 2
    dec = PM.parse_answer(f'DECISION: not-carded\n- M-0000aaaa :: covered by {CA}: "the widget bus voltage sags '
                          f'under load" — the extra date changes no action', "M-0000aaaa")
    assert dec["kind"] == "notcarded" and dec["arg"] == CA
