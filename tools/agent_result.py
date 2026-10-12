#!/usr/bin/env python3
"""Which model actually served a stage call -- read from the agent CLI's own JSON result, never assumed.

WHY (#232, 2026-10-12): the night's stages ask for a model ALIAS (`--model sonnet`), and what an alias serves
depends on the CLI version that answers it. For two nights a stale CLI found through PATH served an older model,
~5x slower per sweep part, and the log could not say so: it recorded the configured alias, never the model.
The stage calls now run with `--output-format json`; the CLI's result object carries `modelUsage`, keyed by the
model ids that served the call (the triage judge reads the same field, khms_contradict.py `_account`).

Usage:
  agent_result.py --stage S --attempt A/N --rc RC [--served-file F] OUTPUT
      Print the call's answer (the JSON result's `result` text; output that is not the CLI's JSON result is
      printed verbatim, as it was logged before), then ONE line
          <iso time> <S> served-by: <model[,model]> (attempt A/N, rc=RC[, $cost][, N turns])
      `UNKNOWN` instead of a model when the output holds no JSON result (a killed or failed call, a CLI that
      ignored the flag). With --served-file, append `<S>\t<models>` there (the night's summary reads it).
  agent_result.py --summary F
      One line: per stage family, the models and how many calls each served (`none recorded` when empty).
  agent_result.py --night-log LOG
      One line about the LAST night in LOG (from its last `nightly start` line): the agent version line, the
      models per stage family, the triage judge's models. Empty output when LOG holds no night.
A report, never a gate: it exits 0 whatever it reads, so a caller under `set -e` cannot die of it.
"""
import argparse
import datetime
import json
import os
import re
import sys

UNKNOWN = "UNKNOWN"


def _result_object(text):
    """-> the CLI's JSON result dict in `text`, or None. The whole text first, then line by line from the end
    (stderr lines of the CLI may surround the one JSON line)."""
    cands = [text.strip()] + [ln.strip() for ln in reversed(text.splitlines())]
    for c in cands:
        if not c.startswith("{"):
            continue
        try:
            r = json.loads(c)
        except ValueError:
            continue
        if isinstance(r, dict) and ("modelUsage" in r or r.get("type") == "result"):
            return r
    return None


def served_models(r):
    """-> sorted model ids of a result object (`canonicalModel` when the CLI gives one), [] when none."""
    usage = r.get("modelUsage") if isinstance(r, dict) else None
    if not isinstance(usage, dict):
        return []
    return sorted({(m.get("canonicalModel") if isinstance(m, dict) else None) or k for k, m in usage.items()})


def family(stage):
    m = re.match(r"[A-Za-z]+", stage)
    return m.group(0) if m else stage


def one_call(a):
    try:
        text = open(a.output, encoding="utf-8", errors="replace").read()
    except OSError:
        text = ""
    r = _result_object(text)
    extra = []
    if r is None:
        if text:
            sys.stdout.write(text if text.endswith("\n") else text + "\n")
        models = [UNKNOWN]
        extra.append("no JSON result")
    else:
        # lines around the JSON object (CLI warnings on stderr) are kept; the object itself is rendered below
        for ln in text.splitlines():
            if ln.strip() and _result_object(ln) is None:
                print(ln)
        said = r.get("result")
        if said:
            print(said if isinstance(said, str) else json.dumps(said))
        models = served_models(r) or [UNKNOWN]
        if not served_models(r):
            extra.append("JSON result without modelUsage")
        if r.get("is_error"):
            extra.append("is_error")
        if r.get("total_cost_usd") is not None:
            try:
                extra.append(f"${float(r['total_cost_usd']):.4f}")
            except (TypeError, ValueError):
                pass
        if r.get("num_turns") is not None:
            extra.append(f"{r['num_turns']} turns")
    ts = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    print(f"{ts} {a.stage} served-by: {','.join(models)} (attempt {a.attempt}, rc={a.rc}"
          + "".join(", " + e for e in extra) + ")")
    if a.served_file:
        try:
            with open(a.served_file, "a", encoding="utf-8") as fh:
                fh.write(f"{a.stage}\t{','.join(models)}\n")
        except OSError as e:
            print(f"{ts} {a.stage}: could not record the served model in {a.served_file}: {e}")


def summarise(pairs):
    """pairs: [(stage, models csv)] -> 'marks=claude-x(7) sweep=claude-x(30),UNKNOWN(1)'."""
    fams = {}
    for stage, models in pairs:
        f = fams.setdefault(family(stage), {})
        for m in models.split(","):
            if m:
                f[m] = f.get(m, 0) + 1
    if not fams:
        return "none recorded"
    return " ".join(f"{fam}=" + ",".join(f"{m}({n})" for m, n in sorted(ms.items()))
                    for fam, ms in fams.items())


def summary(path):
    pairs = []
    try:
        for ln in open(path, encoding="utf-8", errors="replace"):
            if "\t" in ln:
                s, m = ln.rstrip("\n").split("\t", 1)
                pairs.append((s, m))
    except OSError:
        pass
    print(summarise(pairs))


SERVED_RE = re.compile(r"^\S+ (\S+) served-by: (\S+)")
JUDGE_RE = re.compile(r"^judge usage: .*models \[([^\]]*)\]")
AGENT_RE = re.compile(r"^\S+ agent: .*?version: (.+?)\s*$")


def night_log(path):
    try:
        lines = open(path, encoding="utf-8", errors="replace").read().splitlines()
    except OSError:
        return
    start = None
    for i, ln in enumerate(lines):
        if re.match(r"^\S+ nightly start\b", ln):
            start = i
    if start is None:
        return
    pairs, judge, version = [], set(), ""
    for ln in lines[start:]:
        m = SERVED_RE.match(ln)
        if m:
            pairs.append((m.group(1), m.group(2)))
            continue
        m = JUDGE_RE.match(ln)
        if m:
            judge.update(x.strip().strip("'\"") for x in m.group(1).split(",") if x.strip())
            continue
        m = AGENT_RE.match(ln)
        if m:
            version = m.group(1)
    parts = [f"night of {lines[start].split(' ', 1)[0]}"]
    if version:
        parts.append(f"agent {version}")
    parts.append(summarise(pairs))
    if judge:
        parts.append("triage=" + ",".join(sorted(judge)))
    print("; ".join(parts))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("output", nargs="?")
    ap.add_argument("--stage")
    ap.add_argument("--attempt", default="?")
    ap.add_argument("--rc", default="?")
    ap.add_argument("--served-file")
    ap.add_argument("--summary")
    ap.add_argument("--night-log")
    a = ap.parse_args(argv)
    try:
        if a.summary:
            summary(a.summary)
        elif a.night_log:
            night_log(a.night_log)
        elif a.stage and a.output:
            one_call(a)
        else:
            ap.print_usage(sys.stderr)
    except Exception as e:          # a report, never a gate
        print(f"agent_result.py: {type(e).__name__}: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
