"""Review findings F3 and F4 of #173 part 2a (reproducers by the reviewer, kept as repo tests).

F3: an export never writes to or deletes from the base it reads. A reader (KHMS_READ_ONLY=1) may build an
independent copy OUTSIDE the base (positive control), never inside it; a target that overlaps a source tree is
refused even when a file named manifest.json is present (the file proves nothing unless this tool wrote it), and
a symlinked alias of the source is refused the same way.
F4: the review ping does not report a deliberately disabled triage stage (KHMS_NIGHT_TRIAGE unset) as a sweep
that did not finish; with the stage configured the same fixture DOES raise the alarm (positive control).
"""
import os
import subprocess
from pathlib import Path

import pytest

CLONE = Path(__file__).resolve().parent.parent
CARD = "K-%05d.md" % 123


def env_for(tmp_path):
    root = tmp_path / "base"
    for name in ("memory/know", "memory/archive/know", "memory/inbox", "journal", "state/shared/reviewed"):
        (root / name).mkdir(parents=True, exist_ok=True)
    card = "K-%05d.md" % 123
    (root / "memory/know" / card).write_text("active card\n")
    (root / "memory/archive/know" / card).write_text("archived card\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith("KHMS_")}
    env.update(KHMS_ROOT=str(root), KHMS_STATE=str(tmp_path / "agent"),
               KHMS_CONFIG_DIR=str(tmp_path / "config"), PYTHONDONTWRITEBYTECODE="1")
    return root, env


def export(env, target, *extra):
    return subprocess.run(["bash", str(CLONE / "tools/khms_export.sh"), str(target), *extra],
                          env=env, capture_output=True, text=True, timeout=30)


@pytest.mark.parametrize("inside", [True, False])
def test_reader_export_protects_the_base_but_allows_an_external_copy(tmp_path, inside):
    root, env = env_for(tmp_path)
    env["KHMS_READ_ONLY"] = "1"
    out = root / "export" if inside else tmp_path / "export"
    r = export(env, out)
    if inside:
        assert r.returncode != 0 and not out.exists(), (r.returncode, r.stdout, r.stderr)
    else:
        assert r.returncode == 0 and (out / "manifest.json").exists(), r.stderr


def test_export_refuses_source_overlap_even_if_a_manifest_is_present(tmp_path):
    root, env = env_for(tmp_path)
    target = root / "memory"
    (target / "manifest.json").write_text("{}\n")
    archived = target / "archive/know" / ("K-%05d.md" % 123)
    r = export(env, target)
    assert r.returncode != 0 and archived.exists(), (r.returncode, archived.exists(), r.stdout, r.stderr)


@pytest.mark.parametrize("target_rel", ["memory/know", "memory/archive", "memory/archive/know/sub"])
def test_export_refuses_every_overlap_with_a_source_tree(tmp_path, target_rel):
    root, env = env_for(tmp_path)
    before = sorted(p.read_text() for p in (root / "memory").rglob("K-*.md"))
    r = export(env, root / target_rel, "--with-archive")
    assert r.returncode != 0 and "overlaps the source tree" in r.stderr, (r.returncode, r.stderr)
    assert sorted(p.read_text() for p in (root / "memory").rglob("K-*.md")) == before


def test_a_symlinked_alias_of_the_source_is_refused(tmp_path):
    root, env = env_for(tmp_path)
    alias = tmp_path / "alias"
    alias.symlink_to(root / "memory")
    r = export(env, alias)
    assert r.returncode != 0 and (root / "memory/archive/know" / CARD).exists(), r.stderr


def test_a_writer_may_export_inside_its_base_outside_the_sources_and_rerun(tmp_path):
    root, env = env_for(tmp_path)
    out = root / "export"
    assert export(env, out).returncode == 0
    r = export(env, out, "--with-archive")                    # its own previous export: accepted
    assert r.returncode == 0, r.stderr
    assert (out / "archive/know" / CARD).exists() and (root / "memory/archive/know" / CARD).exists()


def test_a_foreign_manifest_does_not_open_a_directory_for_delete(tmp_path):
    _root, env = env_for(tmp_path)
    foreign = tmp_path / "theirs"
    foreign.mkdir()
    (foreign / "manifest.json").write_text('{"tool": "something-else"}\n')
    (foreign / "keep.txt").write_text("not ours\n")
    r = export(env, foreign)
    assert r.returncode != 0 and (foreign / "keep.txt").exists(), r.stderr


@pytest.mark.parametrize("enabled", [False, True])
def test_ping_does_not_claim_a_disabled_triage_failed(tmp_path, enabled):
    root, env = env_for(tmp_path)
    for stamp in ("last-nightly", "last-weekly"):
        (root / "state/shared" / stamp).write_text("2099-01-01T12:00:00Z\n")
    env.update(KHMS_PING_DRYRUN="1", KHMS_PING_TODAY="2099-01-01",
               KHMS_PING_PGREP="no_such_fixture_triage_process_173b")
    if enabled:
        env["KHMS_NIGHT_TRIAGE"] = "/not-executed-by-ping/triage"
    r = subprocess.run(["bash", str(CLONE / "tools/khms_review_ping.sh")],
                       env=env, capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    assert ("the sweep did not finish" in r.stdout) == enabled, r.stdout


def test_an_existing_triage_file_is_still_offered_with_the_stage_off(tmp_path):
    root, env = env_for(tmp_path)
    for stamp in ("last-nightly", "last-weekly"):
        (root / "state/shared" / stamp).write_text("2099-01-02T12:00:00Z\n")
    (root / "memory/inbox/2099-01-01-triage.md").write_text("# triage\n")
    env.update(KHMS_PING_DRYRUN="1", KHMS_PING_TODAY="2099-01-02",
               KHMS_PING_PGREP="no_such_fixture_triage_process_173b")
    r = subprocess.run(["bash", str(CLONE / "tools/khms_review_ping.sh")],
                       env=env, capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    assert "2099-01-01" in r.stdout and "triage: PENDING" in r.stdout, r.stdout
