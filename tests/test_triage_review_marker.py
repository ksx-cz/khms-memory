"""The review marker of a triage unit is named by the unit's FILE STEM, by its writer and its reader alike (#173).

Until this fix the reader (`khms_review_state.triage_reviewed`) looked for `.reviewed-<stem>` while the writer
(`apply_triage.py`) wrote `.reviewed-<first date in the name>-triage`. For a plain `DATE-triage.md` the two
names agree; for a suffixed unit (`DATE-coordinator-triage.md`, `DATE-lerr10-triage.md`) they never did, so an
applied suffixed unit stayed "unreviewed" for the hook and the deputy, and applying it OVERWROTE the date unit's
marker with the suffixed unit's hash, which turned the nightly date unit pending as well.

`triage_marker_repair.py` is the one-off repair of units applied under the old writer: dry run by default,
writes `sha256=<unit as it is now>` under the stem name only for a unit whose `.applied.md` post-dates its last
edit, refuses a marker that already records a review of different content, and is idempotent.
"""
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import time

TOOLS = Path(__file__).resolve().parents[1] / "tools"

TRIAGE = """# triage 2099-01-01

## REVIEW VERDICT
"""


def make_base(tmp):
    base = tmp / "base"
    (base / "memory" / "know").mkdir(parents=True)
    (base / "memory" / "inbox").mkdir(parents=True)
    (base / "state" / "shared" / "reviewed").mkdir(parents=True)
    return base


def env_for(base, tmp):
    env = {k: v for k, v in os.environ.items() if not k.startswith("KHMS_")}
    env.update(KHMS_ROOT=str(base), KHMS_STATE=str(tmp / "agent"), KHMS_NO_SNAPSHOT="1", KHMS_READ_ONLY="0",
               PYTHONDONTWRITEBYTECODE="1")
    return env


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def reviewed(unit, base, tmp):
    r = subprocess.run([sys.executable, str(TOOLS / "khms_review_state.py"), "--triage-reviewed", str(unit)],
                       env=env_for(base, tmp), text=True, capture_output=True, timeout=60)
    assert r.returncode in (0, 1), r.stderr
    return r.returncode == 0


def apply(unit, base, tmp):
    return subprocess.run([sys.executable, str(TOOLS / "apply_triage.py"), str(unit), "--apply"],
                          env=env_for(base, tmp), text=True, capture_output=True, timeout=60)


def test_applying_a_suffixed_unit_marks_it_and_leaves_the_date_units_marker_alone(tmp_path):
    base = make_base(tmp_path)
    inbox, rev = base / "memory" / "inbox", base / "state" / "shared" / "reviewed"
    night = inbox / "2099-01-01-triage.md"
    night.write_text(TRIAGE.replace("triage 2099", "nightly triage 2099"), encoding="utf-8")
    assert apply(night, base, tmp_path).returncode == 0
    night_marker = rev / ".reviewed-2099-01-01-triage"
    before = (night_marker.read_bytes(), night_marker.stat().st_mtime_ns)
    assert reviewed(night, base, tmp_path)                     # positive control: the date unit

    suffixed = inbox / "2099-01-01-lerr10-triage.md"
    suffixed.write_text(TRIAGE.replace("triage 2099", "coordinator triage 2099"), encoding="utf-8")
    r = apply(suffixed, base, tmp_path)
    assert r.returncode == 0, r.stderr
    assert (rev / ".reviewed-2099-01-01-lerr10-triage").read_text().strip() == "sha256=" + sha(suffixed)
    assert reviewed(suffixed, base, tmp_path)
    assert (night_marker.read_bytes(), night_marker.stat().st_mtime_ns) == before
    assert reviewed(night, base, tmp_path)


def repair(base, tmp, *args):
    return subprocess.run([sys.executable, str(TOOLS / "triage_marker_repair.py"), *map(str, args)],
                          env=env_for(base, tmp), text=True, capture_output=True, timeout=60)


def test_the_marker_repair_is_a_dry_run_first_idempotent_and_refuses_what_it_cannot_prove(tmp_path):
    base = make_base(tmp_path)
    inbox, rev = base / "memory" / "inbox", base / "state" / "shared" / "reviewed"
    old = time.time() - 3600

    def unit(name, applied_after=True):
        u = inbox / name
        u.write_text(TRIAGE.replace("2099-01-01", name), encoding="utf-8")
        os.utime(u, (old, old))
        if applied_after is not None:
            a = inbox / name.replace(".md", ".applied.md")
            a.write_text(f"# apply_triage — {name} (APPLIED 2099-01-01T00:00:00+00:00)\n")
            t = old + (60 if applied_after else -60)
            os.utime(a, (t, t))
        return u

    suffixed = unit("2099-01-05-coordinator-triage.md")          # applied, never had a stem marker
    legacy = unit("2099-01-17-triage.md")                        # applied, empty marker OLDER than the unit
    (rev / ".reviewed-2099-01-17-triage").write_text("")
    os.utime(rev / ".reviewed-2099-01-17-triage", (old - 420, old - 420))
    unapplied = unit("2099-01-20-x-triage.md", applied_after=None)
    stale = unit("2099-01-21-y-triage.md", applied_after=False)  # edited after its apply: a review is owed
    other = unit("2099-01-22-z-triage.md")
    (rev / ".reviewed-2099-01-22-z-triage").write_text("sha256=" + "0" * 64 + "\n")
    date_marker = rev / ".reviewed-2099-01-05-triage"            # the date unit's own marker: never touched
    date_marker.write_text("sha256=" + "1" * 64 + "\n")
    assert not reviewed(suffixed, base, tmp_path) and not reviewed(legacy, base, tmp_path)

    args = [suffixed, f"{legacy}={sha(legacy)}", unapplied, stale, other]
    snapshot = {p.name: p.read_bytes() for p in rev.iterdir()}
    dry = repair(base, tmp_path, *args)
    assert dry.returncode == 1, dry.stdout + dry.stderr           # refusals make the run non-zero
    assert {p.name: p.read_bytes() for p in rev.iterdir()} == snapshot      # a dry run writes nothing
    assert dry.stdout.count("WOULD WRITE") == 2, dry.stdout
    assert dry.stdout.count("REFUSED") == 3, dry.stdout

    real = repair(base, tmp_path, "--apply", *args)
    assert real.returncode == 1 and real.stdout.count("WROTE") == 2, real.stdout + real.stderr
    assert reviewed(suffixed, base, tmp_path) and reviewed(legacy, base, tmp_path)
    assert not reviewed(unapplied, base, tmp_path) and not reviewed(stale, base, tmp_path)
    assert (rev / ".reviewed-2099-01-22-z-triage").read_text() == "sha256=" + "0" * 64 + "\n"
    assert date_marker.read_text() == "sha256=" + "1" * 64 + "\n"

    again = repair(base, tmp_path, "--apply", suffixed, f"{legacy}={sha(legacy)}")
    assert again.returncode == 0 and again.stdout.count("ALREADY") == 2 and "WROTE" not in again.stdout, again.stdout

    wrong = repair(base, tmp_path, f"{suffixed}={'2' * 64}")    # the unit is not the one the caller checked
    assert wrong.returncode == 1 and "REFUSED" in wrong.stdout, wrong.stdout


# Review FIX (Diblin, 2026-10-11), defect 2: `apply_triage.py --dry-run` writes the same `<stem>.applied.md` (K-05302),
# so its existence and mtime are no proof that the review was applied. The repair now requires the report's header to
# say APPLIED for THIS unit, and checks and writes under the `cards` lock the apply writer holds.
import fcntl  # noqa: E402
import threading  # noqa: E402


def dry_apply(unit, base, tmp):
    return subprocess.run([sys.executable, str(TOOLS / "apply_triage.py"), str(unit), "--dry-run"],
                          env=env_for(base, tmp), text=True, capture_output=True, timeout=60)


def test_a_dry_run_report_is_not_proof_of_application(tmp_path):
    base = make_base(tmp_path)
    inbox, rev = base / "memory" / "inbox", base / "state" / "shared" / "reviewed"
    unit = inbox / "2099-01-01-probe-triage.md"
    unit.write_text(TRIAGE, encoding="utf-8")
    d = dry_apply(unit, base, tmp_path)                              # the REAL dry-run writer
    assert d.returncode == 0, d.stderr
    report = unit.with_name(unit.stem + ".applied.md")
    assert "(DRY RUN " in report.read_text().splitlines()[0]
    assert not reviewed(unit, base, tmp_path)
    r = repair(base, tmp_path, "--apply", f"{unit}={sha(unit)}")
    assert r.returncode == 1 and "REFUSED" in r.stdout and "DRY RUN" in r.stdout, r.stdout + r.stderr
    assert not (rev / ".reviewed-2099-01-01-probe-triage").exists()
    assert not reviewed(unit, base, tmp_path)

    # a malformed report and a report of ANOTHER unit are refused the same way
    for name, header in (("2099-01-02-m-triage.md", "applied\n"),
                         ("2099-01-03-w-triage.md", "# apply_triage — 2099-01-09-other-triage.md "
                                                    "(APPLIED 2099-01-01T00:00:00+00:00)\n")):
        u = inbox / name
        u.write_text(TRIAGE, encoding="utf-8")
        u.with_name(u.stem + ".applied.md").write_text(header, encoding="utf-8")
        r = repair(base, tmp_path, "--apply", u)
        assert r.returncode == 1 and "REFUSED" in r.stdout, (name, r.stdout + r.stderr)
        assert not (rev / f".reviewed-{u.stem}").exists()


def test_a_real_apply_is_proof_and_the_repair_is_idempotent(tmp_path):
    base = make_base(tmp_path)
    rev = base / "state" / "shared" / "reviewed"
    unit = base / "memory" / "inbox" / "2099-01-01-coordinator-triage.md"
    unit.write_text(TRIAGE, encoding="utf-8")
    assert apply(unit, base, tmp_path).returncode == 0              # the REAL apply writer
    marker = rev / ".reviewed-2099-01-01-coordinator-triage"
    marker.unlink()                                                  # as under the old date-named writer
    assert not reviewed(unit, base, tmp_path)
    r = repair(base, tmp_path, "--apply", f"{unit}={sha(unit)}")
    assert r.returncode == 0 and "WROTE" in r.stdout, r.stdout + r.stderr
    assert reviewed(unit, base, tmp_path)
    before = (marker.read_bytes(), marker.stat().st_mtime_ns)
    again = repair(base, tmp_path, "--apply", f"{unit}={sha(unit)}")
    assert again.returncode == 0 and "ALREADY" in again.stdout, again.stdout + again.stderr
    assert (marker.read_bytes(), marker.stat().st_mtime_ns) == before


def test_the_repair_checks_and_writes_under_the_cards_lock(tmp_path):
    base = make_base(tmp_path)
    rev = base / "state" / "shared" / "reviewed"
    unit = base / "memory" / "inbox" / "2099-01-01-l-triage.md"
    unit.write_text(TRIAGE, encoding="utf-8")
    assert apply(unit, base, tmp_path).returncode == 0
    marker = rev / ".reviewed-2099-01-01-l-triage"
    marker.unlink()
    where = subprocess.run([sys.executable, "-c", "import khms_lock as L; print(L.runtime())"],
                           cwd=str(TOOLS), env=env_for(base, tmp_path), text=True, capture_output=True, timeout=60)
    assert where.returncode == 0, where.stderr
    lockfile = Path(where.stdout.strip()) / "cards.lock"
    lockfile.parent.mkdir(parents=True, exist_ok=True)
    with open(lockfile, "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)                               # the apply writer's lock, held
        out = {}
        t = threading.Thread(target=lambda: out.update(r=repair(base, tmp_path, "--apply", unit)))
        t.start()
        t.join(4)
        assert t.is_alive() and not marker.exists(), "the repair wrote while the cards lock was held"
        fcntl.flock(fh, fcntl.LOCK_UN)
    t.join(60)
    assert out["r"].returncode == 0 and "WROTE" in out["r"].stdout, out["r"].stdout + out["r"].stderr
    assert reviewed(unit, base, tmp_path)
