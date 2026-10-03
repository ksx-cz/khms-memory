"""verify_owner_quotes.py: principal-attributed journal quotes grounded in the chat record.

Every case is discriminating: for each quote that must ground there is a sibling that differs in ONE
property the tool exists to catch (order of words, date, who is speaking) and must not ground. The language
data comes from a patterns file the test writes (English here), the same way a deployment configures its own
language; with no name configured the tool must refuse (rc 2) rather than print a clean scan.
"""
import json
import os
import subprocess
import sys

import pytest

TOOLS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools")
TOOL = os.path.join(TOOLS, "verify_owner_quotes.py")
OWNER, BOT = "1001", "2002"

MSGS = [
    # 12:00Z keeps every local date in Europe/Berlin on the same day all year.
    ("2099-03-01T12:00:00+00:00", OWNER, "you do nothing yourself, hand every package to a subagent"),
    ("2099-03-01T12:05:00+00:00", OWNER, "are you doing something right now"),
    ("2099-03-02T12:00:00+00:00", OWNER, "measure the drift on the known path first"),
    ("2099-03-02T12:01:00+00:00", BOT, "the bot says the sky is green today"),
]


def write_base(tmp_path, patterns):
    hist = tmp_path / "chat.jsonl"
    hist.write_text("".join(json.dumps({"id": str(i), "timestamp": t, "content": c, "author": {"id": a}}) + "\n"
                            for i, (t, a, c) in enumerate(MSGS)), encoding="utf-8")
    pat = tmp_path / "patterns.env"
    pat.write_text(patterns, encoding="utf-8")
    jdir = tmp_path / "journal"
    jdir.mkdir()
    return hist, pat, jdir


PATTERNS = "OWNERQ_NAME=Ann\nOWNERQ_SELFTEST_QUOTE=are you doing something right now\n"


def run(tmp_path, hist, pat, jdir, *extra):
    env = dict(os.environ, KHMS_ROOT=str(tmp_path / "base"), KHMS_PATTERNS=str(pat))
    return subprocess.run([sys.executable, TOOL, "--journal", str(jdir), "--history", str(hist),
                           "--owner-id", OWNER, "--journal-tz", "Europe/Berlin", *extra],
                          capture_output=True, text=True, env=env, timeout=60)


def test_a_true_quote_grounds_and_the_self_test_passes(tmp_path):
    hist, pat, jdir = write_base(tmp_path, PATTERNS)
    (jdir / "2099-03-01.md").write_text(
        '- 13:00 MARK[x] Ann: "you do nothing yourself, hand every package to a subagent"\n', encoding="utf-8")
    r = run(tmp_path, hist, pat, jdir, "--self-test", "--verbose")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "SELFTEST OK" in r.stdout
    assert "control ok: the agent's own words addressed TO the principal are not the principal's" in r.stdout
    assert "1 owner-attributed quotes checked · 1 grounded" in r.stdout


def test_a_severed_and_reordered_quote_is_absent(tmp_path):
    hist, pat, jdir = write_base(tmp_path, PATTERNS)
    (jdir / "2099-03-01.md").write_text('- 13:00 Ann wrote "nothing do yourself"\n', encoding="utf-8")
    r = run(tmp_path, hist, pat, jdir, "--self-test")
    assert r.returncode == 1
    assert "[absent]" in r.stdout and "UNGROUNDED" in r.stdout


def test_a_true_quote_on_the_wrong_day_is_re_dated(tmp_path):
    hist, pat, jdir = write_base(tmp_path, PATTERNS)
    (jdir / "2099-03-05.md").write_text(
        "- 09:00 Ann's request 'measure the drift on the known path first'\n", encoding="utf-8")
    r = run(tmp_path, hist, pat, jdir, "--self-test")
    assert r.returncode == 1
    assert "[time]" in r.stdout


def test_the_agents_own_words_to_the_principal_are_not_checked(tmp_path):
    hist, pat, jdir = write_base(tmp_path, PATTERNS)
    (jdir / "2099-03-01.md").write_text(
        '- 13:00 I replied to Ann, "the sky is green today and that settles it"\n'
        '- 13:01 Ann: "are you doing something right now"\n', encoding="utf-8")
    r = run(tmp_path, hist, pat, jdir, "--self-test")
    assert r.returncode == 0, r.stdout
    assert "1 owner-attributed quotes checked · 1 grounded" in r.stdout


def test_a_bot_message_does_not_ground_a_quote(tmp_path):
    hist, pat, jdir = write_base(tmp_path, PATTERNS)
    (jdir / "2099-03-02.md").write_text('- 13:00 Ann: "the bot says the sky is green today"\n', encoding="utf-8")
    r = run(tmp_path, hist, pat, jdir, "--self-test")
    assert r.returncode == 1 and "[absent]" in r.stdout


@pytest.mark.parametrize("patterns,needle", [
    ("", "no principal name configured"),
    ("OWNERQ_NAME=Ann\n", None),
])
def test_missing_configuration_never_yields_a_clean_scan(tmp_path, patterns, needle):
    hist, pat, jdir = write_base(tmp_path, patterns)
    (jdir / "2099-03-01.md").write_text('- 13:00 Ann: "are you doing something right now"\n', encoding="utf-8")
    r = run(tmp_path, hist, pat, jdir, "--self-test")
    assert r.returncode != 0, r.stdout
    if needle:
        assert r.returncode == 2 and needle in r.stdout
    else:     # a name but no known sentence: the positive control cannot run, the scan is not trusted
        assert "SELFTEST OK" not in r.stdout and "no OWNERQ_SELFTEST_QUOTE configured" in r.stdout


def test_an_inflected_language_is_configured_not_coded(tmp_path):
    """A possessive form configured as a regex attributes; the dative of the same name does not. The
    inflection here is invented (ASCII) on purpose: the property is that ANY case system is data."""
    pats = ("OWNERQ_NAME=Anna\nOWNERQ_SPEAKER=Anna(?!\\w)\nOWNERQ_POSSESSIVE=Annao\n"
            "OWNERQ_NOUNS=quaest\\w*\nOWNERQ_SELFTEST_QUOTE=are you doing something right now\n"
            'OWNERQ_SELFTEST_NOT_HIS=Annae ego scripsi: "{quote}"\nOWNERQ_OWN_VOICE=\\bego\\b\n')
    hist, pat, jdir = write_base(tmp_path, pats)
    (jdir / "2099-03-02.md").write_text(
        "- 13:00 Annao quaestio 'measure the drift on the known path first'\n"
        "- 13:05 Annae ego scripsi: 'this sentence was never written by anybody'\n", encoding="utf-8")
    r = run(tmp_path, hist, pat, jdir, "--self-test")
    assert r.returncode == 0, r.stdout
    assert "SELFTEST OK" in r.stdout and "1 owner-attributed quotes checked · 1 grounded" in r.stdout
