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
            a.write_text("applied\n")
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
