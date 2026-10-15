"""The nightly triage stage (nightly_triage.sh + khms_contradict.py + khms_triage_report.py + apply_triage.py).

  * the judge of record comes from the settings (KHMS_JUDGE, default sonnet); `local` needs KHMS_LLM_URL and an
    unknown name is refused -- a judge that cannot be reached is a configuration error, not a quiet night;
  * the night falls back to the LOCAL judge only when one is configured; without it a Claude night that judged
    nothing says so (and a dead sweep FAILS) instead of inventing a fallback (sibling cases, same fakes);
  * the base's own language data enters through the patterns file, not the code;
  * apply_triage writes its review marker where the morning ping reads it, and a reader cannot apply.
"""
import json
import os
import shutil
import subprocess
import sys

import pytest

TOOLS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools")

FAKE_SWEEP = r'''
import json, os, sys
a = sys.argv[1:]
if "--provenance" in a:
    print("# provenance (fake)"); sys.exit(0)
judge = os.environ.get("KHMS_JUDGE", "?")
open(os.environ["FAKE_CALLS"], "a").write(judge + "\n")
mode = "ok" if judge == "local" else os.environ["FAKE_MODE"]
if mode == "crash":
    sys.exit(3)
res = [{"a": f"K-9000{i}", "b": f"K-9001{i}",
        "verdict": {"contradiction": "no" if mode == "ok" else "unclear",
                    "outcome": "OK" if mode == "ok" else "LLM-ERROR"}} for i in range(3)]
json.dump({"results": res, "meta": {"progress": {"planned": 3}, "judge": judge}},
          open(a[a.index("--json") + 1], "w"))
'''
FAKE_REPORT = r'''
import json, sys
from collections import Counter
def judgement_census(results, meta):
    c = Counter((r.get("verdict") or {}).get("outcome") or "OK" for r in results)
    planned = max((meta.get("progress") or {}).get("planned") or 0, len(results))
    return {"judged": c["OK"], "planned": planned, "not_judged": len(results) - c["OK"],
            "no_record": planned - len(results), "partial": c["OK"] < planned, "by_outcome": dict(c)}
if __name__ == "__main__":
    a = sys.argv[1:]
    d = json.load(open(a[0]))
    fb = (" fallback-from " + a[a.index("--fallback-from") + 1]) if "--fallback-from" in a else ""
    open(a[a.index("--out") + 1], "w").write("morning file, judge " + d["meta"]["judge"] + fb + "\n")
'''


def stage(tmp_path, mode, llm):
    code = tmp_path / "code"
    shutil.copytree(TOOLS, code)
    (code / "khms_contradict.py").write_text(FAKE_SWEEP)
    (code / "khms_triage_report.py").write_text(FAKE_REPORT)
    (tmp_path / "base" / "memory" / "know").mkdir(parents=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith("KHMS_")}
    env.update(KHMS_ROOT=str(tmp_path / "base"), KHMS_STATE=str(tmp_path / "agent"),
               TRIAGE_LOG=str(tmp_path / "night.log"), TRIAGE_OUT=str(tmp_path / "triage.md"),
               NIGHTLY_DATE="2099-01-01", NIGHTLY_STAGING=str(tmp_path / "st"),
               FAKE_CALLS=str(tmp_path / "calls"), FAKE_MODE=mode)
    if llm:
        env["KHMS_LLM_URL"] = "http://local-model.invalid/api/generate"
    r = subprocess.run(["bash", str(code / "nightly_triage.sh")], capture_output=True, text=True,
                       env=env, timeout=120)
    calls = (tmp_path / "calls").read_text().split() if (tmp_path / "calls").exists() else []
    out = (tmp_path / "triage.md").read_text() if (tmp_path / "triage.md").exists() else None
    return r, calls, out, (tmp_path / "night.log").read_text()


@pytest.mark.parametrize("mode", ["errors", "crash"])
def test_a_night_the_claude_judge_cannot_judge_falls_back_to_the_local_model(tmp_path, mode):
    r, calls, out, log = stage(tmp_path, mode, llm=True)
    assert r.returncode == 0, (r.stderr, log)
    assert calls == ["sonnet", "local"] and out == "morning file, judge local fallback-from sonnet\n"
    assert "triage FALLBACK" in log


def test_without_a_local_model_there_is_no_fallback_and_nothing_is_hidden(tmp_path):
    r, calls, out, log = stage(tmp_path, "errors", llm=False)
    assert calls == ["sonnet"] and out == "morning file, judge sonnet\n"
    assert "triage FALLBACK" not in log and "the sweep judged NOTHING" in log
    # #239: the morning file is written AND the stage fails -- 2026-10-14 judged 0 of 250 and exited 0
    assert r.returncode == 1 and "triage FAILED: judged nothing" in log, (r.returncode, log)


def test_without_a_local_model_a_dead_sweep_fails_the_stage(tmp_path):
    r, calls, out, log = stage(tmp_path, "crash", llm=False)
    assert r.returncode == 1 and calls == ["sonnet"] and out is None
    assert "triage sweep FAILED" in log


def test_a_healthy_claude_night_keeps_its_own_file(tmp_path):
    r, calls, out, log = stage(tmp_path, "ok", llm=True)
    assert r.returncode == 0 and calls == ["sonnet"] and out == "morning file, judge sonnet\n"


def test_a_healthy_claude_night_without_a_local_model_exits_zero(tmp_path):
    """Sibling of the judged-nothing failure: the same configuration with judged pairs is not a failure."""
    r, calls, out, log = stage(tmp_path, "ok", llm=False)
    assert r.returncode == 0 and calls == ["sonnet"] and "triage done (3 of 3" in log, log


LIMIT_TEXT = "You've hit your monthly spend limit · raise it at https://example.invalid · resets 3am"


@pytest.mark.parametrize("answer", ["limit", "error"])
def test_an_account_usage_limit_is_its_own_outcome_not_budget(tmp_path, monkeypatch, answer):
    """#239: on 2026-10-14 the account limit answered the first judge call and the morning file said
    `outcomes {'BUDGET': 250}` -- the spend bound's label for an account limit that resets by itself. The
    sibling (an ordinary CLI error) must stay an LLM-ERROR, never a limit."""
    kc = _import(monkeypatch, tmp_path)
    monkeypatch.setattr(kc, "JUDGE", "sonnet")
    monkeypatch.setattr(kc, "JUDGE_BUDGET", None)
    monkeypatch.setattr(kc, "CLAUDE_BIN", "fake-claude")
    monkeypatch.setattr(kc, "LLM_ATTEMPTS", 1)
    text = LIMIT_TEXT if answer == "limit" else "Internal server error"

    def runner(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 1, json.dumps(
            {"type": "result", "is_error": True, "result": text, "total_cost_usd": 0}), "")
    monkeypatch.setattr(kc, "CLAUDE_RUNNER", runner)
    card = {"type": "fact", "status": "active", "level": "observation", "date": "2099-01-01",
            "body": "The widget is measured in units."}
    by_id = {f"K-9{n:04d}": dict(card, id=f"K-9{n:04d}") for n in range(20)}
    pairs = [{"a": f"K-9{2 * n:04d}", "b": f"K-9{2 * n + 1:04d}", "shapes": ""} for n in range(10)]
    results = kc.adjudicate(pairs, by_id, batch=3, verbose=False)
    if isinstance(results, tuple):
        results = results[0]
    outcomes = dict(kc.outcome_counts(results))
    if answer == "limit":
        assert outcomes == {"USAGE-LIMIT": 10}, outcomes
        import khms_triage_report as R
        never_sent = [codes for name, codes, _why in R.NOT_JUDGED_CLASSES if name == "never sent to the model"]
        assert never_sent and "USAGE-LIMIT" in never_sent[0]
    else:
        assert "USAGE-LIMIT" not in outcomes and "BUDGET" not in outcomes, outcomes


def _import(monkeypatch, tmp_path, **env):
    for k in [k for k in os.environ if k.startswith("KHMS_")]:
        monkeypatch.delenv(k)
    monkeypatch.setenv("KHMS_ROOT", str(tmp_path / "base"))
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.syspath_prepend(TOOLS)
    for m in ("khms_contradict", "khms_paths", "khms_patterns", "khms_messages", "khms_search",
              "khms_triage_gate", "khms_triage_report", "khms_triage"):
        sys.modules.pop(m, None)
    import khms_contradict as kc
    return kc


def test_the_judge_is_a_setting_and_a_local_judge_needs_an_endpoint(tmp_path, monkeypatch):
    kc = _import(monkeypatch, tmp_path)
    assert kc.JUDGE == "sonnet"
    with pytest.raises(ValueError):
        kc.judge_from_env({"KHMS_JUDGE": "qwen"})            # model names are not judge names
    with pytest.raises(ValueError, match="KHMS_LLM_URL"):
        kc.judge_from_env({"KHMS_JUDGE": "local"})
    kc = _import(monkeypatch, tmp_path, KHMS_LLM_URL="http://x.invalid/api/generate", KHMS_JUDGE="local")
    assert kc.JUDGE == "local" and kc.judge_description().startswith("local: http://x.invalid")
    assert kc.adj_system() is kc.ADJ_SYSTEM


def test_language_data_comes_from_the_patterns_file(tmp_path, monkeypatch):
    pat = tmp_path / "patterns.env"
    pat.write_text("TRIAGE_NEGATION=nein|kein\\w*\nTRIAGE_MONEY=eur\\b\n", encoding="utf-8")
    msg = tmp_path / "messages.env"
    msg.write_text("TRIAGE_KB_DOMAIN=a greenhouse knowledge base\n", encoding="utf-8")
    kc = _import(monkeypatch, tmp_path, KHMS_PATTERNS=str(pat), KHMS_MESSAGES=str(msg))
    assert kc._NEGATION.search("das ist kein Fehler") and not kc._NEGATION.search("das ist ein Fehler")
    assert kc.ADJ_SYSTEM.startswith("You audit a greenhouse knowledge base for CONTRADICTIONS.")
    kc = _import(monkeypatch, tmp_path)
    assert not kc._NEGATION.search("das ist kein Fehler")      # control: the words were data
    assert kc.ADJ_SYSTEM.startswith("You audit an engineering knowledge base for CONTRADICTIONS.")


def test_gate_reasons_cite_no_card_ids():
    """The reasons a withheld row carries are read in the morning file; they name the rule, not a card."""
    src = open(os.path.join(TOOLS, "khms_triage_gate.py"), encoding="utf-8").read().splitlines()
    reasons = [ln for ln in src if "cannot make it false" in ln or "cannot refute anything" in ln]
    assert len(reasons) >= 2 and not any("K-" in ln for ln in reasons), reasons


TRIAGE = """# triage 2099-01-01

## REVIEW VERDICT
"""


@pytest.mark.parametrize("reader", [False, True])
def test_apply_triage_marks_the_review_where_the_ping_reads_it(tmp_path, reader):
    base = tmp_path / "base"
    (base / "memory" / "know").mkdir(parents=True)
    (base / "memory" / "inbox").mkdir(parents=True)
    f = base / "memory" / "inbox" / "2099-01-01-triage.md"
    f.write_text(TRIAGE, encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith("KHMS_")}
    env.update(KHMS_ROOT=str(base), KHMS_STATE=str(tmp_path / "agent"), KHMS_NO_SNAPSHOT="1",
               KHMS_READ_ONLY="1" if reader else "0")
    r = subprocess.run([sys.executable, os.path.join(TOOLS, "apply_triage.py"), str(f), "--apply"],
                       capture_output=True, text=True, env=env, timeout=60)
    marker = base / "state" / "shared" / "reviewed" / ".reviewed-2099-01-01-triage"
    if reader:
        assert r.returncode == 4 and not marker.exists(), r.stderr
    else:
        assert r.returncode == 0 and marker.exists(), r.stderr
        assert not (base / "tools").exists()                   # no state in a code-shaped dir of the base
