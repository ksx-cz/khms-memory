"""The two inputs the nightly triage judge depends on before its first call (#173, first shared night 2026-10-11).

  * PRICE. The judge bound reserves each attempt's estimated worst case before the call. The judge alias
    `sonnet` bills as `claude-sonnet-5-5`; with no row for it in tok_account's price table the estimate was
    infinite, so the very first attempt was refused and the night recorded `outcomes {'BUDGET': 250}` with
    $0 spent -- a pricing gap reported as an exhausted budget. The real judge model must price finitely, and
    a model with no price must fail with its NAME, never as a BUDGET stop.
  * DENSE INDEX. The candidate builder read the embedding index at `<base>/memory/.embed/<slug>.npz`, the
    location before #173 moved it to KHMS_EMBED_DIR; the night logged "dense channel unavailable" and the
    triage ran on the lexical channel alone. The index is read where the path resolver says it is.

Isolated: synthetic cards in a temporary base, the real tool in `--pairs-only` mode, no model, no network.
"""
import math
import os
from pathlib import Path
import subprocess
import sys

import pytest

TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))


# --- (c) the judge's price ----------------------------------------------------------------------------------
def test_the_real_judge_model_has_a_finite_worst_case():
    import khms_contradict as C
    model = C.CLAUDE_MODEL_IDS["sonnet"]
    b = C.JudgeBudget(15.0, model_id=model)
    w = b.worst_case_usd("a judge prompt " * 500)
    assert math.isfinite(w) and w > 0, (model, w)
    # and the bound therefore lets the first attempt through instead of refusing it as BUDGET
    assert b.reserve("a judge prompt " * 500) == w and b.stop is None


def test_an_unknown_judge_model_fails_by_name_not_as_budget():
    import khms_contradict as C
    name = "claude-no-such-model-0"
    with pytest.raises(C.JudgeModelUnpriced) as e:
        C.JudgeBudget(15.0, model_id=name)
    assert name in str(e.value)
    assert not issubclass(C.JudgeModelUnpriced, C.JudgeBudgetStop)   # never counted as a BUDGET outcome


def test_an_unknown_judge_model_stops_the_triage_run_loudly(tmp_path):
    root = make_base(tmp_path)
    env = env_for(root, tmp_path, KHMS_JUDGE="sonnet")
    r = subprocess.run([sys.executable, "-c",
                        "import sys; sys.path.insert(0, sys.argv[1]); import khms_contradict as C; "
                        "C.CLAUDE_MODEL_IDS['sonnet'] = 'claude-no-such-model-0'; "
                        "sys.argv = ['khms_contradict.py', '--limit', '5', '--max-usd', '15', "
                        "'--out', sys.argv[2], '--json', sys.argv[3]]; sys.exit(C.main())",
                        str(TOOLS), str(tmp_path / "out.md"), str(tmp_path / "out.json")],
                       env=env, text=True, capture_output=True, timeout=120)
    assert r.returncode != 0, r.stderr
    assert "claude-no-such-model-0" in r.stderr and "BUDGET" not in r.stderr, r.stderr[-2000:]


# --- (d) the dense index location ------------------------------------------------------------------------------
WORDS = ["flux capacitor", "zorbium coil", "quantel lattice", "brimwire spindle"]
N_CARDS = 12


def make_base(tmp):
    root = tmp / "base"
    for d in ("memory/know", "memory/inbox/.staging", "memory/views", "state/shared", "journal"):
        (root / d).mkdir(parents=True, exist_ok=True)
    for n in range(1, N_CARDS + 1):
        cid = "K-%05d" % (90100 + n)
        w = WORDS[n % len(WORDS)]
        body = f"The {w} number {n} sits on rig {n * 13} and nothing else is said about it."
        (root / "memory/know" / f"{cid}.md").write_text(
            f"---\nid: {cid}\ntype: fact\nlevel: observation\nstatus: active\ntags: [grommet]\n"
            f"date: '2099-01-01'\n---\n{body}\n")
    return root


def env_for(root, tmp, **extra):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("KHMS_", "NIGHTLY_", "TRIAGE_"))}
    env.update(KHMS_ROOT=str(root), KHMS_STATE=str(tmp / "agent"), KHMS_READ_ONLY="1", KHMS_NO_SNAPSHOT="1",
               PYTHONDONTWRITEBYTECODE="1")
    env.update(extra)
    return env


def write_index(path, ids):
    np = pytest.importorskip("numpy")
    path.parent.mkdir(parents=True, exist_ok=True)
    v = np.eye(len(ids), dtype="float32")
    v[1] = v[0] + 0.01                 # cards 1 and 2 are dense neighbours (cosine ~1), the rest orthogonal
    np.savez(path, ids=np.array(ids), vectors=v.astype("float16"))


def dense_edges(root, tmp):
    r = subprocess.run([sys.executable, str(TOOLS / "khms_contradict.py"), "--pairs-only", "--limit", "50"],
                       env=env_for(root, tmp), text=True, capture_output=True, timeout=120)
    assert r.returncode == 0, r.stderr
    line = [ln for ln in r.stderr.splitlines() if "+ dense:" in ln]
    assert line, r.stderr
    return int(line[0].split("+ dense:")[1].split()[0]), r.stderr


def test_triage_reads_the_dense_index_where_the_resolver_puts_it(tmp_path):
    root = make_base(tmp_path)
    resolved = subprocess.run([sys.executable, str(TOOLS / "khms_paths.py"), "KHMS_EMBED_DIR"],
                              env=env_for(root, tmp_path), text=True, capture_output=True, timeout=60)
    assert resolved.returncode == 0 and resolved.stdout.strip(), resolved.stderr
    embed_dir = Path(resolved.stdout.strip())
    assert embed_dir != root / "memory" / ".embed"      # the fixture exercises the moved location
    ids = ["K-%05d" % (90100 + n) for n in range(1, N_CARDS + 1)]
    # control first: an index ONLY at the pre-#173 location is not the index (no silent fallback)
    write_index(root / "memory" / ".embed" / "bge.npz", ids)
    n_old, err_old = dense_edges(root, tmp_path)
    assert n_old == 0, err_old
    # the index at the resolved location is read
    write_index(embed_dir / "bge.npz", ids)
    n, err = dense_edges(root, tmp_path)
    assert n > 0 and "dense channel unavailable" not in err, err
