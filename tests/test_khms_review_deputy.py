"""Generic review dispatch confirms delivery without approving any unit."""
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import khms_review_deputy as D


def test_clean_never_calls_transport(monkeypatch, tmp_path):
    monkeypatch.setattr(D, 'pending', lambda *args: [])
    monkeypatch.setattr(D.subprocess, 'run', lambda *args, **kw: pytest.fail('unexpected delivery'))
    assert D.dispatch('/tools', tmp_path, ['transport'], {}, False) == 0


def test_confirmed_delivery_preserves_environment_and_never_marks_reviewed(monkeypatch, tmp_path):
    monkeypatch.setattr(D, 'pending', lambda *args: ['UNREVIEWED memory/inbox/unit.md'])
    calls = []
    def run(argv, **kw):
        calls.append((argv, kw))
        return subprocess.CompletedProcess(argv, 0, argv[argv.index('--receipt') + 1], '')
    monkeypatch.setattr(D.subprocess, 'run', run)
    env = {'IDENTITY_CONFIG': '/own/config'}
    assert D.dispatch('/tools', tmp_path, ['transport', '--configured'], env) == 0
    assert calls[0][0][:2] == ['transport', '--configured']
    assert calls[0][1]['env'] is env
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('rc,reply,expected', [(3, '', 3), (4, '', 4), (0, 'unrelated reply', 4)])
def test_busy_failed_or_unconfirmed_delivery_stays_pending(monkeypatch, tmp_path, rc, reply, expected):
    monkeypatch.setattr(D, 'pending', lambda *args: ['UNREVIEWED unit'])
    monkeypatch.setattr(D.subprocess, 'run', lambda argv, **kw: subprocess.CompletedProcess(argv, rc, reply, ''))
    assert D.dispatch('/tools', tmp_path, ['transport'], {}) == expected
    assert not list(tmp_path.iterdir())


def test_dry_run_does_not_contact_transport(monkeypatch, tmp_path):
    monkeypatch.setattr(D, 'pending', lambda *args: ['UNREVIEWED unit'])
    monkeypatch.setattr(D.subprocess, 'run', lambda *args, **kw: pytest.fail('unexpected delivery'))
    assert D.dispatch('/tools', tmp_path, ['transport'], {}, True) == 0
