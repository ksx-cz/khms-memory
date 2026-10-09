#!/usr/bin/env python3
"""Nextcloud per-file version evidence. No local snapshots or pre-write copies.

The deployment supplies DAV URL, account, base path and a curl netrc file. A read
returns bytes plus the server version URI and SHA256. Restore is explicit; callers
must reconcile the related ledger/counter/review marker before resuming writers.
"""
import argparse
import datetime as dt
import hashlib
import json
import os
import subprocess
import sys
from urllib.parse import quote, urlsplit
import xml.etree.ElementTree as ET

import khms_paths as P

DAV = '{DAV:}'
OC = '{http://owncloud.org/ns}'
PROP = b'''<?xml version="1.0"?><d:propfind xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns"><d:prop><oc:fileid/><d:getlastmodified/><d:getcontentlength/><d:resourcetype/></d:prop></d:propfind>'''


class VersionError(RuntimeError):
    pass


class Versions:
    def __init__(self, url, account, base, netrc):
        parsed=urlsplit(url)
        if parsed.scheme != 'https' or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise VersionError('DAV URL must be https without credentials, query or fragment')
        self.url=url.rstrip('/')
        self.account=quote(account,safe='')
        self.base=base.strip('/')
        if any(part in ('', '.', '..') for part in self.base.split('/')):
            raise VersionError('invalid DAV base path')
        self.netrc=netrc
        if not account or not base or not netrc:
            raise VersionError('DAV account, base path and netrc must be configured')

    @classmethod
    def configured(cls):
        return cls(*(P.get(k) for k in ('KHMS_DAV_URL','KHMS_DAV_USER','KHMS_DAV_BASE','KHMS_DAV_NETRC')))

    def request(self, method, path, depth=None, data=None, destination=None):
        # No verbose mode, credentials in argv, redirect following, or shell.
        argv=['curl','--silent','--show-error','--max-time','90','--netrc-file',self.netrc,
              '--request',method,'--write-out','\n%{http_code}',self.url+path]
        if depth is not None: argv+=['--header',f'Depth: {depth}']
        if destination: argv+=['--header','Destination: '+self.url+destination,'--header','Overwrite: T']
        if data is not None: argv+=['--header','Content-Type: application/xml','--data-binary','@-']
        r=subprocess.run(argv,input=data,capture_output=True)
        if r.returncode:
            raise VersionError(f'DAV transport failed rc={r.returncode}')
        body,_,status=r.stdout.rpartition(b'\n')
        if status not in (b'200',b'201',b'204',b'207'):
            raise VersionError(f'DAV {method} failed HTTP {status.decode(errors="replace")}')
        return body

    def file_id(self, relative):
        if relative.startswith('/') or any(p in ('..','') for p in relative.split('/')):
            raise VersionError('file must be a relative path without traversal')
        path='/files/'+self.account+'/'+quote(self.base+'/'+relative,safe='/')
        xml=ET.fromstring(self.request('PROPFIND',path,0,PROP))
        ids=[]
        for propstat in xml.findall('.//'+DAV+'propstat'):
            if ' 200 ' not in propstat.findtext(DAV+'status',''): continue
            ids += [e.text for e in propstat.findall('.//'+OC+'fileid')]
        if len(ids)!=1 or not ids[0].isdigit():
            raise VersionError('server did not prove one numeric file ID')
        return ids[0]

    def versions(self, fileid):
        if not str(fileid).isdigit(): raise VersionError('invalid file ID')
        path='/versions/'+self.account+'/versions/'+str(fileid)
        xml=ET.fromstring(self.request('PROPFIND',path,1,PROP))
        out=[]
        for response in xml.findall(DAV+'response'):
            href=response.findtext(DAV+'href','').rstrip('/')
            version=href.rsplit('/',1)[-1]
            # A collection has the numeric file ID too; discriminate by type.
            propstats=[p for p in response.findall(DAV+'propstat') if ' 200 ' in p.findtext(DAV+'status','')]
            if not propstats: raise VersionError('version listing contains failed properties')
            if any(p.find('.//'+DAV+'collection') is not None for p in propstats):continue
            if not version.isdigit(): raise VersionError('unknown server version identifier')
            out.append({'fileid':str(fileid),'version':version,'uri':path+'/'+version})
        return sorted(out,key=lambda v:int(v['version']))

    def read(self, version):
        if not all(str(version.get(k, '')).isdigit() for k in ('fileid', 'version')):
            raise VersionError('invalid server version reference')
        path='/versions/'+self.account+'/versions/'+version['fileid']+'/'+version['version']
        body=self.request('GET',path)
        return body,dict(version,sha256=hashlib.sha256(body).hexdigest())

    def before(self, relative, at):
        choices=[v for v in self.versions(self.file_id(relative)) if int(v['version'])<float(at)]
        if not choices: raise VersionError('no retained server version before requested time; unverifiable')
        return self.read(choices[-1])

    def restore(self, relative, version):
        fileid=self.file_id(relative)
        candidates=self.versions(fileid)
        chosen=next((v for v in candidates if v['version']==str(version)),None)
        if not chosen:raise VersionError('requested version is not in the server listing')
        body,proof=self.read(chosen)
        self.request('MOVE',chosen['uri'],destination='/versions/'+self.account+'/restore/'+chosen['version'])
        current=self.request('GET','/files/'+self.account+'/'+quote(self.base+'/'+relative,safe='/'))
        if current != body:raise VersionError('restored file failed byte verification')
        return proof


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('file');p.add_argument('--before',help='ISO timestamp with timezone')
    p.add_argument('--restore',help='explicit server version identifier')
    a=p.parse_args()
    try:
        client=Versions.configured()
        if a.restore:
            P.require_writable('restore a server file version')
            print(json.dumps(client.restore(a.file,a.restore)))
        elif a.before:
            when=dt.datetime.fromisoformat(a.before)
            if when.tzinfo is None:raise VersionError('timestamp needs a timezone')
            body,proof=client.before(a.file,when.timestamp())
            print(json.dumps(proof),file=sys.stderr)
            sys.stdout.buffer.write(body)
        else: print(json.dumps(client.versions(client.file_id(a.file)),indent=2))
    except (OSError,ValueError,ET.ParseError,VersionError,P.KhmsPathError) as exc:
        print('versions: '+str(exc),file=sys.stderr);return 3
    return 0


if __name__=='__main__':sys.exit(main())
