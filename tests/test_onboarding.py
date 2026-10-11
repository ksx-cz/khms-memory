"""Executable onboarding contract: isolated HOME, local git update, no models."""
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess

REPO = Path(__file__).resolve().parents[1]


def test_docs_keep_code_out_of_the_base():
    for name in ('AGENTS.md', 'claude-code/README.md'):
        text = (REPO / name).read_text()
        assert '$KHMS_ROOT/tools' not in text, name
    assert 'KHMS_CODE' in (REPO / 'README.md').read_text()
    for name in ('README.md', 'AGENTS.md'):
        assert 'mark_ledger.py" sync' in (REPO / name).read_text()
    assert 'stop the embedding daemon' in (REPO / 'README.md').read_text()
    assert 'code-lock-file' in (REPO / 'README.md').read_text()
    assert 'your KHMS_ROOT' not in (REPO / 'claude-code/settings.hooks.json').read_text()


def test_clean_clone_init_recall_hook_and_locked_update(tmp_path):
    env = {'PATH': os.environ['PATH'], 'HOME': str(tmp_path / 'home'),
           'PYTHONDONTWRITEBYTECODE': '1', 'GIT_CONFIG_NOSYSTEM': '1',
           'GIT_CONFIG_GLOBAL': '/dev/null', 'KHMS_ROOT': str(tmp_path / 'base')}
    Path(env['HOME']).mkdir()

    def run(*args, input=None, cwd=None, ok=True):
        p = subprocess.run(args, env=env, cwd=cwd, input=input, text=True, capture_output=True)
        if ok:
            assert p.returncode == 0, (args, p.stdout, p.stderr)
        return p

    # A local upstream is a standalone clone fixture, never a live deployment.
    upstream = tmp_path / 'upstream'
    shutil.copytree(REPO, upstream, ignore=shutil.ignore_patterns('.git', '__pycache__', '.pytest_cache'))
    run('git', 'init', '-b', 'main', str(upstream))
    run('git', '-C', str(upstream), 'add', '.')
    run('git', '-C', str(upstream), '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
        'commit', '-qm', 'old release')
    clone = tmp_path / 'code'
    run('git', 'clone', str(upstream), str(clone))
    tools = clone / 'tools'
    run('bash', str(tools / 'khms_init.sh'), '--register', env['KHMS_ROOT'])
    run('python3', str(tools / 'build_views.py'))
    # Execute the documented ledger command against this clean, empty base.
    quickstart = (REPO / 'README.md').read_text().split('```bash', 1)[1].split('```', 1)[0]
    ledger_command = next(line for line in quickstart.splitlines() if 'mark_ledger.py' in line)
    env['KHMS_CODE'] = str(clone)
    run('bash', '-c', ledger_command)
    assert (Path(env['KHMS_ROOT']) / 'memory/ledger/marks.jsonl').exists()
    recall = run('bash', str(tools / 'recall.sh'), '--lexical', 'onboarding checksum', ok=False)
    assert recall.returncode == 1 and 'nothing on record' in recall.stdout
    assert 'lexical' in recall.stdout.lower()
    hook = run('python3', str(tools / 'khms_hook.py'), input=json.dumps(
        {'hook_event_name': 'SessionStart', 'session_id': 'onboarding'}))
    context = json.loads(hook.stdout)['hookSpecificOutput']['additionalContext']
    assert 'base:' in context and 'verified:' in context and 'if I were wrong:' in context
    assert 'mandatory reply lines' not in context and '-->' not in context
    assert 'MARK coverage unavailable' not in context
    assert 'no state' not in context and 'FileNotFoundError' not in context
    assert (Path(env['KHMS_ROOT']) / 'state/inject.log').exists()
    assert not (Path(env['KHMS_ROOT']) / 'tools').exists()
    assert not list(Path(env['KHMS_ROOT']).rglob('*.py'))
    sentinel = Path(env['KHMS_ROOT']) / 'journal/retained.md'
    sentinel.write_text('retained memory\n')
    (upstream / 'release.txt').write_text('new release\n')
    run('git', '-C', str(upstream), 'add', 'release.txt')
    run('git', '-C', str(upstream), '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
        'commit', '-qm', 'new release')
    approved_sha = run('git', '-C', str(upstream), 'rev-parse', 'HEAD').stdout.strip()
    run('git', '-C', str(clone), 'fetch', 'origin')
    assert not run('git', '-C', str(clone), 'status', '--porcelain').stdout
    lock = run('python3', str(tools / 'khms_paths.py'), '--code-lock-file').stdout.strip()
    with open(lock, 'a') as held:
        fcntl.flock(held, fcntl.LOCK_SH | fcntl.LOCK_NB)
        refused = run('flock', '-n', '-x', lock, 'git', '-C', str(clone), 'merge', '--ff-only', approved_sha, ok=False)
        assert refused.returncode == 1
        assert not (clone / 'release.txt').exists()
    run('flock', '-n', '-x', lock, 'git', '-C', str(clone), 'merge', '--ff-only', approved_sha)
    assert (clone / 'release.txt').read_text() == 'new release\n'
    assert sentinel.read_text() == 'retained memory\n'
    after = run('bash', str(tools / 'recall.sh'), '--lexical', 'onboarding checksum', ok=False)
    assert after.returncode == 1 and 'nothing on record' in after.stdout
    run('python3', str(tools / 'khms_hook.py'), input=json.dumps(
        {'hook_event_name': 'SessionStart', 'session_id': 'after-update'}))


def test_init_rejects_duplicate_settings_before_any_write(tmp_path):
    base = tmp_path / 'base'
    home = tmp_path / 'home'
    config = tmp_path / 'khms.env'
    home.mkdir()
    config.write_text(f'KHMS_ROOT={base}\nKHMS_READ_ONLY=0\nKHMS_READ_ONLY=1\n')
    env = {'PATH': os.environ['PATH'], 'HOME': str(home),
           'PYTHONDONTWRITEBYTECODE': '1', 'KHMS_CONFIG_FILE': str(config)}
    p = subprocess.run(['bash', str(REPO / 'tools/khms_init.sh')],
                       env=env, text=True, capture_output=True)
    assert p.returncode == 3 and 'duplicate' in p.stderr.lower(), (p.stdout, p.stderr)
    assert not base.exists()
    assert list(home.iterdir()) == []
