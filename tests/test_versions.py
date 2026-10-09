"""Recovery requires a readable retained version and byte-identical readback."""
import hashlib
from pathlib import Path
import sys
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'tools'))
from khms_versions import Versions, VersionError


def client():
    return Versions('https://example.invalid/remote.php/dav','agent','Shared/base','/private/netrc')


@pytest.mark.parametrize('path',['/file','../file','a/../b','a//b'])
def test_relative_file_cannot_escape_base(path):
    with pytest.raises(VersionError):client().file_id(path)


def test_version_id_cannot_escape_account():
    with pytest.raises(VersionError):client().read({'fileid':'../other','version':'123'})


def test_restore_uses_child_of_restore_folder_and_checks_bytes(monkeypatch):
    v=client();calls=[]
    monkeypatch.setattr(v,'file_id',lambda _: '9')
    monkeypatch.setattr(v,'versions',lambda _: [{'fileid':'9','version':'100','uri':'/versions/agent/versions/9/100'}])
    def request(method,path,**kw):
        calls.append((method,path,kw))
        return b'original' if method=='GET' else b''
    monkeypatch.setattr(v,'request',request)
    proof=v.restore('card.md','100')
    assert proof['sha256']==hashlib.sha256(b'original').hexdigest()
    assert calls[1]==('MOVE','/versions/agent/versions/9/100',{'destination':'/versions/agent/restore/100'})
    assert calls[2][1]=='/files/agent/Shared/base/card.md'


def test_restore_does_not_claim_success_after_wrong_readback(monkeypatch):
    v=client();monkeypatch.setattr(v,'file_id',lambda _: '9')
    monkeypatch.setattr(v,'versions',lambda _: [{'fileid':'9','version':'100','uri':'/versions/agent/versions/9/100'}])
    monkeypatch.setattr(v,'request',lambda method,path,**kw:b'wrong' if '/files/' in path else b'original')
    with pytest.raises(VersionError,match='byte verification'):v.restore('card.md','100')


def test_missing_history_never_reads_current_card(monkeypatch):
    v=client();monkeypatch.setattr(v,'file_id',lambda _: '9')
    monkeypatch.setattr(v,'versions',lambda _: [])
    monkeypatch.setattr(v,'request',lambda *a,**kw:pytest.fail('live fallback'))
    with pytest.raises(VersionError,match='unverifiable'):v.before('card.md',200)


def test_unreadable_latest_history_does_not_substitute_an_older_version(monkeypatch):
    v=client();monkeypatch.setattr(v,'file_id',lambda _: '9')
    monkeypatch.setattr(v,'versions',lambda _: [{'fileid':'9','version':'100'},{'fileid':'9','version':'150'}])
    def read(ver):
        assert ver['version']=='150'
        raise VersionError('HTTP 404')
    monkeypatch.setattr(v,'read',read)
    with pytest.raises(VersionError,match='404'):v.before('card.md',200)
