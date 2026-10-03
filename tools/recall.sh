#!/usr/bin/env bash
# KHMS symptom recall: free-text search over card BODIES (know/ + fog archive).
# Usage: recall.sh <symptom / error string / identifier ...>
#        recall.sh Remote I/O error mux channel
#        recall.sh --lexical <...>      # opt OUT of the dense channel
#
# WHY THIS EXISTS (2026-07-29, approved by the owner for immediate use):
# precheck.sh is TAG-based, so using it requires first classifying the situation
# ("I am now root-causing device X") — and that self-classification is exactly
# what failed in all four documented retrieval misses of 27.-29.7. (bed-IMU I2C,
# simware architecture, ZED-hub derived a third time, use_stamped_vel a second
# time). The symptom string — an error message, a parameter name, an odd number —
# is an artefact ALWAYS at hand. Paste it here before root-causing anything.
#
# 2026-08-04: the scoring core moved to khms_search.py, SHARED with the harness
# hook layer (khms_hook.py) so the CLI and the automatic injection cannot drift
# apart. CLI contract and output format unchanged; .recall.log lines gained a
# trailing "| src=cli" field (the hook logs src=hook:<event>).
#
# SCALE NOTE: a stateless scan is ~0.1 s at ~850 cards and needs no index that
# could go stale. When the base outgrows this (~10k cards), swap the internals
# for SQLite FTS5/BM25 — without changing the habit. Zero LLM tokens.
#
# 2026-08-09 (WP-E): the dense (embedding) channel arrived as OPT-IN, behind
# `--dense` / KHMS_DENSE=1.
#
# 2026-09-04: THE DEFAULT IS HYBRID. The opt-in was the defect. K-00542
# (Foxglove loads meshes Y-up, ROS is Z-up) had been in the base since
# 2026-07-29 and did not fire in three incidents; on the owner's Czech sentence
# the lexical channel puts it at rank 7 and the fused ranking at rank 1
# (K-02517; K-01867: an English base queried in Czech owes the system a working
# cross-lingual channel, and that channel was switched off by default).
# Everything now goes through khms_recall_hybrid.py, which uses the dense
# channel unless it is opted out of or the daemon does not answer in 150 ms:
#   recall.sh <...>              hybrid (default)
#   recall.sh --lexical <...>    lexical only  (also: KHMS_DENSE=0)
#   recall.sh --dense <...>      accepted, ignored — kept for old habits
# The first printed line always names the channel that ANSWERED, and the
# .recall.log line carries it too (src=cli:hybrid:bge / src=cli:lexical:fallback
# / src=cli:lexical:optout), so a silent fail-back to the channel that caused
# the miss is countable instead of invisible (K-00453).
set -euo pipefail
[ $# -ge 1 ] || { echo "usage: recall.sh [--lexical] <symptom/error/identifier ...>" >&2; exit 2; }
exec python3 "$(dirname "$(readlink -f "$0")")/khms_recall_hybrid.py" "$@"
