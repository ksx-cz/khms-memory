"""Generic dispatch preserves configuration, personal evidence and containment."""
import os
from pathlib import Path
import shutil
import subprocess

import pytest

TOOLS = Path(__file__).resolve().parents[1] / 'tools'


def fixture_tools(tmp_path):
    tools = tmp_path / 'tools'
    tools.mkdir()
    shutil.copy2(TOOLS / 'khms_exec.sh', tools)
    (tools / 'khms_paths.py').write_text(
        'import os,sys\n'
        'if os.environ.get("INVALID_CONFIG"): sys.exit(3)\n'
        'print(os.environ.get("KHMS_EMBED_PY", "python3") if sys.argv[1]=="KHMS_EMBED_PY" '
        'else os.environ.get("KHMS_STATE", "/fixture"))\n')
    return tools


def run(tools, name, *args, **env):
    return subprocess.run(['bash', str(tools / 'khms_exec.sh'), name, *args],
                          env=dict(os.environ, **env), text=True, capture_output=True)


def test_nested_entry_and_symlink_escape(tmp_path):
    tools = fixture_tools(tmp_path)
    (tools / 'eval').mkdir()
    (tools / 'eval/report.py').write_text('print("contained")\n')
    outside = tmp_path / 'outside.py'
    outside.write_text('print("escaped")\n')
    (tools / 'escape.py').symlink_to(outside)
    assert run(tools, 'eval/report.py').stdout.strip() == 'contained'
    for name in ('../outside.py', str(outside), 'escape.py', 'eval/../../outside.py'):
        p = run(tools, name)
        assert p.returncode == 3 and 'escaped' not in p.stdout


def test_personal_logs_override_stale_environment(tmp_path):
    tools = fixture_tools(tmp_path)
    (tools / 'probe.py').write_text('import os,json\nprint(json.dumps([os.environ["KHMS_RECALL_LOG"],'
                                  'os.environ["KHMS_RECALL_JSONL"]]))\n')
    state = str(tmp_path / 'personal state')
    p = run(tools, 'probe.py', KHMS_STATE=state,
            KHMS_RECALL_LOG='/stale/log', KHMS_RECALL_JSONL='/stale/jsonl')
    import json
    assert p.returncode == 0
    assert json.loads(p.stdout) == [state + '/recall.log', state + '/recall.jsonl']


def test_selected_embedding_interpreter(tmp_path):
    tools = fixture_tools(tmp_path)
    interpreter = tmp_path / 'runtime with spaces/python'
    interpreter.parent.mkdir()
    interpreter.write_text('#!/bin/sh\nprintf "selected:%s\\n" "$*"\n')
    interpreter.chmod(0o755)
    (tools / 'embed_index.py').write_text('# fixture\n')
    p = run(tools, 'embed_index.py', '--model', 'bge', KHMS_EMBED_PY=str(interpreter))
    assert p.returncode == 0
    assert p.stdout.strip() == 'selected:' + str(tools / 'embed_index.py') + ' --model bge'


def test_invalid_config_stops_before_entrypoint(tmp_path):
    tools = fixture_tools(tmp_path)
    (tools / 'writer.py').write_text('raise RuntimeError("entrypoint ran")\n')
    p = run(tools, 'writer.py', INVALID_CONFIG='1')
    assert p.returncode == 3 and 'entrypoint ran' not in p.stderr


def test_readonly_writer_policy_survives_dispatch(tmp_path):
    root = tmp_path / 'base'
    config = tmp_path / 'reader.env'
    config.write_text(f'KHMS_ROOT={root}\nKHMS_READ_ONLY=1\n')
    p = run(TOOLS, 'khms_init.sh', str(root), KHMS_CONFIG_FILE=str(config), KHMS_ROOT=str(root))
    assert p.returncode == 4, p.stdout + p.stderr
    assert not root.exists()
