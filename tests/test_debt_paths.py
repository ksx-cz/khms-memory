import importlib.util
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
