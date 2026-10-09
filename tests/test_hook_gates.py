#!/usr/bin/env python3
"""The hook's gates and SessionStart lines, end to end through the real binary, in English.

Every case runs tools/khms_hook.py as a subprocess against a temporary base made by khms_init.sh, with
the shipped English wording and matchers (the conftest strips every KHMS_* variable). Covered here:

  * PROPOSAL GATE: searches relevant cards, shows new results once per author context.
    An unrelated transcript recall is not a substitute; no-hit is explicitly logged.
  * DISPATCH GATE on the Agent tool: the same rule for a brief; an unreadable brief fails OPEN, and the
    fail-open is logged and shown at the next SessionStart (a broken gate must not look like an idle one).
  * STOP GATE: the principal's last report-channel message is owed an answer; a terminal prompt, a
    subagent, an active continuation and a successful reply all let the turn end.
  * KILL SWITCH (the file form), PRECHECK on a risky command, NIGHT-RUNNING via the lock-table seam.
  * MESSAGE CATALOGUE: every key the code asks for exists in the English defaults, and a deployment's
    messages file replaces the wording (here: the corrector pointer of an injection).

The claim gate (`base:` line) and the directive are covered by test_hook_directive.py.
"""
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

TOOLS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools")
HOOK = os.path.join(TOOLS, "khms_hook.py")
INIT = os.path.join(TOOLS, "khms_init.sh")
REPLY_TOOL = "mcp__plugin_chat_chat__reply"     # matched by KHMS_REPORT_TOOL below
SERVER = "plugin:chat:chat"                     # matched by KHMS_REPORT_SERVER below
HONEST = "base: did not search - the question is about wording only"


def iso(t):
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).isoformat().replace("+00:00", "Z")


class HookCase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="hookgates-")
        self.addCleanup(shutil.rmtree, self.root, True)
        subprocess.run([INIT, self.root], capture_output=True, text=True, check=True)
        self.state = os.path.join(self.root, "state")
        self.transcript = os.path.join(self.root, "session.jsonl")
        open(self.transcript, "w").close()

    def env(self, **extra):
        env = dict(os.environ, KHMS_ROOT=self.root, KHMS_REPORT_TOOL=r"(?i)chat.*reply",
                   KHMS_REPORT_SERVER="chat")
        env.update(extra)
        return env

    def fire(self, payload, **extra):
        r = subprocess.run([sys.executable, HOOK], input=json.dumps(payload), capture_output=True,
                           text=True, env=self.env(**extra), timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        out = r.stdout.strip()
        return json.loads(out) if out else None

    def context(self, payload, **extra):
        return ((self.fire(payload, **extra) or {}).get("hookSpecificOutput") or {}).get(
            "additionalContext", "")

    def denied(self, got):
        hso = (got or {}).get("hookSpecificOutput") or {}
        return hso.get("permissionDecision") == "deny", hso.get("permissionDecisionReason", "")

    def read_log(self, name):
        p = os.path.join(self.state, name)
        return open(p, encoding="utf-8").read() if os.path.exists(p) else ""

    def prompt(self, text="please look at the shielded cable harness question", session="s1"):
        return self.fire({"hook_event_name": "UserPromptSubmit", "prompt": text, "session_id": session})

    def report(self, text, session="s1"):
        return {"hook_event_name": "PreToolUse", "tool_name": REPLY_TOOL, "tool_input": {"text": text},
                "session_id": session, "transcript_path": self.transcript}

    def transcript_add(self, *entries):
        with open(self.transcript, "a", encoding="utf-8") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")

    def recall_in_transcript(self, query, tid="toolu_r1", ok=True, t=None):
        t = time.time() + 1 if t is None else t
        self.transcript_add(
            {"type": "assistant", "timestamp": iso(t), "message": {"content": [
                {"type": "tool_use", "id": tid, "name": "Bash",
                 "input": {"command": "tools/recall.sh '%s'" % query}}]}},
            {"type": "user", "timestamp": iso(t), "message": {"content": [
                {"type": "tool_result", "tool_use_id": tid, "is_error": not ok,
                 "content": "K-90001 (0.03): a card"}]}})


# --- the proposal gate on a report ---------------------------------------------------------------------
PROPOSAL = (HONEST + "\n\nThe harness picks up noise from the motor leads.\n"
            "I recommend we replace the sensor harness with shielded twisted pair wire.\n")


class ProposalGate(HookCase):
    def setUp(self):
        super().setUp()
        path = os.path.join(self.root, 'memory', 'know', 'K-%05d.md' % 90001)
        with open(path, 'w') as f:
            f.write('---\nid: K-%05d\ntype: fact\nlevel: observation\nstatus: active\n'
                    'tags: [testing]\nscope: universal\ndate: 2026-01-01\n---\n'
                    '# Sensor harness and gyroscope calibration\n'
                    'Shielded twisted pair sensor harness reduces motor leads noise. '
                    'Rewrite gyroscope calibration routine only after measuring.\n' % 90001)

    def test_a_recommendation_without_a_recall_on_it_is_denied(self):
        self.prompt()
        deny, why = self.denied(self.fire(self.report(PROPOSAL)))
        self.assertTrue(deny)
        self.assertIn("recall", why)
        self.assertIn("shielded", why)          # the refusal names the query to run
        self.assertIn("| DENY |", self.read_log("proposal_gate.log"))

    def test_a_proposal_citing_the_recalled_card_is_known(self):
        self.prompt()
        self.recall_in_transcript("shielded twisted pair sensor harness")
        deny, why = self.denied(self.fire(self.report(PROPOSAL + "\nRead " + ("K-%05d" % 90001))))
        self.assertFalse(deny, why)
        self.assertIn("| ALLOW-KNOWN |", self.read_log("proposal_gate.log"))

    def test_a_failed_recall_does_not_count(self):
        self.prompt()
        self.recall_in_transcript("shielded twisted pair sensor harness", ok=False)
        deny, _ = self.denied(self.fire(self.report(PROPOSAL)))
        self.assertTrue(deny)

    def test_a_recall_on_the_symptom_only_does_not_cover_the_remedy(self):
        self.prompt()
        self.recall_in_transcript("motor leads noise")
        deny, _ = self.denied(self.fire(self.report(PROPOSAL)))
        self.assertTrue(deny)

    def test_a_report_without_a_proposal_is_not_gated(self):
        self.prompt()
        deny, _ = self.denied(self.fire(self.report(HONEST + "\n\nThe harness was checked; it is fine.\n")))
        self.assertFalse(deny)

    def test_same_proposal_is_once_per_author_and_resets_on_session_start(self):
        self.prompt()
        payload = self.report(PROPOSAL)
        self.assertTrue(self.denied(self.fire(payload))[0])
        self.assertFalse(self.denied(self.fire(payload))[0])
        payload['agent_id'] = 'other-author'
        self.assertTrue(self.denied(self.fire(payload))[0])
        self.fire({'hook_event_name': 'SessionStart', 'session_id': 's1'})
        self.assertTrue(self.denied(self.fire(payload))[0])

    def test_terminology_gate_requires_edit_not_a_repeated_send(self):
        glossary = os.path.join(self.root, 'terms.txt')
        with open(glossary, 'w') as f:
            f.write('smoky ride => smoke test run\n')
        for _ in range(2):
            deny, why = self.denied(self.fire(self.report(HONEST + '\nA smoky ride.'),
                                            KHMS_TERM_GLOSSARY=glossary))
            self.assertTrue(deny)
            self.assertIn('smoke test run', why)
        self.assertFalse(self.denied(self.fire(self.report(HONEST + '\nA smoke test run.'),
                                              KHMS_TERM_GLOSSARY=glossary))[0])

    def test_the_gate_can_be_switched_off(self):
        with open(os.path.join(self.root, "config", "khms_experiment.json"), "w") as f:
            json.dump({"proposal_gate": False}, f)
        self.prompt()
        deny, _ = self.denied(self.fire(self.report(PROPOSAL)))
        self.assertFalse(deny)


# --- the dispatch gate ---------------------------------------------------------------------------------
class DispatchGate(HookCase):
    def setUp(self):
        super().setUp()
        path = os.path.join(self.root, 'memory', 'know', 'K-%05d.md' % 90001)
        with open(path, 'w') as f:
            f.write('---\nid: K-%05d\ntype: fact\nlevel: observation\nstatus: active\n'
                    'tags: [testing]\nscope: universal\ndate: 2026-01-01\n---\n'
                    '# Sensor harness and gyroscope calibration\n'
                    'Shielded twisted pair sensor harness reduces motor leads noise. '
                    'Rewrite gyroscope calibration routine only after measuring.\n' % 90001)

    def dispatch(self, prompt, session="s1"):
        return {"hook_event_name": "PreToolUse", "tool_name": "Agent", "session_id": session,
                "cwd": self.root, "transcript_path": self.transcript,
                "tool_input": {"description": "build it", "subagent_type": "programmer", "prompt": prompt}}

    def test_a_proposal_in_the_brief_without_a_recall_is_denied(self):
        brief = os.path.join(self.root, "brief.md")
        with open(brief, "w") as f:
            f.write("# Brief\nI propose that the agent rewrites the calibration routine of the gyroscope.\n")
        self.prompt()
        deny, why = self.denied(self.fire(self.dispatch("Your brief is %s - do exactly that." % brief)))
        self.assertTrue(deny)
        self.assertIn(brief, why)

    def test_a_brief_citing_the_recalled_card_is_known(self):
        brief = os.path.join(self.root, "brief.md")
        with open(brief, "w") as f:
            f.write("# Brief\nI propose that the agent rewrites the calibration routine of the gyroscope.\n")
        self.prompt()
        self.recall_in_transcript("gyroscope calibration routine rewrite")
        deny, why = self.denied(self.fire(self.dispatch("Read K-90001. Your brief is %s - do exactly that." % brief)))
        self.assertFalse(deny, why)

    def test_an_unreadable_brief_fails_open_and_is_shown_at_the_next_session_start(self):
        self.prompt()
        missing = os.path.join(self.root, "nowhere", "brief.md")
        deny, _ = self.denied(self.fire(self.dispatch("Your brief is %s." % missing)))
        self.assertFalse(deny)
        self.assertIn("ERROR->ALLOW", self.read_log("proposal_gate.log"))
        ctx = self.context({"hook_event_name": "SessionStart", "session_id": "s2"})
        self.assertIn("ERROR->ALLOW", ctx)

    def test_no_turn_still_shows_new_relevant_cards(self):
        deny, _ = self.denied(self.fire(self.dispatch("I propose rewriting the gyroscope calibration.")))
        self.assertTrue(deny)
        self.assertIn("| DENY |", self.read_log("proposal_gate.log"))


# --- the Stop gate ---------------------------------------------------------------------------------------
class StopGate(HookCase):
    def channel_message(self, t=None):
        t = time.time() - 5 if t is None else t
        self.transcript_add({"type": "user", "isMeta": True, "timestamp": iso(t),
                             "origin": {"kind": "channel", "server": SERVER},
                             "message": {"content": '<channel source="%s" chat_id="42" message_id="7">'
                                                    'how is the harness?</channel>' % SERVER}})

    def reply(self, ok=True, tid="toolu_x"):
        self.transcript_add(
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": tid, "name": REPLY_TOOL, "input": {"text": "fine"}}]}},
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": tid, "is_error": not ok, "content": "sent"}]}})

    def stop(self, **extra):
        p = {"hook_event_name": "Stop", "session_id": "s1", "transcript_path": self.transcript}
        p.update(extra)
        return self.fire(p)

    def test_an_unanswered_channel_message_blocks_the_stop(self):
        self.channel_message()
        got = self.stop()
        self.assertEqual((got or {}).get("decision"), "block")
        self.assertIn("chat_id=42", got["reason"])
        self.assertIn("| BLOCK |", self.read_log("stop_gate.log"))

    def test_a_successful_reply_lets_the_turn_end(self):
        self.channel_message()
        self.reply()
        self.assertIsNone(self.stop())
        self.assertIn("| ALLOW | replied", self.read_log("stop_gate.log"))

    def test_a_failed_reply_still_owes_one(self):
        self.channel_message()
        self.reply(ok=False)
        self.assertEqual((self.stop() or {}).get("decision"), "block")

    def test_a_terminal_prompt_owes_nothing(self):
        self.channel_message()
        self.transcript_add({"type": "user", "origin": {"kind": "human"},
                             "message": {"content": "carry on in the terminal"}})
        self.assertIsNone(self.stop())

    def test_a_subagent_and_an_active_continuation_are_let_through(self):
        self.channel_message()
        self.assertIsNone(self.stop(agent_id="abc"))
        self.assertIsNone(self.stop(stop_hook_active=True))
        log = self.read_log("stop_gate.log")
        self.assertIn("subagent", log)
        self.assertIn("stop_hook_active", log)

    def test_a_reply_the_transcript_does_not_show_yet_counts(self):
        """The report tool's PreToolUse stamps the session; the Stop gate trusts that stamp over a
        transcript that has not caught up."""
        self.channel_message()
        self.fire(self.report("fine"))
        self.assertIsNone(self.stop())
        self.assertIn("transcript lags", self.read_log("stop_gate.log"))


# --- the kill switch, precheck, night running ----------------------------------------------------------
class SwitchesAndContext(HookCase):
    def test_the_kill_switch_file_silences_every_event(self):
        open(os.path.join(self.state, "hooks-off"), "w").close()
        self.assertIsNone(self.fire({"hook_event_name": "SessionStart", "session_id": "s1"}))
        self.assertIsNone(self.prompt())

    def card(self, cid, tags, typ="policy", first="Never delete a deployment tree without a dry run."):
        with open(os.path.join(self.root, "memory", "know", cid + ".md"), "w", encoding="utf-8") as f:
            f.write("---\nid: %s\ntype: %s\nlevel: observation\nstatus: active\ntags: [%s]\n"
                    "scope: universal\ndate: 2026-01-01\nlinks:\n  derived_from: []\n  supports: []\n"
                    "  contradicts: []\n  refuted_by: []\n  supersedes: null\n---\n%s\n\nbody\n"
                    % (cid, typ, ", ".join(tags), first))

    def bash(self, cmd, session="s1"):
        return {"hook_event_name": "PreToolUse", "tool_name": "Bash", "session_id": session,
                "tool_input": {"command": cmd}}

    def test_a_risky_command_gets_the_precheck_once_per_cooldown(self):
        self.card("K-90001", ["deployment"])
        ctx = self.context(self.bash("rm -rf /srv/build/old"))
        self.assertIn("risky command: deployment", ctx)
        self.assertIn("K-90001", ctx)
        self.assertEqual(self.context(self.bash("rm -rf /srv/build/older")), "")   # cooldown

    def test_an_ordinary_command_gets_no_precheck(self):
        self.card("K-90001", ["deployment"])
        self.assertNotIn("risky command", self.context(self.bash("ls -la")))

    def test_a_running_night_is_said_at_session_start(self):
        lock = os.path.join(self.state, "shared", "nightly.lock")
        os.makedirs(os.path.dirname(lock), exist_ok=True)
        with open(lock, "w") as f:
            f.write("nightly %s nk=2026-01-01\n" % datetime.datetime.now().astimezone().isoformat())
        st = os.stat(lock)
        table = os.path.join(self.root, "locks")
        with open(table, "w") as f:
            f.write("1: FLOCK  ADVISORY  WRITE 999 %02x:%02x:%d 0 EOF\n"
                    % (os.major(st.st_dev), os.minor(st.st_dev), st.st_ino))
        ctx = self.context({"hook_event_name": "SessionStart", "session_id": "s1"},
                           KHMS_PROC_LOCKS=table, KHMS_NIGHTLY_LOCK=lock)
        self.assertIn("NIGHT PIPELINE IS RUNNING", ctx)
        quiet = self.context({"hook_event_name": "SessionStart", "session_id": "s2"},
                             KHMS_PROC_LOCKS=os.path.join(self.root, "empty-locks"), KHMS_NIGHTLY_LOCK=lock)
        self.assertNotIn("NIGHT PIPELINE IS RUNNING", quiet)

    def test_an_unreadable_lock_table_is_said_not_guessed(self):
        os.makedirs(os.path.join(self.state, "shared"), exist_ok=True)
        open(os.path.join(self.state, "shared", "nightly.lock"), "w").close()
        with open(os.path.join(self.root, "memory", "inbox", "2026-01-01.md"), "w") as f:
            f.write("a card proposal\n")
        ctx = self.context({"hook_event_name": "SessionStart", "session_id": "s1"},
                           KHMS_PROC_LOCKS=os.path.join(self.root, "no-such-table"),
                           KHMS_NIGHTLY_LOCK=os.path.join(self.state, "shared", "nightly.lock"))
        self.assertIn("UNREVIEWED INBOX", ctx)
        self.assertIn("Night pipeline state unknown", ctx)


# --- the message catalogue -------------------------------------------------------------------------------
class MessageCatalogue(unittest.TestCase):
    def test_every_key_the_code_asks_for_has_an_english_default(self):
        sys.path.insert(0, TOOLS)
        import khms_messages as M
        used = set()
        for name in os.listdir(TOOLS):
            if name.endswith(".py"):
                with open(os.path.join(TOOLS, name), encoding="utf-8") as f:
                    used |= set(re.findall(r"\bM\.msg\(\s*\"([A-Z0-9_]+)\"", f.read()))
        self.assertGreater(len(used), 40)
        self.assertEqual(sorted(used - set(M.DEFAULTS)), [])

    def test_every_default_is_ascii_english(self):
        sys.path.insert(0, TOOLS)
        import khms_messages as M
        for k, v in M.DEFAULTS.items():
            self.assertTrue(all(ord(c) < 128 or c == "·" for c in v), k)

    def test_a_deployment_messages_file_replaces_the_wording(self):
        code = ("import sys; sys.path.insert(0, %r)\n"
                "import khms_select as s\n"
                "old = {'id': 'K-90002', 'status': 'active', 'level': 'observation', 'tags': [],\n"
                "       'links': {'supports': [], 'refuted_by': []}, 'first': 'old claim'}\n"
                "new = {'id': 'K-90003', 'status': 'active', 'level': 'observation', 'tags': [],\n"
                "       'links': {'supports': [], 'refuted_by': [], 'contradicts': ['K-90002']},\n"
                "       'first': 'the correction'}\n"
                "print(s.build_injection([(40.0, [], old)], [old, new], {}, 1.0, s.V2, [])[0])\n" % TOOLS)
        with tempfile.TemporaryDirectory() as d:
            msgs = os.path.join(d, "messages.env")
            with open(msgs, "w", encoding="utf-8") as f:
                f.write('SELECT_CORRECTOR_POINTER="  >> FIXED BY {id}: {first}"\n')
            env = {k: v for k, v in os.environ.items() if not k.startswith("KHMS_")}
            plain = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env)
            env["KHMS_MESSAGES"] = msgs
            custom = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env)
        self.assertIn("! CORRECTED BY K-90003: the correction", plain.stdout, plain.stderr)
        self.assertIn(">> FIXED BY K-90003: the correction", custom.stdout, custom.stderr)
        self.assertNotIn("CORRECTED BY", custom.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
