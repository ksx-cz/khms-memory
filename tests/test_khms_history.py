"""Historical evaluations cannot witness their own input from current cards."""
import importlib
from pathlib import Path
import sys

import pytest

EXPERIMENTS = Path(__file__).resolve().parents[1] / 'tools' / 'khms_experiments'
sys.path.insert(0, str(EXPERIMENTS))
sys.path.insert(0, str(EXPERIMENTS.parent))


def test_missing_history_never_selects_live_cards(monkeypatch, tmp_path):
    monkeypatch.setenv("KHMS_ROOT", str(tmp_path))
    import khms_paths as P
    monkeypatch.setattr(P, "_RESOLVED", None)
    module = importlib.import_module('eval_triage_selector')
    def unavailable():
        raise module.VersionError('no server history configured')
    monkeypatch.setattr(module.Versions, 'configured', unavailable)
    (tmp_path / 'memory/know').mkdir(parents=True)
    (tmp_path / 'memory/know/today.md').write_text('not a historical witness')
    with pytest.raises(module.VersionError, match='no server history'):
        module.load_rows(str(tmp_path))


def test_experiments_do_not_call_local_snapshots():
    for path in EXPERIMENTS.glob('*.py'):
        assert 'brain_snapshot.sh' not in path.read_text()
        assert 'E.SNAP_ROOT' not in path.read_text()


def test_configured_metadata_is_external_and_default_names_generic(monkeypatch, tmp_path):
    monkeypatch.setenv("KHMS_ROOT", str(tmp_path))
    import khms_paths as P
    monkeypatch.setattr(P, "_RESOLVED", None)
    module = importlib.import_module('judge_exp')
    assert len(module.LABELLERS) == 2
    assert module.OUT


def test_metrics_use_configured_project_and_patterns(tmp_path):
    import json
    import os
    import subprocess
    root = tmp_path / 'base'
    (root / 'journal').mkdir(parents=True)
    (root / 'journal/2099-01-01.md').write_text('10:00 MARK[correction] same error again\n')
    transcripts = tmp_path / 'project'
    transcripts.mkdir()
    history = tmp_path / 'chat.jsonl'
    history.write_text(''.join(json.dumps({'timestamp': '2099-01-01T12:00:00Z',
                                         'author': {'id': 'principal'}, 'content': text}) + '\n'
                               for text in ['wrong command', 'thanks']))
    config = tmp_path / 'metrics.json'
    config.write_text(json.dumps({'transcripts': str(transcripts), 'history': str(history),
                                  'owner_id': 'principal'}))
    env = dict(os.environ, KHMS_ROOT=str(root), KHMS_METRICS_CONFIG=str(config), TZ='UTC')
    script = EXPERIMENTS.parent / 'khms_experiment_metrics.sh'
    r = subprocess.run(['bash', str(script), '--from', '2099-01-01', '--to', '2099-01-01'],
                       env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    total = next(line for line in r.stdout.splitlines() if line.startswith('TOTAL')).split()
    assert total[1:5] == ['1', '1', '2', '1'], r.stdout
