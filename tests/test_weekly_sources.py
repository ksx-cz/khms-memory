"""Weekly source selection and complete exclusion accounting."""
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / 'tools/weekly_inbox_window.py'


def run(inbox, names, day='2026-10-11'):
    for name in names:
        (inbox / name).write_text('source\n')
    return subprocess.run([sys.executable, str(SCRIPT), str(inbox), day],
                          text=True, capture_output=True)


def test_sources_and_every_artifact_reported(tmp_path):
    sources = ['2026-10-06.md', '2026-10-06-b01.md',
               '2026-10-06-2-b58.md', '2026-10-06-3-b01.md',
               '2026-10-07-triage.md', '2026-10-08-custom-proposal.md']
    artifacts = ['2026-10-06-b01.approve.md'] + [
        '2026-10-07-triage' + infix + '.' + suffix + '.md'
        for infix in ('', '.md')
        for suffix in ('preview', 'verdict-draft', 'verdict-final',
                       'applied', 'doubtful-resolved')]
    conflict = '2026-10-07-triage.applied (conflicted copy 2026-10-08 045657).md'
    result = run(tmp_path, sources + artifacts + [conflict])
    assert result.returncode == 0
    assert [Path(p).name for p in result.stdout.splitlines()] == sorted(sources)
    for name in artifacts:
        assert f'NOT SELECTED {name}:' in result.stderr
    assert f'NOT SELECTED {conflict}: sync conflict copy' in result.stderr
    assert 'approval derivative' in result.stderr
    assert 'triage artifact' in result.stderr


def test_previous_weekly_is_boundary_and_conflicts_cannot_move_it(tmp_path):
    names = ['2026-09-27-weekly.md', '2026-09-27-late-b01.md',
             '2026-09-26-b01.md', '2026-10-11-b01.md', '2026-10-12-b01.md',
             '2026-10-04-weekly (conflicted copy 2026).md',
             '2026-10-04-weekly.approve.md', '2026-bad-date.md']
    result = run(tmp_path, names)
    assert result.returncode == 0
    assert [Path(p).name for p in result.stdout.splitlines()] == [names[1], names[3]]
    assert '2026-09-27..2026-10-11' in result.stderr
    for name in set(names) - {names[1], names[3]}:
        assert f'NOT SELECTED {name}:' in result.stderr
    assert 'outside window' in result.stderr
    assert 'weekly output' in result.stderr
    assert 'no valid date in name' in result.stderr


def test_empty_day_counts_sources_only_and_default_window(tmp_path):
    result = run(tmp_path, ['2026-10-04-b01.md', '2026-10-05-b01.approve.md'])
    assert '2026-10-04..2026-10-11: 1 files over 1 of 8 days' in result.stderr
    assert 'EMPTY days: 2026-10-05' in result.stderr
