"""Durable, project-independent MARK relay and reviewed backlog import."""
import concurrent.futures
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

TOOLS = Path(__file__).resolve().parents[1] / 'tools'
sys.path.insert(0, str(TOOLS))
import journal_marks as J
import mark_ledger as ML


def handoff(tmp_path):
    p = tmp_path / 'handoff.md'
    p.write_text('## MARKs\nMARK[finding] Evidence survives accounting failure. (agent worker)\n'
                 '## Other\nMARK[finding] This is outside the relay section.\n')
    return p


def test_only_marks_section():
    text = 'MARK[finding] unrelated\n## MARKs\n- `MARK[finding] yes`\n## Other\nMARK[finding] no'
    assert list(J.marks(text)) == ['MARK[finding] yes']


def relay(args):
    journal, runtime = args
    return J.append_once(journal, runtime, ['MARK[finding] shared (agent worker)'], 'session:one')


def test_concurrent_retry_receipt_is_in_journal(tmp_path):
    args = (tmp_path / 'journal', tmp_path / 'runtime')
    with concurrent.futures.ProcessPoolExecutor(4) as pool:
        assert sum(pool.map(relay, [args] * 12)) == 1
    (args[1] / 'journal.lock').unlink()
    assert relay(args) == 0
    assert sum(p.read_text().count('MARK[finding]') for p in args[0].glob('*.md')) == 1


def run_tool(tmp_path, tool, *args, **env):
    return subprocess.run([sys.executable, str(TOOLS / tool), *map(str, args)],
                          env=dict(os.environ, KHMS_ROOT=str(tmp_path / 'base'),
                                   PYTHONDONTWRITEBYTECODE='1', KHMS_RUNTIME=str(tmp_path / 'runtime'), **env),
                          capture_output=True, text=True, timeout=30)


def journal_text(tmp_path):
    return ''.join(p.read_text() for p in (tmp_path / 'journal').glob('*.md'))


def test_handoff_cli_needs_no_project_board_or_transcript(tmp_path):
    h = handoff(tmp_path)
    for _ in range(2):
        r = run_tool(tmp_path, 'journal_marks.py', '--handoff', h,
                     '--journal-dir', tmp_path / 'journal', '--runtime-dir', tmp_path / 'runtime')
        assert r.returncode == 0, r.stderr
    assert 'journal: 0 MARKs appended' in r.stdout
    assert journal_text(tmp_path).count('MARK[finding]') == 1


@pytest.mark.parametrize('kind', ['finding', 'done', 'khms-issue', 'private-kind', '\u010dm\u011b\u0159en\u00ed'])
def test_every_mark_kind_is_relayed_to_the_ledger(tmp_path, kind):
    text = f'MARK[{kind}] A reusable lesson.'
    assert list(J.marks('## MARKs\n' + text)) == [text]
    J.append_once(tmp_path / 'journal', tmp_path / 'runtime', [text], 'source')
    rows = ML.scan_journal(next((tmp_path / 'journal').glob('*.md')))
    assert len(rows) == 1
    assert rows[0]['kind'] == ML.canon_kind(kind)
    assert rows[0]['id'] == ML.mark_id(ML.canon_kind(kind), 'A reusable lesson.')


@pytest.mark.parametrize('example', ['MARK[...] placeholder', 'The rule mentions MARK[finding] here.'])
def test_prose_and_placeholders_are_not_relayed(example):
    assert list(J.marks('## MARKs\n' + example)) == []


def test_reader_refuses_before_any_mutation_but_dry_preview_is_allowed(tmp_path):
    args = ('--mark', 'MARK[finding] Evidence.', '--source', 'session:one',
            '--journal-dir', tmp_path / 'journal', '--runtime-dir', tmp_path / 'runtime')
    r = run_tool(tmp_path, 'journal_marks.py', *args, KHMS_READ_ONLY='1')
    assert r.returncode == 4, r.stderr
    assert not (tmp_path / 'journal').exists() and not (tmp_path / 'runtime').exists()
    r = run_tool(tmp_path, 'journal_marks.py', *args, '--dry-run', KHMS_READ_ONLY='1')
    assert r.returncode == 0, r.stderr
    assert not (tmp_path / 'journal').exists() and not (tmp_path / 'runtime').exists()


def test_default_destinations_use_memory_configuration(tmp_path):
    r = run_tool(tmp_path, 'journal_marks.py', '--mark', 'MARK[finding] Evidence.', '--source', 'source')
    assert r.returncode == 0, r.stderr
    assert len(list((tmp_path / 'base' / 'journal').glob('*.md'))) == 1
    assert (tmp_path / 'runtime' / 'journal.lock').exists()


def test_backlog_preview_manifest_and_retry(tmp_path):
    h = handoff(tmp_path)
    preview = run_tool(tmp_path, 'import_journal_backlog.py', tmp_path)
    assert preview.returncode == 0, preview.stderr
    manifest = tmp_path / 'preview.json'
    manifest.write_text(preview.stdout)
    assert json.loads(preview.stdout)['marks'] == 1
    args = (tmp_path, '--apply', '--manifest', manifest, '--journal-dir', tmp_path / 'journal',
            '--runtime-dir', tmp_path / 'runtime')
    r = run_tool(tmp_path, 'import_journal_backlog.py', *args, KHMS_READ_ONLY='1')
    assert r.returncode == 4 and not (tmp_path / 'journal').exists(), r.stderr
    for _ in range(2):
        r = run_tool(tmp_path, 'import_journal_backlog.py', *args)
        assert r.returncode == 0, r.stderr
    assert '0 newly appended MARKs' in r.stdout
    assert journal_text(tmp_path).count('MARK[finding]') == 1
    h.write_text(h.read_text() + '\nChanged after preview.\n')
    r = run_tool(tmp_path, 'import_journal_backlog.py', *args)
    assert r.returncode == 2 and 'unchanged reviewed preview manifest' in r.stderr
    assert journal_text(tmp_path).count('MARK[finding]') == 1


@pytest.mark.parametrize('entry', [
    '2026-03-04T10:15:00+00:00 MARK[measurement] Sample latency 4 ms.',
    '10:15 MARK[measurement] Sample latency 4 ms.',
    '10:15 The run completed. MARK[measurement] Sample latency 4 ms.',
])
def test_original_timestamp_backlog_remains_parseable_and_id_stable(tmp_path, entry):
    assert list(J.marks('## MARKs\n' + entry)) == [entry]
    J.append_once(tmp_path / 'journal', tmp_path / 'runtime', [entry], 'source')
    rows = ML.scan_journal(next((tmp_path / 'journal').glob('*.md')))
    assert len(rows) == 1
    assert rows[0]['id'] == ML.mark_id('measurement', 'Sample latency 4 ms.')
    assert rows[0]['time'] == '10:15'
    assert J.append_once(tmp_path / 'journal', tmp_path / 'runtime', [entry], 'source') == 0


def test_legacy_receipt_and_absolute_handoff_source_keep_identity(tmp_path):
    import hashlib
    h = handoff(tmp_path)
    entry = list(J.marks(h.read_text()))[0]
    ident = hashlib.sha256((str(h.resolve()) + '\0' + entry).encode()).hexdigest()
    journal = tmp_path / 'journal'
    journal.mkdir()
    (journal / '2026-03-03.md').write_text(f'<!-- mark-id:{ident} --> {entry}\n')
    r = run_tool(tmp_path, 'journal_marks.py', '--handoff', h,
                 '--journal-dir', journal, '--runtime-dir', tmp_path / 'runtime')
    assert r.returncode == 0, r.stderr
    assert '0 MARKs appended' in r.stdout
    assert journal_text(tmp_path).count(entry) == 1


@pytest.mark.parametrize('entry', ['MARK[...] placeholder', 'MARK[finding]'])
def test_direct_placeholder_or_empty_record_refused(tmp_path, entry):
    r = run_tool(tmp_path, 'journal_marks.py', '--mark', entry, '--source', 'source')
    assert r.returncode == 2, r.stderr
    assert not (tmp_path / 'base').exists()


def test_parenthetical_handoff_prefix_remains_visible_to_ledger(tmp_path):
    entry = '(worker handoff) MARK[finding] A reusable fact.'
    assert list(J.marks('## MARKs\n' + entry)) == [entry]
    J.append_once(tmp_path / 'journal', tmp_path / 'runtime', [entry], 'source')
    rows = ML.scan_journal(next((tmp_path / 'journal').glob('*.md')))
    assert len(rows) == 1
    assert rows[0]['id'] == ML.mark_id('finding', 'A reusable fact.')
    assert J.append_once(tmp_path / 'journal', tmp_path / 'runtime', [entry], 'source') == 0
