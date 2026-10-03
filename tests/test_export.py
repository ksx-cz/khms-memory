"""khms_export.sh: the read-only export of a base (layout, archive switch, the --delete guard, the manifest,
the code-use lock) on a throwaway base."""
import json
import os
import subprocess

import pytest

def cid(n):                       # card ids of a fresh base, built, never written as literals
    return "K-%05d" % n


TOOLS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools")
EXPORT = os.path.join(TOOLS, "khms_export.sh")


@pytest.fixture
def base(tmp_path):
    root = tmp_path / "base"
    (root / "memory" / "know").mkdir(parents=True)
    (root / "memory" / "archive" / "know").mkdir(parents=True)
    for i in (11, 12, 13):
        (root / "memory" / "know" / (cid(i) + ".md")).write_text("card %d\n" % i)
    (root / "memory" / "know" / "notes.txt").write_text("not a card\n")
    (root / "memory" / "archive" / "know" / (cid(10) + ".md")).write_text("old\n")
    return root


def run(tmp_path, root, *args):
    env = {k: v for k, v in os.environ.items() if not k.startswith("KHMS_")}
    env.update(KHMS_ROOT=str(root), KHMS_CONFIG_DIR=str(tmp_path / "cfg"), KHMS_STATE=str(tmp_path / "agent"))
    return subprocess.run(["bash", EXPORT, *args], env=env, capture_output=True, text=True, timeout=120)


def test_cards_only_and_a_manifest(tmp_path, base):
    out = tmp_path / "out"
    p = run(tmp_path, base, str(out))
    assert p.returncode == 0, p.stderr
    assert sorted(os.listdir(out / "know")) == [cid(i) + ".md" for i in (11, 12, 13)]
    assert not (out / "archive").exists()
    m = json.loads((out / "manifest.json").read_text())
    assert m["cards"] == 3 and m["archived_cards"] == 0 and m["tool"] == "khms_export.sh"
    assert m["source_dir"] == str(base / "memory" / "know")


def test_archive_switch_and_removal_of_a_card_that_left(tmp_path, base):
    out = tmp_path / "out"
    assert run(tmp_path, base, str(out), "--with-archive").returncode == 0
    assert os.listdir(out / "archive" / "know") == [cid(10) + ".md"]
    (base / "memory" / "know" / (cid(13) + ".md")).unlink()
    assert run(tmp_path, base, str(out)).returncode == 0
    assert sorted(os.listdir(out / "know")) == [cid(11) + ".md", cid(12) + ".md"]
    assert not (out / "archive").exists()


def test_refuses_a_foreign_directory(tmp_path, base):
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / "keep").write_text("x")
    p = run(tmp_path, base, str(foreign))
    assert p.returncode == 1 and "refusing" in p.stderr
    assert os.listdir(foreign) == ["keep"]


def test_refuses_while_an_update_holds_the_code_lock(tmp_path, base):
    env = {k: v for k, v in os.environ.items() if not k.startswith("KHMS_")}
    env.update(KHMS_ROOT=str(base), KHMS_CONFIG_DIR=str(tmp_path / "cfg"), KHMS_STATE=str(tmp_path / "agent"))
    lock = subprocess.run(["bash", "-c", '. "%s/khms_paths.sh"; khms_load; khms_code_lock_file' % TOOLS],
                          env=env, capture_output=True, text=True).stdout.strip()
    os.makedirs(os.path.dirname(lock), exist_ok=True)
    out = tmp_path / "out"
    p = subprocess.run(["flock", "-x", lock, "bash", EXPORT, str(out)], env=env, capture_output=True, text=True,
                       timeout=120)
    assert p.returncode == 5 and "being updated" in p.stderr, (p.returncode, p.stderr)
    assert not out.exists()
