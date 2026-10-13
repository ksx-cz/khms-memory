"""#236 (owner 2026-10-13): status lines are plain lines, never a tracked MARK.

Three places keep a status line from becoming a tracked MARK again:

  * LEDGER. `mark_ledger.py sync` does not track a MARK whose kind is on the status denylist
    (LEDGER_STATUS_KINDS, default `DEFAULT_STATUS_KINDS`), and it SAYS how many such lines it skipped --
    in its output, in summary.json and in the status line -- so the skip is never silent (K-00586).
  * HOOK. A PreToolUse Edit/Write/Bash that would add a `MARK[<status kind>]` line to a journal file of the
    base is refused with "status lines are plain lines without MARK (owner 2026-10-13)"; a know-how MARK and
    a plain status line (`- 08:55 agent-done X ...`) pass.
  * RELAY. `journal_marks.py` refuses a direct status MARK and does not relay one from a handoff, and says
    how many it left out.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[1] / 'tools'
sys.path.insert(0, str(TOOLS))
import mark_ledger as ML  # noqa: E402

DENY_TEXT = 'status lines are plain lines without MARK (owner 2026-10-13)'
DAY = '2026-10-13'
JOURNAL_FIXTURE = (
    f'- 08:50 MARK[decision] Keep the ledger floor in memory/ledger/FLOOR because the default is 1970.\n'
    f'- 08:55 MARK[agent-done] NOSTAT #236: package finished\n'
    f'- 09:00 MARK[owner-request] The owner wants status lines kept out of the ledger.\n'
    f'- 09:05 agent-done NOSTAT #236 plain status line, no MARK\n'
)


def _run(args, root, **env):
    e = dict(os.environ, KHMS_ROOT=str(root), PYTHONDONTWRITEBYTECODE='1')
    e.update(env)
    return subprocess.run([sys.executable, *map(str, args)], env=e, capture_output=True, text=True,
                          timeout=60)


# --- the ledger ---------------------------------------------------------------------------------------
def test_default_denylist_is_the_owner_list():
    want = {'agent-done', 'agent-done-check', 'agent-fail', 'agent-partial', 'board-reply', 'board-archive',
            'board', 'hotovo', 'stav', 'done', 'check', 'dispatch', 'deploy', 'review', 'merge', 'plan',
            'handoff', 'report', 'overeno', 'potvrzeno', 'pending', 'resolved', 'verified', 'khms'}
    assert set(ML.DEFAULT_STATUS_KINDS) == want
    # know-how kinds that #209 A2 decided to track stay tracked
    for kind in ('owner-request', 'prusvih', 'fix', 'vyreseno', 'correction', 'owner-decision', 'defect',
                 'khms-issue', 'nalez', 'chyba'):
        assert ML.status_kind(kind) is None, kind


@pytest.mark.parametrize('raw', ['agent-done', 'AGENT-DONE', 'board-reply', 'ov\u011b\u0159eno', 'overeno',
                                 'agent-done→x', '**done**'])
def test_status_kind_matches_spellings(raw):
    assert ML.status_kind(raw)


def test_sync_tracks_base_and_other_skips_status_and_says_so(tmp_path):
    (tmp_path / 'journal').mkdir()
    (tmp_path / 'journal' / f'{DAY}.md').write_text(JOURNAL_FIXTURE, encoding='utf-8')
    r = _run([TOOLS / 'mark_ledger.py', '--root', tmp_path, '--floor', DAY, 'sync'], tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    marks = [json.loads(ln) for ln in
             (tmp_path / 'memory' / 'ledger' / 'marks.jsonl').read_text(encoding='utf-8').splitlines()]
    assert sorted(m['kind'] for m in marks) == ['decision', 'owner-request']
    summary = json.loads((tmp_path / 'memory' / 'ledger' / 'summary.json').read_text(encoding='utf-8'))
    assert summary['total'] == 2
    assert summary['status_skipped'] == 1
    assert summary['status_skipped_kinds'] == {'agent-done': 1}
    assert 'agent-done' in summary['scope']['status_kinds']
    # the skip is SAID, in the sync output and in the one-line status
    assert 'status lines skipped' in r.stdout and 'agent-done 1' in r.stdout, r.stdout
    st = _run([TOOLS / 'mark_ledger.py', '--root', tmp_path, 'status'], tmp_path)
    assert st.returncode == 0, st.stderr
    assert 'status lines skipped' in st.stdout and ': 1' in st.stdout, st.stdout


def test_denylist_is_configurable(tmp_path):
    (tmp_path / 'journal').mkdir()
    (tmp_path / 'journal' / f'{DAY}.md').write_text(JOURNAL_FIXTURE, encoding='utf-8')
    pat = tmp_path / 'patterns.env'
    pat.write_text('LEDGER_STATUS_KINDS=owner-request\n', encoding='utf-8')
    r = _run([TOOLS / 'mark_ledger.py', '--root', tmp_path, '--floor', DAY, 'sync', '--dry'], tmp_path,
             KHMS_PATTERNS=str(pat))
    assert r.returncode == 0, r.stdout + r.stderr
    assert 'total tracked marks: 2' in r.stdout, r.stdout
    assert 'owner-request 1' in r.stdout, r.stdout


# --- the hook -----------------------------------------------------------------------------------------
@pytest.fixture
def base():
    root = tempfile.mkdtemp(prefix='nostat-')
    subprocess.run([str(TOOLS / 'khms_init.sh'), root], capture_output=True, text=True, check=True)
    os.makedirs(os.path.join(root, 'journal'), exist_ok=True)
    yield Path(root)
    shutil.rmtree(root, True)


def fire(base, tool, tool_input, cwd=None):
    payload = {'hook_event_name': 'PreToolUse', 'tool_name': tool, 'tool_input': tool_input,
               'session_id': 'nostat', 'cwd': str(cwd or base)}
    r = subprocess.run([sys.executable, str(TOOLS / 'khms_hook.py')], input=json.dumps(payload),
                       capture_output=True, text=True, timeout=60,
                       env=dict(os.environ, KHMS_ROOT=str(base), PYTHONDONTWRITEBYTECODE='1'))
    assert r.returncode == 0, r.stdout + r.stderr
    assert 'Traceback' not in r.stderr, r.stderr
    out = r.stdout.strip()
    hso = (json.loads(out) if out else {}).get('hookSpecificOutput') or {}
    return hso.get('permissionDecision') == 'deny', hso.get('permissionDecisionReason', '')


STATUS_LINE = '- 08:55 MARK[agent-done] NOSTAT #236: package finished\n'
KNOW_LINE = '- 08:55 MARK[nalez] the ledger floor default is 1970-01-01, not the first tracked day\n'
PLAIN_LINE = '- 08:55 agent-done NOSTAT #236: package finished\n'


def test_write_of_status_mark_into_journal_is_refused(base):
    j = base / 'journal' / f'{DAY}.md'
    denied, why = fire(base, 'Write', {'file_path': str(j), 'content': '# day\n' + STATUS_LINE})
    assert denied and DENY_TEXT in why, why


def test_edit_adding_status_mark_is_refused_know_how_and_plain_pass(base):
    j = base / 'journal' / f'{DAY}.md'
    j.write_text('# day\n', encoding='utf-8')
    denied, why = fire(base, 'Edit', {'file_path': str(j), 'old_string': '# day\n',
                                      'new_string': '# day\n' + STATUS_LINE})
    assert denied and DENY_TEXT in why, why
    for line in (KNOW_LINE, PLAIN_LINE):
        denied, why = fire(base, 'Edit', {'file_path': str(j), 'old_string': '# day\n',
                                          'new_string': '# day\n' + line})
        assert not denied, (line, why)


def test_edit_keeping_an_existing_status_mark_passes(base):
    """The gate stops ADDING one; an edit elsewhere in a block that already holds an old status MARK is not
    a new status line."""
    j = base / 'journal' / f'{DAY}.md'
    old = STATUS_LINE + '- 09:00 typo\n'
    j.write_text(old, encoding='utf-8')
    denied, why = fire(base, 'Edit', {'file_path': str(j), 'old_string': old,
                                      'new_string': STATUS_LINE + '- 09:00 typo fixed\n'})
    assert not denied, why


def test_bash_append_of_status_mark_is_refused_and_reads_pass(base):
    j = base / 'journal' / f'{DAY}.md'
    denied, why = fire(base, 'Bash', {'command': "echo '%s' >> %s" % (STATUS_LINE.strip(), j)})
    assert denied and DENY_TEXT in why, why
    denied, why = fire(base, 'Bash', {'command': "echo '%s' >> journal/%s.md" % (STATUS_LINE.strip(), DAY)},
                       cwd=base)
    assert denied and DENY_TEXT in why, why
    for cmd in ("echo '%s' >> %s" % (KNOW_LINE.strip(), j),
                "echo '%s' >> %s" % (PLAIN_LINE.strip(), j),
                "grep -c 'MARK\\[agent-done\\]' %s" % j):
        denied, why = fire(base, 'Bash', {'command': cmd})
        assert not denied, (cmd, why)


@pytest.mark.parametrize('cmd', [
    'echo "$(date -Is) MARK[board-reply] #236 answered (agent x)" >> {j}',
    "printf -- '- %s MARK[agent-done] %s\\n' \"$(date +%H:%M)\" X >> {j}",
    "cat >> {j} <<'X'\n- 10:00 MARK[ov\u011b\u0159eno] CHECK green\nX",
    "echo '- 10:00 MARK[done] x' | tee -a {j}",
])
def test_bash_writer_shapes_are_refused(base, cmd):
    j = base / 'journal' / f'{DAY}.md'
    denied, why = fire(base, 'Bash', {'command': cmd.format(j=j)})
    assert denied and DENY_TEXT in why, (cmd, why)


def test_bash_status_mark_to_another_file_passes(base):
    denied, why = fire(base, 'Bash', {'command': "echo '%s' >> %s" % (STATUS_LINE.strip(),
                                                                      base / 'plans' / 'x.md')})
    assert not denied, why


def test_status_mark_outside_the_journal_passes(base):
    denied, why = fire(base, 'Write', {'file_path': str(base / 'plans' / 'handoff-x.md'),
                                       'content': STATUS_LINE})
    assert not denied, why


# --- the relay ----------------------------------------------------------------------------------------
def test_relay_refuses_a_direct_status_mark(tmp_path):
    r = _run([TOOLS / 'journal_marks.py', '--mark', 'MARK[agent-done] NOSTAT finished', '--source', 's1',
              '--journal-dir', tmp_path / 'journal', '--runtime-dir', tmp_path / 'rt'], tmp_path / 'base')
    assert r.returncode != 0
    assert DENY_TEXT in r.stderr, r.stderr
    assert not list((tmp_path / 'journal').glob('*.md')) if (tmp_path / 'journal').exists() else True


def test_relay_leaves_status_marks_of_a_handoff_out_and_says_so(tmp_path):
    h = tmp_path / 'handoff.md'
    h.write_text('## MARKs\n- MARK[nalez] a reusable lesson (agent programmer #236)\n'
                 '- MARK[done] package finished (agent programmer #236)\n## Other\n', encoding='utf-8')
    r = _run([TOOLS / 'journal_marks.py', '--handoff', h,
              '--journal-dir', tmp_path / 'journal', '--runtime-dir', tmp_path / 'rt'], tmp_path / 'base')
    assert r.returncode == 0, r.stderr
    text = ''.join(p.read_text() for p in (tmp_path / 'journal').glob('*.md'))
    assert 'MARK[nalez]' in text and 'MARK[done]' not in text, text
    assert '1 status' in r.stdout + r.stderr, r.stdout + r.stderr
