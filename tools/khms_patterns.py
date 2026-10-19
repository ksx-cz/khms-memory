#!/usr/bin/env python3
"""khms_patterns.py -- deployment-specific input vocabulary for the core's text classifiers.

The shipped classifiers (khms_lint.py, khms_triage.py) recognise English. A base whose cards are also written
in another language adds alternatives in the private file named by KHMS_PATTERNS (default
${KHMS_CONFIG_DIR}/patterns.env; KEY=VALUE grammar of the settings file, values are regular expressions and
are NOT expanded). Absent file = English only. Keys:

  TRIAGE_<NAME>=<alternation>     appended as `|<alternation>` inside the named classifier's group
                                  (NAME: ATTEMPT, APPROACH_REJECT, APPROACH_TOPIC, HEDGE, RESOLVED, FALSIFIER)
  LINT_RECORD_REF=<alternation>   more forms of a DEFINITE card reference ("this card"), normalised
  LINT_BLOCK_<n>=<label>::<regex> an extra unambiguous correction marker (case-sensitive), in file order
  LINT_CONTEXT_<n>=<label>::<regex> an extra phrase stating a claim is false, matched on normalised (lower-case,
                                  diacritics stripped) lines that also name a card (K-id or definite reference)
  LINT_WEAK_<n>=<label>::<regex>  an extra word that names correcting/repairing as an activity as often as it
                                  performs one; counts only on a line with a DEFINITE card reference
  (LINT_RECORD_CTX — generic record words — is retired since #237: khms_lint.py says so on stderr.)
  HOOK_<NAME>=<value>             a matcher of khms_hook.py that REPLACES its English default
                                  (regex(), value()) or extends it (words()); khms_hook.py names each key:
                                  HOOK_DOMAIN, HOOK_PROPOSAL_MARKER,
                                  HOOK_PROPOSAL_REF, HOOK_PROPOSAL_CHOICE, HOOK_PROPOSAL_SKIP,
                                  HOOK_PROPOSAL_META, HOOK_PROPOSAL_STOPWORDS, HOOK_PROPOSAL_GENERIC
  OWNERQ_<NAME>=<value>           verify_owner_quotes.py: the principal's name forms and wording, self-test
                                  sentences (its docstring names each key)
  CONVERT_TAG_HINT_<n>=<tag>::<regex>  convert_inbox2.py: an extra surface form of a registry tag, in file order
  CONVERT_SCOPE_DEFAULT, CONVERT_SCOPE_AGENT, CONVERT_AGENT_TAGS  convert_inbox2.py: the base's scope names
                                  and the tags that mean "the agent's own machinery"
  The hook's input is normalised before matching (lower-case, diacritics stripped) where its docstring says so.
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
        items = []
        try:
            path = P.get("KHMS_PATTERNS")
        except P.KhmsNoBaseError:        # no base selected (lint of explicit files): defaults
            path = os.environ.get("KHMS_PATTERNS") or ""
        if path and os.path.isfile(path):
            with open(path, encoding="utf-8") as fh:
                items = P.parse_config(fh.read(), path)
        # Publish only a successful load: a parse/resolution error must stay an
        # error on subsequent calls rather than silently selecting defaults.
        _ITEMS = items
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


def raw_labelled(prefix):
    """[(label, raw value)] for every `<prefix><n>=<label>::<value>` line, in file order, NOT compiled --
    for callers that interpret their own flag markers (convert_inbox2.py's leading "(?-i)")."""
    return [tuple(v.split("::", 1)) for k, v in _load() if k.startswith(prefix) and "::" in v]


def value(key, default):
    """The configured value of `key` (a replacement), or `default`."""
    for k, v in _load():
        if k == key and v:
            return v
    return default


def regex(key, default, flags=0):
    """A compiled matcher: the configured pattern of `key` REPLACES the default pattern."""
    return re.compile(value(key, default), flags)


def words(key, default, sep=None):
    """A word list: the configured list of `key` (split on `sep`, whitespace by default) when set, else
    `default`. Callers that EXTEND a built-in list pass an empty default and add their own."""
    v = value(key, None)
    if v is None:
        return tuple(default)
    return tuple(w.strip() for w in v.split(sep) if w.strip())
