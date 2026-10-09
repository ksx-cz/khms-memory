"""Behavioral tests of two writers using one base, without a live service."""
import concurrent.futures
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import time

TOOLS = Path(__file__).resolve().parents[1] / 'tools'
sys.path.insert(0, str(TOOLS))
import khms_files
from khms_runtime import default_runtime

CARD = '''---
id: C1
type: fact
level: observation
status: active
tags: [testing]
scope: universal
evidence: measured
source: fixture
date: 2026-01-01
---
The relay opens when its supply is removed.
'''


def base(tmp_path):
    root = tmp_path / 'base'
    env = dict(os.environ, KHMS_ROOT=str(root), KHMS_STATE=str(tmp_path / 'agent'),
               KHMS_RUNTIME=str(tmp_path / 'runtime'), PYTHONDONTWRITEBYTECODE='1')
    subprocess.run(['bash', str(TOOLS/'khms_init.sh'), str(root)], env=env, check=True, capture_output=True)
    unit = root/'memory/inbox/2026-01-01.md'
    unit.write_text(CARD)
    return root, env, unit


def approve(env, path):
    return subprocess.run([sys.executable, str(TOOLS/'approve_inbox.py'), str(path)],
                          env=env, text=True, capture_output=True)


def test_two_agents_approve_same_unit_once(tmp_path):
    root, env, unit = base(tmp_path)
    other = unit.with_name('2026-01-01.approve.md')
    other.write_text(CARD)
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        runs = [pool.submit(approve, dict(env, HOME=str(tmp_path/str(i))), p)
                for i,p in enumerate((unit, other))]
        for run in runs:
            r=run.result(timeout=15)
            assert r.returncode == 0, r.stdout+r.stderr
    assert len(list((root/'memory/know').glob('*.md'))) == 1
    assert (root/'state/shared/next_id').read_text().strip() == '2'
    assert not (root/'state/shared/views-dirty').exists()
    assert 'relay' in (root/'memory/views/recent.md').read_text().lower() or (root/'memory/MEMORY.md').exists()


def test_approval_waits_for_card_transaction(tmp_path):
    root, env, unit = base(tmp_path)
    runtime = Path(env['KHMS_RUNTIME']); runtime.mkdir(exist_ok=True)
    with (runtime/'cards.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        proc = subprocess.Popen([sys.executable,str(TOOLS/'approve_inbox.py'),str(unit)],env=env,
                                stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        try:
            time.sleep(.2)
            assert proc.poll() is None
            assert not list((root/'memory/know').glob('*.md'))
        finally:
            fcntl.flock(lock,fcntl.LOCK_UN)
            out,err=proc.communicate(timeout=15)
        assert proc.returncode == 0,out+err


def test_partial_approval_resumes_reserved_ids(tmp_path):
    root,env,unit=base(tmp_path)
    unit.write_text(CARD+ '\n'+CARD.replace('C1','C2').replace('relay opens','sensor closes'))
    code='''import sys
sys.path.insert(0,sys.argv.pop(1))
import approve_inbox as A
real=A.L.atomic_text
written=0
def fail(path,text):
 global written
 real(path,text)
 if '/memory/know/' in str(path):
  written+=1
  if written==1: raise RuntimeError('injected crash after first published card')
A.L.atomic_text=fail
A.main()
'''
    r=subprocess.run([sys.executable,'-c',code,str(TOOLS),str(unit)],env=env,capture_output=True,text=True)
    assert r.returncode !=0 and 'injected crash' in r.stderr
    assert (root/'state/shared/views-dirty').exists()
    r=approve(env,unit)
    assert r.returncode==0,r.stdout+r.stderr
    assert len(list((root/'memory/know').glob('*.md')))==2
    assert (root/'state/shared/next_id').read_text().strip()=='3'


def test_conflict_files_never_become_cards(tmp_path):
    for name in ('K-90001.md','K-90001 (conflicted copy 2026).md','K-extra.md'):
        (tmp_path/name).write_text('fixture')
    assert [Path(p).name for p in khms_files.glob(str(tmp_path/'K-*.md'))]==['K-90001.md']


def test_runtime_independent_of_home_and_symlink(tmp_path,monkeypatch):
    root=tmp_path/'base';root.mkdir();alias=tmp_path/'alias';alias.symlink_to(root)
    monkeypatch.setenv('HOME',str(tmp_path/'a'));a=default_runtime(str(root))
    monkeypatch.setenv('HOME',str(tmp_path/'b'));assert a==default_runtime(str(alias))
    assert not a.startswith(str(root))


def test_triage_waits_before_reading_or_writing_cards(tmp_path):
    root,env,unit=base(tmp_path)
    tri=root/'memory/inbox/2026-01-01-triage.md'
    tri.write_text('# triage\n\n## REVIEW VERDICT\n')
    runtime=Path(env['KHMS_RUNTIME']);runtime.mkdir(exist_ok=True)
    with (runtime/'cards.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        proc=subprocess.Popen([sys.executable,str(TOOLS/'apply_triage.py'),str(tri),'--apply'],
                              env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        try:
            time.sleep(.2)
            assert proc.poll() is None
            assert not tri.with_suffix('.applied.md').exists()
        finally:
            fcntl.flock(lock,fcntl.LOCK_UN)
            out,err=proc.communicate(timeout=15)
        assert proc.returncode==0,out+err


def test_shared_config_rejects_private_lock_or_shared_personal_state(tmp_path):
    root,env,unit=base(tmp_path)
    assert approve(env,unit).returncode==0
    (root/'config/khms.env').write_text('KHMS_SHARED=1\nKHMS_RUNTIME='+env['KHMS_RUNTIME']+'\n')
    for key in ('KHMS_RUNTIME','KHMS_STATE'):
        broken=dict(env,**{key:str(root/'wrong')})
        for script in ('khms_paths.py','khms_paths.sh'):
            r=subprocess.run([sys.executable if script.endswith('.py') else 'bash',str(TOOLS/script),'--root'],
                             env=broken,capture_output=True,text=True)
            assert r.returncode==3,r.stdout+r.stderr
    assert approve(env,unit).returncode==0


def test_fresh_and_older_open_evidence_are_recalled_separately(tmp_path):
    root,env,unit=base(tmp_path)
    (root/'journal/2026-01-02.md').write_text('MARK[finding] relay supply removed (agent test)\n')
    (root/'state/shared/last-nightly').write_text('2026-01-01T00:00:00\n')
    ledger=root/'memory/ledger';ledger.mkdir()
    (ledger/'marks.jsonl').write_text(json.dumps({'id':'M-12345678','state':'OPEN',
        'text':'relay supply fluctuation','loc':'journal/2025-12-01.md:1'})+'\n')
    r=subprocess.run(['bash',str(TOOLS/'recall.sh'),'--lexical','relay supply'],env=env,capture_output=True,text=True)
    assert r.returncode==0,r.stdout+r.stderr
    assert 'UNREVIEWED EVIDENCE (lexical' in r.stdout
    assert '2026-01-02.md:1' in r.stdout and '2025-12-01.md:1' in r.stdout
    assert 'nothing on record among approved cards' in r.stdout


def test_deleted_shared_know_does_not_reinitialize_on_approval(tmp_path):
    root,env,unit=base(tmp_path)
    assert approve(env,unit).returncode==0
    (root/'config/khms.env').write_text('KHMS_SHARED=1\nKHMS_RUNTIME='+env['KHMS_RUNTIME']+'\n')
    for p in (root/'memory/know').iterdir():p.unlink()
    before=(root/'state/shared/next_id').read_text()
    r=approve(env,unit)
    assert r.returncode!=0 and 'unhealthy' in r.stderr
    assert (root/'state/shared/next_id').read_text()==before
    assert not list((root/'memory/know').iterdir())


def test_triage_marker_hash_reopens_changed_proposal(tmp_path):
    root,env,unit=base(tmp_path)
    tri=root/'memory/inbox/2026-01-01-triage.md'
    tri.write_text('# triage\n\n## REVIEW VERDICT\n')
    r=subprocess.run([sys.executable,str(TOOLS/'apply_triage.py'),str(tri),'--apply'],env=env,capture_output=True,text=True)
    assert r.returncode==0,r.stdout+r.stderr
    def census():
        return subprocess.run([sys.executable,str(TOOLS/'khms_review_state.py')],env=env,capture_output=True,text=True)
    assert str(tri) not in census().stdout
    tri.write_text(tri.read_text()+'\nnew proposal\n')
    assert str(tri) in census().stdout


def test_triage_reads_after_erratum_commit_not_before_lock(tmp_path):
    import importlib.util
    spec=importlib.util.spec_from_file_location('tri_fixture',Path(__file__).with_name('test_triage_boundaries.py'))
    fixture=importlib.util.module_from_spec(spec);spec.loader.exec_module(fixture)
    root,env,unit=base(tmp_path)
    other,files,tri=fixture.apply_case(tmp_path,root,[False,False])
    # Make complete cards, preserving the proposal's verbatim quotes.
    for p in files:
        p.write_text(p.read_text().replace('status: active','type: fact\ntags: [testing]\nscope: universal\ndate: 2026-01-01\nstatus: active'))
    env.update(KHMS_ROOT=str(other),KHMS_READ_ONLY='0')
    runtime=Path(env['KHMS_RUNTIME']);runtime.mkdir(exist_ok=True)
    with (runtime/'cards.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        proc=subprocess.Popen([sys.executable,str(TOOLS/'apply_triage.py'),str(tri),'--apply','--brain',str(other)],env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        try:
            time.sleep(.2)
            assert proc.poll() is None
            # The concurrent writer's update is committed before releasing the
            # cards lock. An implementation reading its plan before flock loses it.
            files[0].write_text(files[0].read_text()+'\nAn independently corrected wiring fact.\n')
        finally:
            fcntl.flock(lock,fcntl.LOCK_UN)
            out,err=proc.communicate(timeout=20)
        assert proc.returncode==0,out+err
    assert 'independently corrected wiring' in files[0].read_text()
    assert 'challenged' in files[0].read_text()


def test_other_approval_input_cannot_abandon_partial_receipt(tmp_path):
    root,env,unit=base(tmp_path)
    code='''import sys
sys.path.insert(0,sys.argv.pop(1))
import approve_inbox as A
real=A.L.atomic_text
def fail(path,text):
 real(path,text)
 if '/memory/know/' in str(path):raise RuntimeError('injected crash')
A.L.atomic_text=fail
A.main()
'''
    r=subprocess.run([sys.executable,'-c',code,str(TOOLS),str(unit)],env=env,capture_output=True,text=True)
    assert r.returncode!=0
    unit.write_text(unit.read_text().replace('relay opens','motor stops'))
    r=approve(env,unit)
    assert r.returncode!=0 and 'partial approval' in r.stderr
    assert len(list((root/'memory/know').glob('*.md')))==1


def test_waiting_view_builders_publish_cards_committed_before_unlock(tmp_path):
    root,env,unit=base(tmp_path)
    runtime=Path(env['KHMS_RUNTIME']);runtime.mkdir(exist_ok=True)
    procs=[]
    with (runtime/'cards.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        try:
            for _ in range(2):
                procs.append(subprocess.Popen([sys.executable,str(TOOLS/'build_views.py')],env=env,
                             stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True))
            time.sleep(.2)
            assert all(p.poll() is None for p in procs)
            cid='K-%05d' % 90001
            (root/'memory/know'/(cid+'.md')).write_text(CARD.replace('C1',cid))
            (root/'state/shared/views-dirty').write_text('pending committed card\n')
        finally:
            fcntl.flock(lock,fcntl.LOCK_UN)
            for p in procs:
                out,err=p.communicate(timeout=20)
                assert p.returncode==0,out+err
    assert cid in (root/'memory/views/by-type/fact.md').read_text()
    assert not (root/'state/shared/views-dirty').exists()


def test_failed_triage_report_does_not_mark_review_complete(tmp_path):
    root,env,unit=base(tmp_path)
    tri=root/'memory/inbox/2026-01-01-triage.md'
    tri.write_text('# triage\n\n## REVIEW VERDICT\n')
    code="""import sys,runpy
sys.path.insert(0,sys.argv[1])
import khms_lock as L
real=L.atomic_text
def fail_report(path,text):
    if str(path).endswith('.applied.md'):raise OSError('simulated report failure')
    return real(path,text)
L.atomic_text=fail_report
sys.argv=[sys.argv[1]+'/apply_triage.py',sys.argv[2],'--apply']
runpy.run_path(sys.argv[0],run_name='__main__')
"""
    p=subprocess.run([sys.executable,'-c',code,str(TOOLS),str(tri)],env=env,text=True,capture_output=True)
    assert p.returncode!=0 and 'simulated report failure' in p.stderr
    marker=root/'state/shared/reviewed/.reviewed-2026-01-01-triage'
    assert not marker.exists()


def test_erratum_waits_for_card_lock_then_applies_without_snapshot(tmp_path):
    root,env,unit=base(tmp_path)
    approved=approve(env,unit)
    assert approved.returncode==0,approved.stdout+approved.stderr
    card=next((root/'memory/know').glob('*.md'))
    runtime=Path(env['KHMS_RUNTIME'])
    cmd=[sys.executable,str(TOOLS/'khms_erratum.py'),'declare','--card',card.stem,
         '--was','The relay opens when its supply is removed.',
         '--evidence','Bench measurement: removing the supply kept the relay closed.',
         '--by','coordinator']
    with (runtime/'cards.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        proc=subprocess.Popen(cmd,env=env,text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        try:
            time.sleep(.2)
            assert proc.poll() is None
        finally:
            fcntl.flock(lock,fcntl.LOCK_UN)
            out,err=proc.communicate(timeout=20)
    assert proc.returncode==0,out+err
    tomb=next((root/'memory/tombstones/bodies').glob('TS-*.md')).stem
    p=subprocess.run([sys.executable,str(TOOLS/'khms_erratum.py'),'apply','--card',card.stem,
                      '--ts',tomb,'--now','The relay stays closed when its supply is removed.'],
                     env=env,text=True,capture_output=True)
    assert p.returncode==0,p.stdout+p.stderr
    assert 'The relay stays closed' in card.read_text()
    assert 'ERRATUM' in card.read_text()
    assert not list(root.rglob('*snapshot*'))
    assert not (root/'state/shared/views-dirty').exists()
