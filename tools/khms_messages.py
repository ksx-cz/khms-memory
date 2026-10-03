#!/usr/bin/env python3
"""khms_messages.py -- user-visible wording of the core, English by default, overridable per deployment.

The shipped strings are English. A deployment that speaks another language (or wants other wording)
puts KEY=VALUE lines into the file named by KHMS_MESSAGES (default ${KHMS_CONFIG_DIR}/messages.env; the
same grammar as the settings file, no `${...}` expansion needed). That file is private configuration and
never enters this repository. A key the file does not set keeps its English default; an unknown key in the
file is ignored, and `khms_messages.py --check` lists both directions.

JOB_MARKERS is a `|`-separated list of prompt prefixes that identify the memory pipeline's own prompts.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import khms_paths as P  # noqa: E402

DEFAULTS = {
    "GLOSSARY_LABEL": "(glossary: +{terms})",
    "NOT_IN_BASE": "(not in the base)",
    "SELECT_HEADER": "KHMS (auto-recall -- cards relevant to what is being worked on):",
    "SELECT_FOOTER": "If a card is relevant: open it whole (memory/know/<id>.md) and follow its links.",
    "REPORT_DIRECTIVE_FOUND": "see the cards above",
    "NOTHING_ABOVE_BAR_CAPPED": "nothing above the bar (rate cap) -- the base is silent on this",
    "NOTHING_ABOVE_BAR": "nothing above the bar -- the base is silent on this",
    "NOTHING_NEW": "nothing new (the relevant cards were already shown in this session)",
    "JOB_MARKERS": ("# Nightly sweep|# Weekly synthesis|# KHMS consolidation instructions"
                    "|# KHMS history-extraction instructions|[AUTO-PING] KHMS review"),
}

_LOADED = None


def _load():
    global _LOADED
    if _LOADED is None:
        _LOADED = dict(DEFAULTS)
        try:
            path = P.get("KHMS_MESSAGES")
        except P.KhmsNoBaseError:        # a library import with no base selected: English defaults
            path = os.environ.get("KHMS_MESSAGES") or ""
        if path and os.path.isfile(path):
            with open(path, encoding="utf-8") as fh:
                for k, v in P.parse_config(fh.read(), path):
                    if k in DEFAULTS:
                        _LOADED[k] = v
    return _LOADED


def msg(key, **kw):
    text = _load()[key]
    return text.format(**kw) if kw else text


def job_markers():
    return tuple(m for m in msg("JOB_MARKERS").split("|") if m)


if __name__ == "__main__":
    if sys.argv[1:2] == ["--check"]:
        path = P.get("KHMS_MESSAGES")
        given = {}
        if path and os.path.isfile(path):
            with open(path, encoding="utf-8") as fh:
                given = dict(P.parse_config(fh.read(), path))
        for k in DEFAULTS:
            print(f"{'override' if k in given else 'default '} {k}")
        for k in sorted(set(given) - set(DEFAULTS)):
            print(f"UNKNOWN  {k}")
        sys.exit(0)
    print(__doc__.strip().split("\n\n")[0])
