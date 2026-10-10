#!/usr/bin/env bash
# Read-only correction, repetition and recall-citation metrics.
# Private patterns/archive/principal id come from KHMS_METRICS_CONFIG (JSON).
set -u
. "$(dirname "$(readlink -f "$0")")/khms_paths.sh"
khms_load || exit 3
BRAIN="$(khms_get KHMS_ROOT)"
PROJ=""
HIST=""; OWNER_ID=""; FROM=""; TO=""
while [ $# -gt 0 ]; do
  case "$1" in
    --from) FROM="$2"; shift 2;;
    --to) TO="$2"; shift 2;;
    --hist) HIST="$2"; shift 2;;
    --owner-id) OWNER_ID="$2"; shift 2;;
    --transcripts) PROJ="$2"; shift 2;;
    *) echo "usage: $0 --from DATE --to DATE [--hist FILE] [--owner-id ID] [--transcripts DIR]" >&2; exit 2;;
  esac
done
[ -n "$FROM" ] && [ -n "$TO" ] || { echo "usage: $0 --from DATE --to DATE" >&2; exit 2; }
export KHMS_METRICS_CONFIG="$(khms_get KHMS_METRICS_CONFIG 2>/dev/null || true)"
python3 - "$BRAIN" "$PROJ" "$FROM" "$TO" "$HIST" "$OWNER_ID" <<'PY'
import datetime, glob, json, os, re, sys
brain, proj, d_from, d_to, hist, owner_id = sys.argv[1:7]
config = json.load(open(os.environ["KHMS_METRICS_CONFIG"], encoding="utf-8")) if os.environ.get("KHMS_METRICS_CONFIG") else {}
hist = hist or os.path.join(brain, config.get("history", "archive/chat/history.jsonl"))
owner_id = owner_id or config.get("owner_id", "")
proj = proj or config.get("transcripts", "")
if not proj:
    sys.exit("metrics require --transcripts or a configured transcripts directory")
MARK = re.compile(config.get("mark_pattern", r"^\s*(?:[-*]\s*)?(?:\d{1,2}:\d{2}\s*)?MARK\[correction\]"))
REPEAT = re.compile(config.get("repeat_pattern", r"again|repeat|same error"), re.I)
CORR = re.compile(config.get("correction_pattern", r"\bwrong\b|\bwhy did you\b|\bfix\b|\?\?|!!"), re.I)
TS = re.compile(r'"timestamp"\s*:\s*"([^"]{10,40})"')

days = []
d = datetime.date.fromisoformat(d_from)
end = datetime.date.fromisoformat(d_to)
while d <= end:
    days.append(d.isoformat())
    d += datetime.timedelta(days=1)

opr = {k: [0, 0] for k in days}
for k in days:
    p = f"{brain}/journal/{k}.md"
    try:
        for ln in open(p, encoding="utf-8", errors="replace"):
            if MARK.search(ln):
                opr[k][0] += 1
                if REPEAT.search(ln):
                    opr[k][1] += 1
    except OSError:
        opr[k] = [None, None]

corr = {k: 0 for k in days}
nmsg = {k: 0 for k in days}
hist_note, newest = None, None
try:
    with open(hist, encoding="utf-8", errors="replace") as fh:
        for ln in fh:
            ln = ln.strip()
            if not ln:
                continue
            try:
                o = json.loads(ln)
            except ValueError:
                continue
            ts = o.get("timestamp") or ""
            try:
                day = datetime.datetime.fromisoformat(
                    ts.replace("Z", "+00:00")).astimezone().date().isoformat()
            except ValueError:
                continue
            if newest is None or ts > newest:
                newest = ts
            if day not in corr:
                continue
            if (o.get("author") or {}).get("id") != owner_id:
                continue
            nmsg[day] += 1
            if CORR.search(o.get("content") or ""):
                corr[day] += 1
except OSError as exc:
    hist_note = f"NO CHAT ARCHIVE ({exc}) — owner_corr is '-', not 0"
    corr = {k: None for k in days}
    nmsg = {k: None for k in days}

agents = {k: [0, 0, 0] for k in days}
for p in glob.glob(f"{proj}/*/subagents/*.jsonl"):
    day = None
    recall_ids, pending, last_ids, called = set(), set(), set(), False
    try:
        for ln in open(p, encoding="utf-8", errors="replace"):
            if day is None:
                m = TS.search(ln)
                if m:
                    day = datetime.datetime.fromisoformat(
                        m.group(1).replace("Z", "+00:00")).astimezone().date().isoformat()
                    if day not in agents:
                        break
            if '"tool_use"' in ln and "recall.sh" in ln:
                called = True
                pending |= set(re.findall(r'"(toolu_[A-Za-z0-9]+)"', ln))
            if pending and '"tool_result"' in ln:
                for tid in list(pending):
                    if tid in ln:
                        recall_ids |= set(KID.findall(ln))
                        pending.discard(tid)
            if '"type":"assistant"' in ln:
                ids = set(KID.findall(ln))
                if ids:
                    last_ids = ids
    except OSError:
        continue
    if day in agents:
        agents[day][2] += 1
        if called:
            agents[day][0] += 1
            if last_ids & recall_ids:
                agents[day][1] += 1

if hist_note:
    print(hist_note)
print(f"{'date':<12}{'correction':>7}{'repeat*':>8}{'owner_msg':>10}"
      f"{'owner_corr':>11}{'agents':>7}"
      f"{'recall':>7}{'cite':>6}{'share':>7}")
for k in days:
    o, r = opr[k]
    a, c, n = agents[k]
    share = f"{100.0*c/a:.0f}%" if a else "-"
    oc, om = corr[k], nmsg[k]
    print(f"{k:<12}{('-' if o is None else o):>7}{('-' if r is None else r):>8}"
          f"{('-' if om is None else om):>10}"
          f"{('-' if oc is None else oc):>11}"
          f"{n:>7}{a:>7}{c:>6}{share:>7}")
tot_o = sum(v[0] for v in opr.values() if v[0] is not None)
tot_r = sum(v[1] for v in opr.values() if v[1] is not None)
tot_a = sum(v[0] for v in agents.values())
tot_c = sum(v[1] for v in agents.values())
tot_n = sum(v[2] for v in agents.values())
tot_corr = sum(v for v in corr.values() if v is not None)
tot_msg = sum(v for v in nmsg.values() if v is not None)
print(f"{'TOTAL':<12}{tot_o:>7}{tot_r:>8}"
      f"{('-' if hist_note else tot_msg):>10}"
      f"{('-' if hist_note else tot_corr):>11}"
      f"{tot_n:>7}{tot_a:>7}{tot_c:>6}"
      f"{(f'{100.0*tot_c/tot_a:.0f}%' if tot_a else '-'):>7}")
print("\n* repeat is a PROXY: correction MARK lines that SAY the error repeated.")
print("* owner_corr is a PROXY too: of owner_msg principal chat messages that "
      "day, the ones matching")
print("  the correction pattern \u2014 read it AGAINST owner_msg, never alone")
print(f"  Source: {hist}")
print(f"  newest message: {newest or 'none'}; completeness depends on archive refresh")
PY
