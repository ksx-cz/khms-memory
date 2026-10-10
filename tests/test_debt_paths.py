import importlib.util
import os
import subprocess

import pytest
from pathlib import Path
import sys

TOOLS = Path(__file__).resolve().parents[1] / 'tools'
sys.path.insert(0, str(TOOLS))
import khms_paths as P
spec = importlib.util.spec_from_file_location('debt_paths', TOOLS / 'khms_debt.py')
debt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(debt)


def test_reach_uses_configured_personal_logs(tmp_path, monkeypatch):
    base = tmp_path / 'base'
    (base / 'tools').mkdir(parents=True)
    (base / 'tools' / '.inject.log').write_text('INJECT [K-90003]\n')
    inject = tmp_path / 'personal-inject.log'
    recall = tmp_path / 'personal-recall.log'
    inject.write_text('INJECT [K-90001]\n')
    recall.write_text('top=K-90002\n')
    monkeypatch.setenv('KHMS_ROOT', str(base))
    monkeypatch.setenv('KHMS_INJECT_LOG', str(inject))
    monkeypatch.setenv('KHMS_RECALL_LOG', str(recall))
    P.reset()
    try:
        assert debt.scan_reach(str(base), {'K-90001', 'K-90002', 'K-90003'}) == {'K-90001', 'K-90002'}
    finally:
        P.reset()


@pytest.fixture
def report_env(tmp_path):
    base = tmp_path / 'base'
    know = base / 'memory' / 'know'
    know.mkdir(parents=True)
    (know / 'K-90001.md').write_text('status: active\n')
    inject = tmp_path / 'inject.log'
    recall = tmp_path / 'recall.log'
    inject.write_text('')
    recall.write_text('')
    env = {k: v for k, v in os.environ.items() if not k.startswith('KHMS_')}
    env.update(KHMS_ROOT=str(base), KHMS_INJECT_LOG=str(inject),
               KHMS_RECALL_LOG=str(recall), PYTHONDONTWRITEBYTECODE='1')
    return env, inject, recall


@pytest.mark.parametrize('missing', ['inject', 'recall', 'both'])
@pytest.mark.parametrize('unreadable_directory', [False, True])
def test_debt_missing_log_is_unavailable(report_env, missing, unreadable_directory):
    env, inject, recall = report_env
    paths = {'inject': inject, 'recall': recall}
    absent = list(paths.values()) if missing == 'both' else [paths[missing]]
    for path in absent:
        path.unlink()
        if unreadable_directory:
            path.mkdir()
    result = subprocess.run([sys.executable, str(TOOLS / 'khms_debt.py')],
                            env=env, text=True, capture_output=True)
    assert result.returncode == 0
    assert 'unavailable' in result.stdout.lower()
    assert 'cards never touched: 1 / 1 (100.0 %)' not in result.stdout
    assert 'Traceback' not in result.stderr
    for path in absent:
        assert str(path) in result.stdout
        assert path.is_dir() if unreadable_directory else not path.exists()


def test_debt_existing_empty_logs_are_valid(report_env):
    env, _, _ = report_env
    result = subprocess.run([sys.executable, str(TOOLS / 'khms_debt.py')],
                            env=env, text=True, capture_output=True)
    assert result.returncode == 0
    assert 'cards never touched: 1 / 1 (100.0 %)' in result.stdout
    assert 'unavailable' not in result.stdout.lower()


@pytest.mark.parametrize('json_output', [False, True])
@pytest.mark.parametrize('unreadable_directory', [False, True])
def test_inject_cited_missing_log_is_unavailable(report_env, json_output, unreadable_directory):
    env, inject, _ = report_env
    inject.unlink()
    if unreadable_directory:
        inject.mkdir()
    command = [sys.executable, str(TOOLS / 'inject_cited.py'),
               '--transcripts', str(inject.parent)]
    if json_output:
        command.append('--json')
    result = subprocess.run(command, env=env, text=True, capture_output=True)
    assert result.returncode == 2
    assert 'unavailable' in result.stderr.lower()
    assert str(inject) in result.stderr
    assert 'Traceback' not in result.stderr
    assert not result.stdout
    assert inject.is_dir() if unreadable_directory else not inject.exists()


def test_inject_cited_existing_empty_log_is_valid(report_env):
    env, inject, _ = report_env
    result = subprocess.run([sys.executable, str(TOOLS / 'inject_cited.py'),
                             '--transcripts', str(inject.parent)],
                            env=env, text=True, capture_output=True)
    assert result.returncode == 0
    assert result.stdout.strip() == 'no INJECT records in the window'
    assert not result.stderr
