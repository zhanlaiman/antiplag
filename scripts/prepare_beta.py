#!/usr/bin/env python3
"""Inspect the local beta and optionally make a private, verified SQLite backup.

Only an explicit list of application databases/configs is accessed. Evaluation
collections are never traversed. No search, model or external request is sent.
"""
import argparse
from contextlib import closing
from datetime import datetime,timezone
from hashlib import sha256
import json
from pathlib import Path
import shutil
import sqlite3
import sys
from urllib.parse import quote
from urllib.request import build_opener

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
DATABASES=('data/istok.sqlite3','data/search-budget.sqlite3','data/private/mediawiki.sqlite3')
CONFIGS=('.runtime/search-api.json','.runtime/pdf-extraction.json','.runtime/local-inference-disabled')


def readonly(path):
    return sqlite3.connect('file:'+quote(str(path.resolve()),safe='/')+'?mode=ro',uri=True,timeout=15)


def fingerprint(path):
    value=sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''):value.update(chunk)
    return value.hexdigest()


def backup(root,destination):
    destination.mkdir(parents=True,exist_ok=False,mode=0o700)
    files=[]
    try:
        for relative in DATABASES:
            source=root/relative
            if not source.exists():continue
            if source.is_symlink():raise ValueError('Database symlinks are not backed up')
            target=destination/relative;target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
            with closing(readonly(source)) as original,closing(sqlite3.connect(target)) as snapshot:
                original.backup(snapshot)
                if snapshot.execute('PRAGMA quick_check').fetchall()!=[('ok',)]:raise ValueError('Backup integrity check failed')
            target.chmod(0o600)
            files.append({'path':relative,'sha256':fingerprint(target),'bytes':target.stat().st_size,'kind':'sqlite'})
        for relative in CONFIGS:
            source=root/relative
            if not source.exists():continue
            if source.is_symlink():raise ValueError('Configuration symlinks are not backed up')
            target=destination/relative;target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
            shutil.copyfile(source,target);target.chmod(0o600)
            files.append({'path':relative,'sha256':fingerprint(target),'bytes':target.stat().st_size,'kind':'config'})
        manifest={'created_at':datetime.now(timezone.utc).isoformat(),'state':'complete','files':files,
                  'external_requests':0,'cost_kzt':0,'evaluation_collections_read':False}
        path=destination/'manifest.json';path.write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n');path.chmod(0o600)
        return manifest
    except Exception:
        # Keep partial evidence; a missing complete manifest forbids restoration.
        (destination/'INCOMPLETE').touch(mode=0o600)
        raise


def inspect(root):
    from plagiarism.auth_provider import NoRedirect
    result={'python_supported':sys.version_info>=(3,11),'one_click_launcher_present':(root/'Запустить Исток.command').is_file(),
            'local_models_disabled':(root/'.runtime/local-inference-disabled').is_file(),
            'external_requests':0,'search_queries':0,'cost_kzt':0,'databases':{}}
    for relative in DATABASES:
        path=root/relative
        if not path.is_file():result['databases'][relative]='missing';continue
        try:
            with closing(readonly(path)) as db:
                rows=db.execute('PRAGMA quick_check').fetchall()
                result['databases'][relative]='ok' if rows==[('ok',)] else 'invalid'
                if relative=='data/search-budget.sqlite3':
                    month=datetime.now(timezone.utc).strftime('%Y-%m')
                    used=db.execute('SELECT count(*) FROM calls WHERE month=?',(month,)).fetchone()[0]
                    result['search_monthly_attempts']=used;result['local_protective_headroom']=max(0,900-used)
        except sqlite3.Error:result['databases'][relative]='unreadable'
    try:
        with build_opener(NoRedirect()).open('http://127.0.0.1:8766/api/status',timeout=3) as response:status=json.load(response)
        result.update(site_running=True,search_configured=status['web']['configured'],
                      models_available=status['model']['available'],max_queries=status['web']['max_queries'],
                      max_page_attempts=status['web']['max_urls'])
    except (OSError,ValueError,KeyError):result['site_running']=False
    result['ready_for_local_beta']=(result['python_supported'] and result['one_click_launcher_present']
        and result['local_models_disabled'] and all(v=='ok' for v in result['databases'].values())
        and result.get('site_running') and result.get('search_configured') and not result.get('models_available'))
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--backup',action='store_true')
    args=parser.parse_args();result=inspect(ROOT)
    if args.backup:
        destination=ROOT/'.runtime/backups'/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        needed=sum((ROOT/p).stat().st_size for p in DATABASES if (ROOT/p).is_file())
        if shutil.disk_usage(ROOT).free<needed+64*1024*1024:raise SystemExit('Not enough room for a verified backup')
        manifest=backup(ROOT,destination);result['backup']={'path':str(destination),'state':manifest['state'],'files':len(manifest['files'])}
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0 if result['ready_for_local_beta'] else 1


if __name__=='__main__':raise SystemExit(main())
