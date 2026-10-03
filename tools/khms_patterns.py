#!/usr/bin/env python3
"""khms_patterns.py -- deployment-specific input vocabulary for the core's text classifiers.

The shipped classifiers (khms_lint.py, khms_triage.py) recognise English. A base whose cards are also written
in another language adds alternatives in the private file named by KHMS_PATTERNS (default
${KHMS_CONFIG_DIR}/patterns.env; KEY=VALUE grammar of the settings file, values are regular expressions and
are NOT expanded). Absent file = English only. Keys:

  TRIAGE_<NAME>=<alternation>     appended as `|<alternation>` inside the named classifier's group
                                  (NAME: ATTEMPT, APPROACH_REJECT, APPROACH_TOPIC, HEDGE, RESOLVED, FALSIFIER)
  LINT_RECORD_CTX=<alternation>   more words that mean "this line talks about the record"
  LINT_BLOCK_<n>=<label>::<regex> an extra unambiguous correction marker (case-sensitive), in file order
  LINT_CONTEXT_<n>=<label>::<regex> an extra correction phrase, matched on normalised (lower-case,
                                  diacritics stripped) lines that also talk about the record, in file order
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import khms_paths as P  # noqa: E402

_ITEMS = None


def _load():
    global _ITEMS
    if _ITEMS is None:
        _ITEMS = []
        try:
            path = P.get("KHMS_PATTERNS")
        except P.KhmsPathError:          # a library import with no base (lint of explicit files): defaults
            path = os.environ.get("KHMS_PATTERNS") or ""
        if path and os.path.isfile(path):
            with open(path, encoding="utf-8") as fh:
                _ITEMS = P.parse_config(fh.read(), path)
    return _ITEMS


def extra(key):
    """The configured alternation for `key`, as `|...` ready to append, or ''."""
    for k, v in _load():
        if k == key and v:
            return "|" + v
    return ""


def labelled(prefix):
    """[(label, compiled regex)] for every `<prefix><n>=<label>::<regex>` line, in file order."""
    out = []
    for k, v in _load():
        if k.startswith(prefix) and "::" in v:
            label, rx = v.split("::", 1)
            out.append((label, re.compile(rx)))
    return out
