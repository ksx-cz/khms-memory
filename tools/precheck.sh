#!/usr/bin/env bash
# Pre-action check: print active policies, gotchas, refuted/challenged cards for given tags.
# Usage: precheck.sh <tag> [tag...]   -- a duty before risky actions (hardware writes, config, deletion).
# Zero LLM tokens.
set -euo pipefail
. "$(dirname "$(readlink -f "$0")")/khms_paths.sh"
[ $# -ge 1 ] || { echo "usage: precheck.sh <tag> [tag...]" >&2; exit 2; }
khms_load || exit 3
ROOT="$(khms_get KHMS_ROOT)"
LOG="$(khms_get KHMS_PRECHECK_LOG)"

# Every invocation is logged, so the daily review can count precheck runs against the day's
# root-cause entries: the rule "run precheck before investigating" kept failing on
# self-classification, and the count makes the omission visible instead of relying on the
# moment's judgement. A reader deployment logs only into its own state.
if khms_may_write "$LOG"; then
  mkdir -p "$(dirname "$LOG")" && echo "$(date -Is) $*" >> "$LOG"
fi

PYTHONPATH="$KHMS_CODE${PYTHONPATH:+:$PYTHONPATH}" KHMS_KNOW="$ROOT/memory/know" python3 - "$@" <<'PY'
import khms_files as glob, os, re, sys, yaml
tags = set(sys.argv[1:])
hits = []
for path in sorted(glob.glob(os.path.join(os.environ["KHMS_KNOW"], "K-*.md"))):
    text = open(path, encoding="utf-8").read()
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
    if not m:
        continue
    meta = yaml.safe_load(m.group(1)); body = m.group(2).strip()
    if not (set(meta.get("tags") or []) & tags):
        continue
    warn = None
    if meta.get("status") in ("refuted", "challenged"):
        warn = f"⛔ {meta['status'].upper()}"
    elif "gotcha" in (meta.get("tags") or []) and meta.get("status") == "active":
        warn = "⚠ GOTCHA"
    elif meta.get("type") == "policy" and meta.get("status") == "active":
        warn = "📏 POLICY"
    if warn:
        first = body.splitlines()[0] if body else ""
        hits.append(f"{warn} {meta['id']}: {first}")
        for line in body.splitlines()[1:4]:
            hits.append(f"      {line}")
print("\n".join(hits) if hits else f"precheck: nothing on record for tags: {', '.join(sorted(tags))}")
PY
