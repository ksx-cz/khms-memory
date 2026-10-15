#!/usr/bin/env python3
"""khms_messages.py -- user-visible wording of the core, English by default, overridable per deployment.

The shipped strings are English. A deployment that speaks another language (or wants other wording)
puts KEY=VALUE lines into the file named by KHMS_MESSAGES (default ${KHMS_CONFIG_DIR}/messages.env; the
same grammar as the settings file, no `${...}` expansion needed). That file is private configuration and
never enters this repository. A key the file does not set keeps its English default; an unknown key in the
file is ignored, and `khms_messages.py --check` lists both directions.

JOB_MARKERS is a `|`-separated list of prompt prefixes that identify the memory pipeline's own prompts.
A value may contain `\n` for a line break (the file grammar is one line per key). A value that must keep a
leading or trailing space is written in quotes. Placeholders are `{name}` (str.format).

A PATH IS NEVER A LITERAL (#239). A placeholder named after a setting -- `{KHMS_NIGHT_LOG}`, `{KHMS_NIGHTLY_LOCK}`,
any `{KHMS_*}` -- is filled from the resolved configuration when the caller does not pass it. On 2026-10-15 the
morning ping sent its reader to `logs/nightly.log`, a file nothing had written since the log moved five days
earlier; the wording had copied the path instead of naming the setting. `--check` fails (rc 1) on a template that
spells out a file name one of the path settings owns.
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import khms_paths as P  # noqa: E402

DEFAULTS = {
    "GLOSSARY_LABEL": "(glossary: +{terms})",
    "NOT_IN_BASE": "(not in the base)",
    # card_validity.py proposals: the route of a card the judge found not to be know-how
    "VALIDITY_RETIRE_CANDIDATE": "retire candidate (a retired status is not designed yet)",
    "SELECT_HEADER": "KHMS (auto-recall -- cards relevant to what is being worked on):",
    "SELECT_FOOTER": "If a card is relevant: open it whole (memory/know/<id>.md) and follow its links.",
    # card-line prefixes of an injection: the principle umbrella served beside a card, the corrector served
    # in full beside a refuted/superseded card, and the one-line pointer to a corrector of an active card
    "SELECT_UMBRELLA_PREFIX": "- principle: ",
    "SELECT_CORRECTED_BY_PREFIX": "  -> corrected by: ",
    "SELECT_CORRECTOR_POINTER": "  ! CORRECTED BY {id}: {first}",
    "REPORT_DIRECTIVE_FOUND": "see the cards above",
    # mark_ledger.status_line: the coverage ledger's one-line state ({suggested} and {age} are the two
    # optional parts below, already formatted, or empty)
    # khms_review_ping.sh: the morning ping. {pending} is the list of what waits; the rest are its parts.
    "PING_REVIEW": ("[AUTO-PING] Morning KHMS review: memory/inbox/ holds unreviewed proposals -- {pending}. "
                    "Check whether they are already reviewed (cards in know/); if not, review each unit on its "
                    "own: (1) tools/convert_inbox2.py memory/inbox/<unit> writes <unit>.approve.md and a report; "
                    "(2) resolve what the report and the inbox's ## Flagged section flag; (3) tools/approve_inbox.py "
                    "on the corrected .approve.md (it writes the unit's review marker); (4) tools/build_views.py. "
                    "A unit that yields no card is closed with tools/mark_ledger.py mark-reviewed memory/inbox/<unit> "
                    "--reason '...'; anything appended after a review reopens the unit. A contradiction triage "
                    "(DATE-triage.md) is a review of its own: add a '## REVIEW VERDICT' section with lines "
                    "'ROW n: APPLY refute|challenge|contradicts / REJECT reason', run tools/apply_triage.py "
                    "memory/inbox/DATE-triage.md --dry-run and only after checking it --apply. No hand edits of "
                    "frontmatter. Then report to {principal}.{zero_note}"),
    "PING_ZERO_NOTE": (" !!! WARNING: the sweep wrote a triage file but JUDGED NOT ONE PAIR (0 judged). That file "
                       "is not a result, it is the record of a night that did not happen -- 'no contradictions' in "
                       "it does not mean there are none. FIRST run the sweep again (tools/nightly_triage.sh) and "
                       "check that the judge answers at all; only then review the triage."),
    "PING_NIGHT_RUNNING": ("THE NIGHT IS STILL RUNNING (since {hm}, night {nk}) -- review only finished units; in "
                           "progress: {wip}; the rest after the 'nightly done' / 'nightly INCOMPLETE' line of "
                           "{KHMS_NIGHT_LOG}"),
    "PING_NONE": "none",
    "PING_NIGHT_UNKNOWN": ("NIGHT STATE UNKNOWN ({line}) -- before reviewing, check in {KHMS_NIGHT_LOG} that the last "
                           "'nightly start' has its 'nightly done'"),
    "PING_NEVER": "never",
    "PING_NIGHT_MISSED": "THE NIGHT RUN DID NOT FINISH (last: {last})",
    "PING_SWEEP_FAILED": ("THE NIGHT SWEEP FAILED {n} TIMES IN A ROW (since {first}; last: {last}) -- the window "
                          "grows every night and the same failure will repeat; find the cause in {KHMS_NIGHT_LOG} "
                          "('sweep part' / 'FAILED' lines)"),
    # the LAST failed sweep stopped on the account's usage limit (its sweep-failed line says reason=usage-limit)
    "PING_SWEEP_LIMIT": ("THE NIGHT SWEEP FAILED {n} TIMES IN A ROW (since {first}; last: {last}) -- the last one "
                         "stopped on the account's usage limit, which is transient: the night needs a re-run after "
                         "the limit resets, not a fix ('USAGE LIMIT' lines in {KHMS_NIGHT_LOG})"),
    # the last night's MARK stage stopped at its dollar cap (#243; permark.py's `MARK stage stopped at the $ cap`)
    "PING_MARK_CAP": ("THE MARK STAGE STOPPED AT ITS $ CAP ({spent} of {cap}); {n} mark(s) stay OPEN for the next "
                      "night -- not a failure, the cap is MARK_BUDGET_USD ('MARK stage stopped' in {KHMS_NIGHT_LOG})"),
    # the daily card-validity judge (#219, khms_validity_daily.sh): a batch whose parts wait for verification
    # (until {dir}/APPLIED exists), a last run that failed, and the last status line riding on the message
    "PING_VALIDITY_READY": ("validity batch {date}: {n} parts ready for verification ({dir}/proposals-part*.md; "
                            "write {dir}/APPLIED once applied -- the next batch waits for it)"),
    "PING_VALIDITY_FAILED": "THE DAILY CARD-VALIDITY RUN FAILED ({line}; see {KHMS_NIGHT_LOG})",
    "PING_VALIDITY_LAST": "LAST CARD-VALIDITY RUN: {line}",
    "PING_WEEK_MISSED": "THE WEEKLY RUN DID NOT FINISH (last: {last})",
    # KHMS_WEEK_PAUSED is set: the weekly is not failing, it is paused on purpose
    "PING_WEEK_PAUSED": "THE WEEKLY RUN IS PAUSED ({reason}; last: {last})",
    "PING_EXTRA_ONLY": "[AUTO-PING] Project check: {extra}. Then report to {principal}.",
    "PING_EXTRA_FAILED": "PROJECT CHECK DID NOT ANSWER (rc={rc}, {cmd}): {out}",
    "PING_EXTRA_CLEAN": "clean (nothing to report)",
    "PING_MODELS": "LAST NIGHT'S MODELS (from each call's own JSON result): {models}",
    "PING_INBOX_CLEAN": "(inbox clean)",
    "LEDGER_NO_STATE": "MARK LEDGER: no state -- run mark_ledger.py sync",
    "LEDGER_STATUS": ("MARK LEDGER (since {floor}): OPEN {open} (queued {queued}, proposed in an unreviewed "
                      "inbox/carry {proposed}{suggested}) · CARDED {carded} · DISMISSED {dismissed} · carry "
                      "{carry} batch(es) · {oldest}{age} · state of {generated} · scope: all MARK kinds except "
                      "the status kinds since {floor} -- not covered: status MARKs, journals before the floor, "
                      "knowledge without a MARK line"),
    # appended after LEDGER_STATUS / HOOK_LEDGER_LINE (outside them, so a private wording cannot hide it)
    "LEDGER_STATUS_SKIPPED": " · status lines skipped, not tracked: {n}{kinds}",
    "LEDGER_SUGGESTED": ", with a card suggestion {n}",
    "LEDGER_AGE": " (age {days} d)",
    "LEDGER_OLDEST": "oldest OPEN {id} {date} {time} [{kind}] {text}",
    "LEDGER_NONE_OPEN": "none OPEN",
    "NOTHING_ABOVE_BAR_CAPPED": "nothing above the bar (rate cap) -- the base is silent on this",
    "NOTHING_ABOVE_BAR": "nothing above the bar -- the base is silent on this",
    "NOTHING_NEW": "nothing new (the relevant cards were already shown in this session)",
    "JOB_MARKERS": ("# Nightly sweep|# Weekly synthesis|# KHMS consolidation instructions"
                    "|# KHMS history-extraction instructions|[AUTO-PING] KHMS review"),
    # --- khms_hook.py -------------------------------------------------------------------------------
    "HOOK_DIRECTIVE_LINE": ("REQUIRED: base: <what I searched for -> the cards, or 'did not search - why'>"
                            " \u00b7 verified: <the command or file:line whose output I read | unverified>"
                            " \u00b7 if I were wrong: <how that output would have looked different>"
                            "  (definition: tools/prompts/report_directive.md)"),
    "HOOK_LEDGER_NONE": "MARK LEDGER: no state ({exc}) -- run tools/mark_ledger.py sync",
    "HOOK_LEDGER_OLDEST": "oldest OPEN {id} {date} {time} [{kind}] {text}",
    "HOOK_LEDGER_NO_OPEN": "no OPEN",
    "HOOK_LEDGER_SUGGESTED": ", with a card proposal {n} -- a proposal is not coverage, `mark_ledger.py suggest`",
    "HOOK_LEDGER_LINE": ("MARK LEDGER (since {floor}): OPEN {open} (queued {queued}, in the unreviewed inbox/carry "
                         "{proposed}{suggested}) \u00b7 CARDED {carded} \u00b7 DISMISSED {dismissed} \u00b7 carry "
                         "{carry} batches \u00b7 {oldest} \u00b7 state of {generated} \u00b7 scope: all MARK "
                         "kinds except the status kinds since {floor}; outside it: status MARKs, journals "
                         "before the floor, "
                         "findings without a MARK line -- OPEN 0 does not mean \"nothing was lost\""),
    "HOOK_LEDGER_SKIPPED": " \u00b7 status lines skipped, not tracked: {n}{kinds}",
    "HOOK_LEDGER_ERROR": "MARK LEDGER: report failed ({exc}) -- run tools/mark_ledger.py status",
    # #236: a journal write that adds a status-kind MARK line is refused (khms_hook.py, journal_marks.py)
    "HOOK_STATUS_MARK_DENY": ("status lines are plain lines without MARK (owner 2026-10-13). Refused: {lines}. "
                              "Write the line without `MARK[...]` (e.g. `- HH:MM agent-done <what>`); a MARK is "
                              "only for know-how (a finding, refutation, measurement, decision, mistake)."),
    "HOOK_INBOX_UNREVIEWED": ("KHMS: UNREVIEWED INBOX: {files} -- do the review (approve_inbox.py, "
                              "build_views.py, report) BEFORE other work."),
    "HOOK_INBOX_NIGHT_UNKNOWN": (" (Night pipeline state unknown: {exc} -- before you start, check in "
                                 "{KHMS_NIGHT_LOG} that the last `nightly start` has its `nightly done`.)"),
    "HOOK_NIGHT_RUNNING": ("KHMS: THE NIGHT PIPELINE IS RUNNING (since {start}{night}; it holds the night lock)"
                           " -- the inbox is not complete yet."),
    "HOOK_NIGHT_KEY": ", night {nk}",
    "HOOK_NIGHT_READY": " Ready for review now: {files} -- review them BEFORE other work.",
    "HOOK_NIGHT_NOTHING_READY": (" Nothing is ready for review yet; the review waits for the `nightly done` (or "
                                 "`nightly INCOMPLETE`) line in {KHMS_NIGHT_LOG}."),
    "HOOK_NIGHT_WIP": " Written by the running night (review them after it ends): {files}.",
    "HOOK_PRECHECK_HEAD": "KHMS precheck (risky command: {tags}) -- active policies, gotchas and refuted dead ends:",
    "HOOK_PRECHECK_FOOT": "If any of it applies, open the card whole before running the command.",
    "HOOK_CLAIM_DENY_CITE": ("base: cites cards without a recall in this turn -- run `tools/recall.sh "
                             "'<query>'` and send again, or write `base: did not search - <why>`. (reason: {why};"
                             " checked: the src=cli lines of the recall log since this report turn began)"),
    "HOOK_CLAIM_DENY_MISSING": ("The report lacks the required `base:` line ({why}). Write `base: <query of "
                                "tools/recall.sh> -> <cards>`, or honestly `base: did not search - <why>`; both "
                                "are valid, silence is not. Definition: tools/prompts/report_directive.md"),
    "HOOK_WHY_NO_LINE": "no claim line ({n} chars)",
    "HOOK_WHY_SHORT": "short reply",
    "HOOK_WHY_NOT_A_TURN": "outside a report turn",
    "HOOK_WHY_HONEST": "honest form ({form})",
    "HOOK_WHY_NO_IDS": "line without card ids",
    "HOOK_WHY_NO_TURN": "no report turn in this session",
    "HOOK_WHY_NO_DISPATCH_TURN": "no turn in this session",
    "HOOK_WHY_NO_RECALL": "no src=cli recall in this turn",
    "HOOK_WHY_NO_QUERY": "no query, but a recall ran in this turn",
    "HOOK_WHY_MATCH": "{pct}% match with a recall of this turn",
    "HOOK_WHY_LOW_MATCH": "best match with a recall of this turn only {pct}%",
    "HOOK_WHY_NO_PROPOSAL": "no proposal",
    "HOOK_WHY_UNCOVERED": ("{n} of {total} proposals without a bound recall on their subject (bound recalls "
                           "in this turn: {bound})"),
    "HOOK_WHY_COVERED": "{n} proposals: recall evidenced (bound to this session: {how}), relevance heuristic",
    "HOOK_WHY_DISPATCH_UNCOVERED": "{n} proposals without a bound recall on their subject",
    "HOOK_WHY_RELATIVE": "relative path without cwd",
    "HOOK_WHY_BRIEF_UNREADABLE": "{what}: brief unreadable ({err}, fail-open)",
    "HOOK_HOW_OWN": "own",
    "HOOK_HOW_SUBAGENT": "subagent {agent}, result cited",
    "HOOK_PROPOSAL_DENY": ("The report carries a proposal the base was not asked about in this report turn "
                           "({why}). A recall on the symptom alone is not enough -- the base may know the answer "
                           "to the proposal itself. For each proposal run a recall with its own words, read the "
                           "result and send again:\n{items}\n(counted: only recall.sh calls of THIS session and "
                           "turn that finished without an error, per the session transcript; a subagent's recall "
                           "only through its own transcript and only when the report cites a card that call "
                           "returned; the recall must share at least two words of the proposal, one of them a "
                           "word the rest of the report does not use)"),
    "HOOK_DISPATCH_DENY": ("The brief carries a proposal the base was not asked about in this turn ({why}; "
                           "where: {where}). The agent will carry the proposal out as a settled decision, and the "
                           "base may know the answer to the proposal itself. For each proposal run a recall with "
                           "its own words, read the result and send the brief again:\n{items}\n(counted: only "
                           "recall.sh calls of THIS session since this turn began that finished without an error, "
                           "per the session transcript; a subagent's recall only through its own transcript and "
                           "only when the brief cites a card that call returned; the recall must share at least "
                           "two words of the proposal, one of them a word the rest of the brief does not use)"),
    "HOOK_PROPOSAL_ITEM": "- proposal \"{label}...\" -> `tools/recall.sh \"{query}\"`",
    "HOOK_GATE_PROPOSALS": "proposals",
    "HOOK_GATE_STOP": "Stop",
    "HOOK_FAILOPEN": ("GATES FAILED OPEN: {n} ERROR->ALLOW record(s) ({gates}) -- a gate that failed looks idle. "
                      "Last at {hhmm}, gate {gate}: {why} ({path}). Investigate and fix at the source."),
    "HOOK_STOP_REASON": ("REPORT OWED: the principal's last message came over the report channel (chat_id="
                         "{chat_id}, message_id={message_id}) and no successful report-tool call ({tool}) "
                         "followed it in this turn -- answer there; the principal does not see the terminal."),
    # khms_contradict.py / khms_triage_report.py: the words that say what the base is about, in the judge's
    # instructions ("You audit <domain> for CONTRADICTIONS") and the subject check ("... notes about <scope>").
    "TRIAGE_KB_DOMAIN": "an engineering knowledge base",
    "TRIAGE_SUBJECT_SCOPE": "one project",
}

_LOADED = None


def _load():
    global _LOADED
    if _LOADED is None:
        loaded = dict(DEFAULTS)
        try:
            path = P.get("KHMS_MESSAGES")
        except P.KhmsNoBaseError:        # a library import with no base selected: English defaults
            path = os.environ.get("KHMS_MESSAGES") or ""
        if path and os.path.isfile(path):
            with open(path, encoding="utf-8") as fh:
                for k, v in P.parse_config(fh.read(), path):
                    if k in DEFAULTS:
                        loaded[k] = v
        # Cache only successful loads; invalid configuration never means defaults.
        _LOADED = loaded
    return _LOADED


_SETTING_FIELD = re.compile(r"\{(KHMS_[A-Z0-9_]+)\}")


def _setting(key):
    try:
        return P.get(key)
    except P.KhmsNoBaseError:        # a library import with no base selected: name the setting
        return os.environ.get(key) or "$" + key


def msg(key, **kw):
    text = _load()[key].replace("\\n", "\n")
    for name in set(_SETTING_FIELD.findall(text)) - set(kw):
        kw[name] = _setting(name)
    return text.format(**kw) if kw else text


def path_file_names():
    """-> the file names the path settings own (`nightly.log`, `nightly.lock`, `recall.log` ...)."""
    names = set()
    for _key, default in P.DEFAULTS:
        if default and default.startswith("${") and "/" in default:
            name = default.rsplit("/", 1)[1]
            if "." in name or "-" in name:
                names.add(name)
    return names


def literal_paths(text):
    """-> the owned file names `text` spells out instead of naming their `{KHMS_*}` setting."""
    plain = re.sub(r"\{[A-Za-z0-9_]+\}", " ", text)
    return sorted(n for n in path_file_names()
                  if re.search(r"(?<![\w-])\.?" + re.escape(n) + r"(?![\w-])", plain))


def job_markers():
    return tuple(m for m in msg("JOB_MARKERS").split("|") if m)


if __name__ == "__main__":
    if sys.argv[1:2] == ["get"] and len(sys.argv) >= 3:
        # `khms_messages.py get KEY [name=value ...]` -- one message for a shell caller, placeholders filled
        try:
            print(msg(sys.argv[2], **dict(a.split("=", 1) for a in sys.argv[3:])))
        except (KeyError, ValueError, IndexError) as e:
            sys.exit(f"khms_messages: cannot render {sys.argv[2]}: {e!r}")
        sys.exit(0)
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
        bad = 0
        for k, v in sorted(dict(DEFAULTS, **{k: v for k, v in given.items() if k in DEFAULTS}).items()):
            lit = literal_paths(v)
            if lit:
                bad += 1
                print(f"LITERAL-PATH {k}: names {', '.join(lit)} -- write the setting's placeholder "
                      f"({{KHMS_...}}) instead")
        sys.exit(1 if bad else 0)
    print(__doc__.strip().split("\n\n")[0])
