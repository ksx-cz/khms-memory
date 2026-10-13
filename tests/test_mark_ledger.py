"""Every real journal MARK has a ledger entry, including private kinds -- except the status kinds of the
denylist (#236, owner 2026-10-13), which are counted as skipped (tests/test_status_marks.py)."""
import hashlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import mark_ledger as ml


@pytest.mark.parametrize('kind', list(ml.DEFAULT_KINDS) + [
    # progress words NOT on the status denylist stay tracked: the denylist names kinds, it does not guess
    'status', 'result', 'todo', 'spawn', 'tokens', 'progress',
    # know-how kinds #209 A2 decided to track
    'owner-request', 'prusvih', 'fix', 'vyreseno', 'correction', 'owner-decision', 'defect',
    'private-kind', 'm\u011b\u0159en\u00ed', 'finding→status',
])
def test_every_actual_kind_is_accounted_for(tmp_path, kind):
    journal = tmp_path / 'journal'
    journal.mkdir()
    text = 'Specific journal record for this kind'
    (journal / '2026-10-10.md').write_text(f'- 10:00 MARK[{kind}] {text}\n')
    marks = ml.derive(ml.Paths(str(tmp_path)), '2026-10-10')
    expected_kind = ml.CANON.get(kind, kind)
    expected_id = 'M-' + hashlib.sha1(f'{expected_kind}|{text}'.encode()).hexdigest()[:8]
    assert list(marks) == [expected_id]
    assert marks[expected_id]['state'] == 'OPEN'
    assert marks[expected_id]['kind'] == expected_kind
    summary = ml.summarize(marks, '2026-10-10')
    assert summary['open_queued'] == 1
    assert kind not in summary['scope']['status_kinds']


@pytest.mark.parametrize('kind', ml.DEFAULT_STATUS_KINDS)
def test_status_kinds_are_skipped_and_counted(tmp_path, kind):
    journal = tmp_path / 'journal'
    journal.mkdir()
    (journal / '2026-10-10.md').write_text(f'- 10:00 MARK[{kind}] Specific status line\n')
    skipped = {}
    marks = ml.derive(ml.Paths(str(tmp_path)), '2026-10-10', skipped=skipped)
    assert marks == {}
    assert skipped == {kind: 1}
    assert ml.summarize(marks, '2026-10-10', skipped=skipped)['status_skipped'] == 1


@pytest.mark.parametrize('line', [
    '- MARK[...] placeholder', '- MARK[<kind>] placeholder',
    '- MARK[..] placeholder', '- MARK[kind] placeholder', '- MARK[x] placeholder',
    'The rule mentions MARK[khms-issue] in prose.',
    '- `MARK[done]` is an example.',
])
def test_prose_placeholders_are_not_marks(line):
    assert ml.parse_marks(line) == []


@pytest.mark.parametrize('canon,aliases', ml.DEFAULT_KINDS.items())
def test_existing_alias_ids_remain_stable(canon, aliases):
    text = 'Existing stable record'
    expected = 'M-' + hashlib.sha1(f'{canon}|{text}'.encode()).hexdigest()[:8]
    for alias in aliases:
        kind, _, parsed = ml.parse_line(f'MARK[{alias}] {text}')
        assert ml.mark_id(kind, parsed) == expected


def test_status_record_needs_explicit_dismissal_and_keeps_reason(tmp_path):
    """A progress MARK of a kind NOT on the status denylist is tracked, and leaves OPEN only by a reason."""
    journal = tmp_path / 'journal'
    journal.mkdir()
    (journal / '2026-10-10.md').write_text('MARK[result] Completed the deployment\n')
    paths = ml.Paths(str(tmp_path))
    mid = next(iter(ml.derive(paths, '2026-10-10')))
    paths_dir = tmp_path / 'memory' / 'ledger'
    paths_dir.mkdir(parents=True)
    import json
    reason = 'Deployment status only; no transferable know-how.'
    (paths_dir / 'events.jsonl').write_text(json.dumps({
        'id': mid, 'ev': 'dismiss', 'reason': reason, 'by': 'reviewer',
    }) + '\n')
    mk = ml.derive(paths, '2026-10-10')[mid]
    assert mk['state'] == 'DISMISSED'
    assert mk['reason'] == reason


def test_durable_pending_record_remains_open_and_is_not_queued(tmp_path):
    journal = tmp_path / 'journal'
    journal.mkdir()
    (journal / '2026-10-10.md').write_text('MARK[khms-issue] Card source was never supplied\n')
    paths = ml.Paths(str(tmp_path))
    mid = next(iter(ml.derive(paths, '2026-10-10')))
    carry = tmp_path / 'memory' / 'ledger' / 'carry'
    carry.mkdir(parents=True)
    (carry / 'cons-01.md').write_text(f'MARKS: {mid}\n')
    marks = ml.derive(paths, '2026-10-10')
    assert marks[mid]['state'] == 'OPEN'
    assert marks[mid]['proposed_in'] == 'memory/ledger/carry/cons-01.md'
    assert ml.summarize(marks, '2026-10-10')['open_queued'] == 0


@pytest.mark.parametrize('time,kind,prefix', [
    ('12:40', 'khms-issue', 'DBASE ($8.85): design discussion; owner questions.'),
    ('00:30', 'chyba', 'RGATEM4C: cycle dirty; teardown reached stop timeout.'),
    ('01:59', 'nalez', 'CHK173 FAIL posted and delivered (delivery verified).'),
    ('02:28', 'chyba', 'PTCPCLEAN closed: reconnect deletion hangs. Fix 2e28fdd6.'),
    ('03:02', 'chyba', 'RGATEM5 refused at candidate (EXTERNAL evidence).'),
])
def test_mark_after_timestamped_journal_narrative(time, kind, prefix):
    text = 'Specific lesson from the recorded event.'
    assert ml.parse_marks(f'- {time} {prefix} MARK[{kind}] {text}') == [(kind, time, text)]


@pytest.mark.parametrize('line', [
    '- 12:40 The rule mentions MARK[khms-issue] in prose.',
    '- 12:40 Earlier MARK[finding] about the failure is a reference.',
    '- 12:40 Example. `MARK[done] Completed deployment`.',
    '- 12:40 Example. "MARK[done] Completed deployment".',
    'A design note. MARK[khms-issue] describes the syntax.',
])
def test_narrative_references_and_quoted_examples_stay_excluded(line):
    assert ml.parse_marks(line) == []


def test_a_covered_by_verdict_is_a_dismissal_with_its_reason_never_coverage(tmp_path):
    """#237: `covered by K-x: <why>` is a NOT CARDED reason (the extra detail changes no action): it closes the
    mark as DISMISSED without a quote, while `already carded K-x` still needs its verbatim quote (#209 A1)."""
    journal = tmp_path / 'journal'
    journal.mkdir()
    (journal / '2099-03-04.md').write_text(
        'MARK[nalez] the widget bus sags again on the second unit, 11.2 V\n'
        'MARK[nalez] the widget bus voltage sags under load, measured once more\n'
        'MARK[nalez] the widget bus sags, a third note without a quote\n')
    know = tmp_path / 'memory' / 'know'
    know.mkdir(parents=True)
    (know / 'K-00042.md').write_text('---\nid: K-00042\ntype: fact\nlevel: observation\nstatus: active\n'
                                     'tags: [power]\n---\nthe widget bus voltage sags under load\n')
    paths = ml.Paths(str(tmp_path))
    ids = list(ml.derive(paths, '2099-03-04'))
    assert len(ids) == 3
    unit = (f'## MARKS NOT CARDED\n'
            f'- {ids[0]} :: covered by K-00042: the second unit and its 11.2 V change no action the card sets\n'
            f'- {ids[1]} :: already carded K-00042: "the widget bus voltage sags under load"\n'
            f'- {ids[2]} :: already carded K-00042\n')
    evs, bad, unproven, carded = ml.record_not_carded(paths, unit, 'reviewer', floor='2099-03-04')
    assert not bad and not carded
    assert [u[0] for u in unproven] == [ids[2]], 'a bare "already carded" is still not coverage'
    cov = next(e for e in evs if e['id'] == ids[0])
    assert cov['ev'] == 'dismiss' and 'card' not in cov
    assert cov['reason'] == ('nightly verdict accepted at review: covered by K-00042: the second unit and '
                             'its 11.2 V change no action the card sets')
    marks = ml.derive(paths, '2099-03-04')
    assert marks[ids[0]]['state'] == 'DISMISSED' and 'card' not in marks[ids[0]]
    assert marks[ids[1]]['state'] == 'CARDED' and marks[ids[1]]['card'] == 'K-00042'
    assert marks[ids[2]]['state'] == 'OPEN'
    s = ml.summarize(marks, '2099-03-04')
    assert (s['dismissed'], s['carded'], s['open']) == (1, 1, 1)
    again = ml.record_not_carded(paths, unit, 'reviewer', floor='2099-03-04')
    assert [e for e in again[0] if e['id'] == ids[0]] == [], 'closing the unit twice adds nothing'
