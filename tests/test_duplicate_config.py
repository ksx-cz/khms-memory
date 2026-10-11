"""Config keys are unique within a file; failed pattern loads never become defaults."""
import os
from pathlib import Path
import subprocess
import sys

import pytest

TOOLS = Path(__file__).resolve().parents[1] / "tools"


def run_python(tmp_path, script, **settings):
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
           "HOME": str(tmp_path), "PYTHONPATH": str(TOOLS),
           "PYTHONDONTWRITEBYTECODE": "1", **settings}
    return subprocess.run([sys.executable, "-c", script], env=env,
                          cwd=tmp_path, text=True, capture_output=True)


@pytest.mark.parametrize("reader", ["python", "shell"])
@pytest.mark.parametrize("second", ["first", "other", ""])
@pytest.mark.parametrize("selection", ["explicit", "default"])
def test_duplicate_settings_rejected_even_with_environment_override(tmp_path, second, selection, reader):
    root = tmp_path / "base"
    cfg = root / "config" / "khms.env"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(f"# settings\nKHMS_STATE=first\n\nKHMS_STATE={second}\n")
    settings = {"KHMS_ROOT": str(root), "KHMS_STATE": "/environment"}
    if selection == "explicit":
        settings["KHMS_CONFIG_FILE"] = str(cfg)
    result = run_python(tmp_path, "import khms_paths as P; print(P.get('KHMS_STATE'))", **settings)
    if reader == "shell":
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
               "HOME": str(tmp_path), "PYTHONDONTWRITEBYTECODE": "1", **settings}
        result = subprocess.run(["bash", str(TOOLS / "khms_paths.sh"), "KHMS_STATE"],
                                env=env, cwd=tmp_path, text=True, capture_output=True)
    assert result.returncode == 3, result.stdout + result.stderr
    assert str(cfg) in result.stderr
    assert "duplicate key KHMS_STATE" in result.stderr
    assert "line 4" in result.stderr
    assert "first defined at line 2" in result.stderr
    assert not result.stdout


@pytest.mark.parametrize("second", ["first", "other", ""])
def test_duplicate_patterns_fail_every_attempt(tmp_path, second):
    cfg = tmp_path / "patterns.env"
    cfg.write_text(f"# patterns\nHOOK_DOMAIN=first\n\nHOOK_DOMAIN={second}\n")
    script = """
import khms_paths as P
import khms_patterns as patterns
for call in (lambda: patterns.value('HOOK_DOMAIN', 'fallback'),
             lambda: patterns.extra('HOOK_DOMAIN'),
             lambda: patterns.words('HOOK_DOMAIN', ['fallback']),
             lambda: patterns.labelled('LINT_BLOCK_')):
    try:
        call()
    except P.KhmsPathError as exc:
        print(exc)
    else:
        raise AssertionError('failed config load was cached as defaults')
"""
    result = run_python(tmp_path, script, KHMS_PATTERNS=str(cfg))
    assert result.returncode == 0, result.stdout + result.stderr
    errors = result.stdout.splitlines()
    assert len(errors) == 4
    for error in errors:
        assert str(cfg) in error
        assert "duplicate key HOOK_DOMAIN" in error
        assert "line 4" in error and "first defined at line 2" in error


def test_separate_configs_and_environment_override_remain_valid(tmp_path):
    settings = tmp_path / "settings.env"
    patterns = tmp_path / "patterns.env"
    settings.write_text(f"KHMS_ROOT={tmp_path / 'base'}\nHOOK_DOMAIN=settings\nKHMS_STATE=/file\n")
    patterns.write_text("HOOK_DOMAIN=patterns\nLINT_BLOCK_1=first::one\nLINT_BLOCK_2=second::two\n")
    script = """
import khms_paths as P
import khms_patterns as patterns
assert P.get('KHMS_STATE') == '/environment'
assert patterns.value('HOOK_DOMAIN', 'fallback') == 'patterns'
assert [label for label, _ in patterns.labelled('LINT_BLOCK_')] == ['first', 'second']
"""
    result = run_python(tmp_path, script, KHMS_CONFIG_FILE=str(settings),
                        KHMS_PATTERNS=str(patterns), KHMS_STATE="/environment")
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("second", ["first", "other", ""])
def test_duplicate_messages_fail_every_attempt(tmp_path, second):
    cfg = tmp_path / "messages.env"
    cfg.write_text(f"# messages\nJOB_MARKERS=first\n\nJOB_MARKERS={second}\n")
    script = """
import khms_paths as P
import khms_messages as messages
for _ in range(2):
    try:
        messages.job_markers()
    except P.KhmsPathError as exc:
        print(exc)
    else:
        raise AssertionError('failed messages load was cached as defaults')
"""
    result = run_python(tmp_path, script, KHMS_MESSAGES=str(cfg))
    assert result.returncode == 0, result.stdout + result.stderr
    errors = result.stdout.splitlines()
    assert len(errors) == 2
    for error in errors:
        assert str(cfg) in error and "duplicate key JOB_MARKERS" in error
        assert "line 4" in error and "first defined at line 2" in error
