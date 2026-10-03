"""The configuration decides the write policy -- a settings FILE as much as the environment, and an invalid
selected configuration is an error, never a writer.

Three properties, each with the reproduction that broke it and the controls that keep the supported
interfaces alive:

1. khms_init.sh resolves the effective configuration (environment, explicit KHMS_CONFIG_FILE, the base's
   default config/khms.env) BEFORE it writes anything: a reader configured only in a file is refused (rc 4),
   an invalid configuration is refused (rc 3), and the base stays byte-for-byte as it was.
2. khms_init.sh prepares the layout the configuration names -- the configured shared state holds the one
   counter that the allocator then uses; no second counter appears at the default path.
3. An explicit-path tool (verify_relations.py --know/--archive) may run with no base selected at all, but a
   SELECTED configuration that cannot be resolved (missing file, malformed line, unresolved reference,
   cycle, bad KHMS_READ_ONLY) stops it with rc 3 before it rewrites anything.
"""
import os
import shutil
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.join(REPO, "tools")
FIXTURE = os.path.join(REPO, "tests", "fixtures", "smoke-inbox.md")


def fresh_id(n):
    """The id a FRESH base allocates n-th. Built, not written out: a literal low id would read, to a
    deployment's pre-publication disclosure scan, like one of that deployment's own card ids."""
    return "K-%05d" % n


def env_for(tmp_path, **kw):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    return dict(PATH=os.environ["PATH"], HOME=str(home), PYTHONDONTWRITEBYTECODE="1",
                KHMS_NO_SNAPSHOT="1", **kw)


def run(tool, env, *args):
    prefix = [sys.executable] if tool.endswith(".py") else []
    return subprocess.run(prefix + [os.path.join(TOOLS, tool), *map(str, args)],
                          env=env, text=True, capture_output=True)


def tree(root):
    """Every path below root with its content (files) -- a write of any kind changes it."""
    out = {}
    for p in sorted(root.rglob("*")):
        out[str(p.relative_to(root))] = p.read_bytes() if p.is_file() else None
    return out


# --- 1. init refuses a reader or an invalid configuration before any write --------------------------------

def test_init_respects_readonly_from_config(tmp_path):
    """Diblin F1: KHMS_READ_ONLY=1 only in <base>/config/khms.env."""
    root = tmp_path / "base"
    cfg = root / "config" / "khms.env"
    cfg.parent.mkdir(parents=True)
    cfg.write_text("KHMS_READ_ONLY=1\n")
    before = tree(root)
    r = run("khms_init.sh", env_for(tmp_path), root)
    assert (r.returncode, tree(root)) == (4, before), (r.returncode, r.stdout, r.stderr)
    assert "read-only" in r.stderr


def test_init_respects_readonly_from_explicit_config_file(tmp_path):
    root = tmp_path / "new base"
    cfg = tmp_path / "reader.env"
    cfg.write_text("KHMS_READ_ONLY=1\n")
    r = run("khms_init.sh", env_for(tmp_path, KHMS_CONFIG_FILE=str(cfg)), root)
    assert r.returncode == 4, (r.stdout, r.stderr)
    assert not root.exists()


def test_init_rejects_an_invalid_default_config_before_writing(tmp_path):
    root = tmp_path / "base"
    cfg = root / "config" / "khms.env"
    cfg.parent.mkdir(parents=True)
    for text in ("KHMS_READ_ONLY=1\nnot a pair\n", "KHMS_STATE=${NOWHERE}/state\n", "KHMS_READ_ONLY=yes\n"):
        cfg.write_text(text)
        before = tree(root)
        r = run("khms_init.sh", env_for(tmp_path), root)
        assert (r.returncode, tree(root)) == (3, before), (text, r.returncode, r.stdout, r.stderr)


def test_init_rejects_a_missing_explicit_config_before_writing(tmp_path):
    root = tmp_path / "new base"
    r = run("khms_init.sh", env_for(tmp_path, KHMS_CONFIG_FILE=str(tmp_path / "absent.env")), root)
    assert r.returncode == 3, (r.stdout, r.stderr)
    assert not root.exists()


def test_init_refuses_an_explicit_config_that_names_another_base(tmp_path):
    root, other = tmp_path / "base", tmp_path / "other"
    cfg = tmp_path / "k.env"
    cfg.write_text(f"KHMS_ROOT={other}\n")
    r = run("khms_init.sh", env_for(tmp_path, KHMS_CONFIG_FILE=str(cfg)), root)
    assert r.returncode == 3, (r.stdout, r.stderr)
    assert not root.exists() and not other.exists()


def test_control_init_refuses_environment_readonly(tmp_path):
    root = tmp_path / "base"
    r = run("khms_init.sh", env_for(tmp_path, KHMS_READ_ONLY="1"), root)
    assert r.returncode == 4
    assert not root.exists()


# --- 2. init prepares the configured layout, and allocation/approval use it -------------------------------

def _approve_smoke(root, env):
    inbox = root / "memory" / "inbox" / "2026-03-05.md"
    shutil.copy(FIXTURE, inbox)
    return run("approve_inbox.py", env, inbox)


def test_init_initializes_configured_shared_allocator(tmp_path):
    """Diblin F2: KHMS_SHARED_STATE from the environment, outside the base."""
    root = tmp_path / "base"
    shared = tmp_path / "shared authority"
    env = env_for(tmp_path, KHMS_ROOT=str(root), KHMS_SHARED_STATE=str(shared))
    init = run("khms_init.sh", env, root)
    assert init.returncode == 0, init.stderr
    alloc = run("new_card.sh", env)
    assert alloc.returncode == 0, (init.stdout, alloc.stdout, alloc.stderr)
    assert alloc.stdout.strip() == fresh_id(1)
    assert not (root / "state" / "shared").exists(), "a second authority at the default path"
    r = _approve_smoke(root, env)
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert "C1 -> " + fresh_id(2) in r.stdout
    assert (shared / "next_id").read_text().strip() == "4"


def test_init_layout_from_an_explicit_config_file(tmp_path):
    root = tmp_path / "base"
    shared = tmp_path / "shared authority"
    state = tmp_path / "agent state"
    cfg = tmp_path / "deploy.env"
    cfg.write_text(f"KHMS_ROOT={root}\nKHMS_SHARED_STATE='{shared}'\nKHMS_STATE={state}\n")
    env = env_for(tmp_path, KHMS_CONFIG_FILE=str(cfg))
    init = run("khms_init.sh", env)             # the root comes from the selected file
    assert init.returncode == 0, (init.stdout, init.stderr)
    assert (shared / "next_id").read_text().strip() == "1"
    assert state.is_dir()
    assert not (root / "state").exists()
    assert not (root / "config" / "khms.env").exists(), "a second, unread settings file"
    r = _approve_smoke(root, env)
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert "C1 -> " + fresh_id(1) in r.stdout


def test_init_layout_from_the_default_config_file_preserves_the_counter(tmp_path):
    root = tmp_path / "base"
    shared = tmp_path / "shared"
    cfg = root / "config" / "khms.env"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(f"KHMS_SHARED_STATE={shared}\nKHMS_PRINCIPAL=operator\n")
    shared.mkdir()
    (shared / "next_id").write_text("90007\n")
    init = run("khms_init.sh", env_for(tmp_path), root)
    assert init.returncode == 0, (init.stdout, init.stderr)
    assert (shared / "next_id").read_text() == "90007\n"
    assert cfg.read_text() == f"KHMS_SHARED_STATE={shared}\nKHMS_PRINCIPAL=operator\n"
    assert not (root / "state" / "shared").exists()
    env = env_for(tmp_path, KHMS_ROOT=str(root))
    alloc = run("new_card.sh", env)
    assert alloc.stdout.strip() == "K-90007", (alloc.stdout, alloc.stderr)


# --- 3. an invalid selected configuration never implies writer mode ---------------------------------------

def _inbox(tmp_path):
    root = tmp_path / "base"
    know = root / "memory" / "know"
    arch = root / "memory" / "archive" / "know"
    know.mkdir(parents=True)
    arch.mkdir(parents=True)
    cards = root / "memory" / "inbox.md"
    cards.write_text("## Cards\n\n### C1: heater\n\nA candidate missing its required relation.\n")
    return cards, know, arch


def test_invalid_explicit_config_cannot_authorize_rewrite(tmp_path):
    """Diblin F3: explicit reader config with an unresolved reference."""
    cards, know, arch = _inbox(tmp_path)
    before = cards.read_bytes()
    cfg = tmp_path / "reader.env"
    cfg.write_text("KHMS_READ_ONLY=1\nKHMS_ROOT=${UNRESOLVED_ROOT}\n")
    env = env_for(tmp_path, KHMS_CONFIG_FILE=str(cfg))
    r = run("verify_relations.py", env, cards, "--know", know, "--archive", arch)
    assert (r.returncode, cards.read_bytes()) == (3, before), (r.returncode, r.stdout, r.stderr)


def test_every_kind_of_invalid_selection_stops_before_the_rewrite(tmp_path):
    cards, know, arch = _inbox(tmp_path)
    before = cards.read_bytes()
    cfg = tmp_path / "k.env"
    cases = {
        "missing explicit file": ({"KHMS_CONFIG_FILE": str(tmp_path / "absent.env")}, None),
        "malformed line": ({"KHMS_CONFIG_FILE": str(cfg)}, "KHMS_READ_ONLY=1\nnot a pair\n"),
        "cycle": ({"KHMS_CONFIG_FILE": str(cfg)}, "KHMS_ROOT=${KHMS_STATE}\nKHMS_STATE=${KHMS_ROOT}/s\n"),
        "file without a root": ({"KHMS_CONFIG_FILE": str(cfg)}, "KHMS_READ_ONLY=1\n"),
        "bad read-only value, no base": ({"KHMS_READ_ONLY": "yes"}, None),
        "relative root": ({"KHMS_ROOT": "relative/base"}, None),
    }
    for name, (extra, text) in cases.items():
        if text is not None:
            cfg.write_text(text)
        r = run("verify_relations.py", env_for(tmp_path, **extra), cards, "--know", know, "--archive", arch)
        assert (r.returncode, cards.read_bytes()) == (3, before), (name, r.returncode, r.stdout, r.stderr)
        assert "Traceback" not in r.stderr, name


def test_control_no_base_selected_explicit_paths_still_rewrite(tmp_path):
    """The supported standalone interface: nothing selects a base, explicit paths, writer by default."""
    cards, know, arch = _inbox(tmp_path)
    before = cards.read_bytes()
    r = run("verify_relations.py", env_for(tmp_path), cards, "--know", know, "--archive", arch)
    assert r.returncode == 1, (r.stdout, r.stderr)
    assert cards.read_bytes() != before
    assert "DROPPED" in cards.read_text()


def test_control_no_base_selected_environment_reader_refuses(tmp_path):
    cards, know, arch = _inbox(tmp_path)
    before = cards.read_bytes()
    env = env_for(tmp_path, KHMS_READ_ONLY="1")
    r = run("verify_relations.py", env, cards, "--know", know, "--archive", arch)
    assert (r.returncode, cards.read_bytes()) == (4, before), (r.stdout, r.stderr)


def test_control_resolver_rejects_the_invalid_config(tmp_path):
    cfg = tmp_path / "reader.env"
    cfg.write_text("KHMS_READ_ONLY=1\nKHMS_ROOT=${UNRESOLVED_ROOT}\n")
    for tool in ("khms_paths.py", "khms_paths.sh"):
        r = run(tool, env_for(tmp_path, KHMS_CONFIG_FILE=str(cfg)), "--all")
        assert r.returncode == 3, (tool, r.stdout, r.stderr)
