#!/usr/bin/env python3
"""Measure a contradiction JUDGE against every reviewed triage night (reviewed history).

Every reviewed row (`memory/inbox/*-triage.md` with a `## REVIEW VERDICT`) exists because the
nightly judge (qwen3-coder-next) called its card pair a contradiction; the morning review then
APPLIED or REJECTED it. This tool sends the SAME pairs again \u2014 the same ADJ prompt, batch 6,
cards as they stood that night (eval_triage_selector.load_rows reads the server version), the pair in its original orientation (a = lower id, as build_candidates orders it)
and with the original lexical hint (read back from that night's sweep JSON) \u2014 to the judge
named by --judge, through khms_contradict.adjudicate, i.e. the nightly code path itself.

    reject_cut  = REJECT rows the judge no longer calls "yes"  (the row would not exist)
    apply_kept  = APPLY  rows the judge still calls "yes"
over the whole set and over the CONSISTENT subset (pairs labelled both APPLY and REJECT on
different nights are dropped \u2014 the labels contradict themselves there).

Answers are cached in a JSONL per judge (resumable: rows with an OK answer are not re-sent);
every process run appends its own usage line (tokens + the CLI's total_cost_usd) next to it.

    eval_triage_judge.py run    --judge sonnet --cache <dir>   # spends tokens
    eval_triage_judge.py report --cache <dir> [--judges sonnet,haiku,qwen]

`report` reads caches only, calls nothing. Prices: the CLI's own total_cost_usd, cross-checked
with tools/tok_account.py (never a price typed here).
"""
from __future__ import annotations

# Public helpers live beside the experiment package.
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import argparse
import glob
import json
import os
import re
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import eval_triage_selector as E  # noqa: E402
import khms_contradict as kc  # noqa: E402

BRAIN = E.BRAIN
STAGING = os.path.join(BRAIN, "memory", "inbox", ".staging")


def sweep_shapes(date):
    """{(a, b): shapes} from that night's sweep JSON (the lexical hint the model was shown)."""
    p = os.path.join(STAGING, f"triage-{date}-sweep.json")
    if not os.path.exists(p):
        return {}
    try:
        res = json.load(open(p, encoding="utf-8")).get("results", [])
    except (ValueError, OSError):
        return {}
    return {(r["a"], r["b"]): r.get("shapes") or [] for r in res if "a" in r and "b" in r}


def build_jobs(rows):
    """One job per (night, unordered pair); rows that share it share the answer."""
    shapes_by_date, jobs = {}, {}
    for r in rows:
        if not r["loser"] or not r["winner"]:
            continue
        # the night key of the file (`DATE-triage.md` -> DATE, `DATE-2-triage.md` -> DATE-2, #237): the
        # sweep JSON is named by it; a suffixed hand unit (`DATE-coordinator-triage.md`) keeps its date
        nk = re.match(r"^(\d{4}-\d{2}-\d{2}(?:-\d+)?)-triage\.md$", r["file"])
        date = nk.group(1) if nk else r["file"][:10]
        if date not in shapes_by_date:
            shapes_by_date[date] = sweep_shapes(date)
        a, b = sorted((r["loser_id"], r["winner_id"]))
        key = (date, a, b)
        if key not in jobs:
            cards = {r["loser_id"]: r["loser"], r["winner_id"]: r["winner"]}
            jobs[key] = {"key": key, "a": a, "b": b, "cards": cards, "rows": [],
                         "shapes": shapes_by_date[date].get((a, b), [])}
        jobs[key]["rows"].append((r["file"], r["n"], r["verdict"]))
    return list(jobs.values())


def consistent_mask(rows):
    labels = defaultdict(set)
    for r in rows:
        labels[frozenset((r["loser_id"], r["winner_id"]))].add(r["verdict"])
    both = {k for k, v in labels.items() if len(v) > 1}
    return (lambda r: frozenset((r["loser_id"], r["winner_id"])) not in both), both


def load_cache(path):
    ans = {}
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            o = json.loads(line)
            ans[(o["file"], o["n"])] = o
    return ans


def cmd_run(args):
    kc.JUDGE = args.judge
    kc.reset_judge_usage()
    os.makedirs(args.cache, exist_ok=True)
    cpath = os.path.join(args.cache, f"{args.judge}.jsonl")
    done = {k for k, o in load_cache(cpath).items()
            if (o.get("j") or {}).get("outcome") == kc.OUTCOME_OK
            and o.get("prompt") == kc.judge_prompt_id()}
    rows, notes = E.load_rows(BRAIN)
    jobs = [j for j in build_jobs(rows)
            if not all((f, n) in done for f, n, _ in j["rows"])]
    if args.max_jobs:
        jobs = jobs[:args.max_jobs]
    # batches of `batch` pairs inside one night (one snapshot -> one by_id)
    by_night = defaultdict(list)
    for j in jobs:
        by_night[j["key"][0]].append(j)
    chunks = []
    for night in sorted(by_night):
        js = by_night[night]
        for i in range(0, len(js), args.batch):
            chunks.append(js[i:i + args.batch])
    print(f"judge {kc.judge_description()} · {len(rows)} rows · {len(jobs)} pairs to send "
          f"in {len(chunks)} calls · workers {args.workers}", file=sys.stderr)
    t0 = time.time()

    def work(chunk):
        by_id, pairs = {}, []
        for j in chunk:
            by_id.update(j["cards"])
            pairs.append({"a": j["a"], "b": j["b"], "shapes": j["shapes"], "_job": j})
        res = kc.adjudicate(pairs, by_id, batch=args.batch, verbose=False, attempts=2)
        lines = []
        for p in res:
            j = p["_job"]
            v = {k: p["verdict"].get(k) for k in ("contradiction", "outcome", "shape",
                                                  "likely_wrong", "severity", "why")}
            for f, n, verdict in j["rows"]:
                lines.append(json.dumps({"file": f, "n": n, "verdict": verdict,
                                         "a": j["a"], "b": j["b"], "judge": args.judge,
                                         "prompt": kc.judge_prompt_id(),
                                         "j": v}, ensure_ascii=False))
        return lines

    n_done = 0
    with ThreadPoolExecutor(args.workers) as ex, open(cpath, "a", encoding="utf-8") as fh:
        for lines in ex.map(work, chunks):
            fh.write("\n".join(lines) + "\n")
            fh.flush()
            n_done += 1
            if n_done % 10 == 0:
                u = kc.judge_usage()
                print(f"  {n_done}/{len(chunks)} calls · ${u['cli_cost_usd']:.3f} · "
                      f"{time.time() - t0:.0f}s", file=sys.stderr)
    u = kc.judge_usage()
    u.update({"pairs_sent": len(jobs), "calls_planned": len(chunks),
              "wall_total_s": round(time.time() - t0, 1),
              "ended": time.strftime("%Y-%m-%dT%H:%M:%S")})
    with open(os.path.join(args.cache, f"{args.judge}.usage.jsonl"), "a") as fh:
        fh.write(json.dumps(u) + "\n")
    print(json.dumps(u), file=sys.stderr)
    return 0


def tok_account_usd(u):
    """$ from tools/tok_account.py's price table for the model(s) the CLI reported."""
    try:
        import tok_account as T
    except ImportError:
        return None
    if len(u["models"]) != 1:
        return None
    m = u["models"][0]
    pr = T.price_for_model(m)
    if not pr:
        return None
    pin, pout = pr
    return (u["input_tokens"] * pin + u["output_tokens"] * pout
            + u["cache_creation_input_tokens"] * pin * T.MULT_CW5
            + u["cache_read_input_tokens"] * pin * T.cache_read_mult(m)) / 1e6


def sum_usage(path):
    tot = defaultdict(float)
    models = set()
    for line in open(path, encoding="utf-8"):
        o = json.loads(line)
        for k in ("calls", "errors", "input_tokens", "output_tokens", "cache_read_input_tokens",
                  "cache_creation_input_tokens", "thinking_tokens", "cli_cost_usd",
                  "pairs_sent", "wall_total_s"):
            tot[k] += o.get(k) or 0
        models.update(o.get("models") or [])
    tot = dict(tot)
    tot["models"] = sorted(models)
    return tot


def pct(a, b):
    return 100.0 * a / b if b else float("nan")


def score(rows, ans, keep):
    """keep(row) -> the judge still makes this row. Missing answers are reported, not guessed."""
    out = {}
    for lab in ("REJECT", "APPLY"):
        rs = [r for r in rows if r["verdict"] == lab]
        got = [r for r in rs if (r["file"], r["n"]) in ans]
        kept = sum(1 for r in got if keep(ans[(r["file"], r["n"])]))
        out[lab] = (kept, len(got), len(rs))
    rk, rg, rn = out["REJECT"]
    ak, ag, an = out["APPLY"]
    return {"reject_cut": pct(rg - rk, rg), "apply_kept": pct(ak, ag),
            "reject_txt": f"{rg - rk}/{rg}", "apply_txt": f"{ak}/{ag}",
            "missing": (rn - rg) + (an - ag)}


def cmd_report(args):
    rows, notes = E.load_rows(BRAIN)
    is_cons, both = consistent_mask(rows)
    cons = [r for r in rows if is_cons(r)]
    files = sorted({r["file"] for r in rows})
    held = set(files[-E.N_HELD:])
    n_a = sum(r["verdict"] == "APPLY" for r in rows)
    print(f"rows {len(rows)} (APPLY {n_a}, REJECT {len(rows) - n_a}) over {len(files)} "
          f"reviewed files · consistent subset {len(cons)} rows "
          f"(APPLY {sum(r['verdict'] == 'APPLY' for r in cons)}, dropped {len(rows) - len(cons)} "
          f"rows of {len(both)} pairs labelled both ways)")
    yes = lambda o: (o.get("j") or {}).get("contradiction") == "yes"  # noqa: E731
    out = {}
    for judge in args.judges.split(","):
        cpath = os.path.join(args.cache, f"{judge}.jsonl")
        if not os.path.exists(cpath):
            print(f"\n[{judge}] no cache at {cpath}")
            continue
        allans = load_cache(cpath)
        kc.JUDGE = judge if judge in kc.JUDGE_CHOICES else kc.JUDGE
        pid = kc.judge_prompt_id()          # the prompt THIS judge is given today
        ans = {k: o for k, o in allans.items()
               if (o.get("j") or {}).get("outcome") == kc.OUTCOME_OK
               and (args.any_prompt or o.get("prompt") == pid)}
        stale = sum(1 for o in allans.values() if o.get("prompt") != pid)
        print(f"\n[{judge}] answers {len(ans)}/{len(rows)} rows made with the current judge "
              f"prompt {pid} (a non-OK outcome counts as missing"
              f"{'; ' + str(stale) + ' answers from another prompt ' + ('INCLUDED (--any-prompt)' if args.any_prompt else 'ignored') if stale else ''})")
        for name, rs in (("full", rows), ("consistent", cons),
                         ("held-out", [r for r in rows if r["file"] in held]),
                         ("held-out consistent", [r for r in cons if r["file"] in held])):
            s = score(rs, ans, yes)
            print(f"  {name:20s} REJECT cut {s['reject_txt']:>8s} ({s['reject_cut']:5.1f} %) · "
                  f"APPLY kept {s['apply_txt']:>6s} ({s['apply_kept']:5.1f} %)"
                  f"{' \u00b7 missing ' + str(s['missing']) if s['missing'] else ''}")
            out[(judge, name)] = s
        upath = os.path.join(args.cache, f"{judge}.usage.jsonl")
        if os.path.exists(upath):
            u = sum_usage(upath)
            if u.get("calls"):
                ta = tok_account_usd(u)
                per_pair = u["cli_cost_usd"] / u["pairs_sent"] if u.get("pairs_sent") else 0
                print(f"  usage: {int(u['calls'])} calls ({int(u['errors'])} failed) for "
                      f"{int(u['pairs_sent'])} pairs · in {int(u['input_tokens'])} · out "
                      f"{int(u['output_tokens'])} (thinking {int(u['thinking_tokens'])}) · cache r/w "
                      f"{int(u['cache_read_input_tokens'])}/{int(u['cache_creation_input_tokens'])}"
                      f" · ${u['cli_cost_usd']:.4f} CLI"
                      f"{f' / ${ta:.4f} tok_account' if ta is not None else ''} · models "
                      f"{u['models']}")
                # the CLI caches the prompt: most input arrives as cache_creation, so the
                # input a pair costs is all three input counters together
                pp_in = (u["input_tokens"] + u["cache_creation_input_tokens"]
                         + u["cache_read_input_tokens"]) / u["pairs_sent"]
                pp_out = u["output_tokens"] / u["pairs_sent"]
                print(f"  per pair: in {pp_in:.0f} · out {pp_out:.0f} · ${per_pair:.5f}"
                      f"  ->  250 pairs/night: in {250 * pp_in / 1e3:.0f}k · out "
                      f"{250 * pp_out / 1e3:.0f}k · ${250 * per_pair:.2f} (CLI)"
                      f"{f' / ${250 * ta / u['pairs_sent']:.2f} (tok_account)' if ta else ''}")
                out[(judge, "usd_250")] = 250 * per_pair
                out[(judge, "eval_usd")] = u["cli_cost_usd"]
            else:
                print("  usage: $0 (local model)")
    if args.json:
        json.dump({f"{k[0]}|{k[1]}": v for k, v in out.items()}, open(args.json, "w"),
                  indent=1, default=str)
    if args.verdict:
        if args.any_prompt:
            print("JUDGEDEF-VERDICT: refused \u2014 --any-prompt mixes answers from other prompts",
                  file=sys.stderr)
            return 2
        return verdict(out, args.extra_usd)
    return 0


GATE_APPLY_KEPT, GATE_REJECT_CUT = 90.0, 50.0  # on the HELD-OUT CONSISTENT subset (brief JUDGEDEF)
NIGHTLY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "nightly_triage.sh")


def nightly_default(path=NIGHTLY):
    """The judge nightly_triage.sh runs when KHMS_JUDGE is not set (None: no switch there)."""
    import re
    m = re.search(r"^export KHMS_JUDGE=\$\{KHMS_JUDGE:-(\w+)\}", open(path).read(), re.M)
    return m.group(1) if m else None


def gate_state():
    """`fixed` when the triage gate's different-moments rule no longer reads a card's date
    (independent review), `rule_off` when the rule is not in force, else `as-was`."""
    import inspect
    import khms_triage_gate as G
    if "different-moments" not in G.enabled_rules([n for n, (_, _, on) in G.RULES.items() if on]):
        return "rule_off"
    src = inspect.getsource(G._reading_date)
    return "fixed" if "card" not in inspect.signature(G._reading_date).parameters\
        and "get(\"date\")" not in src else "as-was"


def verdict(out, extra_usd=0.0):
    """Print JUDGEDEF-VERDICT. rc 0 iff the sonnet answers are complete AND were all made with
    the judge prompt now in khms_contradict.py, AND nightly_triage.sh's default is the judge the
    gate picks: sonnet when it keeps >= 90 % of APPLY and cuts >= 50 % of REJECT on the
    HELD-OUT CONSISTENT subset (8 newest nights, pairs labelled one way only), else qwen.
    rc 2: no complete measurement with the current prompt."""
    kc.JUDGE = "sonnet"
    h = out.get(("sonnet", "held-out consistent"))
    c = out.get(("sonnet", "consistent"))
    if not h or not c or h["missing"] or c["missing"]:
        print("JUDGEDEF-VERDICT: no complete sonnet measurement made with the current judge "
              f"prompt {kc.judge_prompt_id()}", file=sys.stderr)
        return 2
    passes = h["apply_kept"] >= GATE_APPLY_KEPT and h["reject_cut"] >= GATE_REJECT_CUT
    want = "sonnet" if passes else "qwen"
    have = nightly_default()
    usd = out.get(("sonnet", "usd_250"), float("nan"))
    eval_usd = out.get(("sonnet", "eval_usd"), 0.0) + extra_usd
    print(f"\nJUDGEDEF-VERDICT: heldout_reject_cut={h['reject_cut']:.1f}% "
          f"heldout_apply_kept={h['apply_kept']:.1f}% consistent_apply_kept={c['apply_kept']:.1f}% "
          f"usd_per_night={usd:.2f} default={want} eval_usd={eval_usd:.2f} gate={gate_state()} "
          f"(prompt {kc.judge_prompt_id()}; switch gate on held-out consistent: apply_kept>="
          f"{GATE_APPLY_KEPT:.0f}% and reject_cut>={GATE_REJECT_CUT:.0f}%: "
          f"{'PASS' if passes else 'FAIL'}; nightly_triage.sh default={have})")
    return 0 if have == want else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--judge", required=True, choices=kc.JUDGE_CHOICES)
    r.add_argument("--cache", required=True)
    r.add_argument("--batch", type=int, default=6)
    r.add_argument("--workers", type=int, default=4)
    r.add_argument("--max-jobs", type=int, default=0, help="send at most N pairs (probe)")
    p = sub.add_parser("report")
    p.add_argument("--cache", required=True)
    p.add_argument("--judges", default="sonnet,haiku,qwen")
    p.add_argument("--json")
    p.add_argument("--verdict", action="store_true",
                   help="print JUDGEDEF-VERDICT; rc 0 iff the answers come from the current "
                        "prompt and nightly_triage.sh's default judge is the one the measured "
                        "gate picks")
    p.add_argument("--any-prompt", action="store_true",
                   help="also score answers made with another judge prompt (history only; "
                        "--verdict refuses to pass on them)")
    p.add_argument("--extra-usd", type=float, default=0.0,
                   help="eval spend outside this cache (tuning runs) added to eval_usd")
    args = ap.parse_args()
    return cmd_run(args) if args.cmd == "run" else cmd_report(args)


if __name__ == "__main__":
    sys.exit(main())
