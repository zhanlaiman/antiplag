#!/usr/bin/env python3
"""Bounded first-use sample through an isolated normal HTTP queue."""
import argparse
from collections import defaultdict
from hashlib import sha256
from http.cookiejar import LWPCookieJar
import json
import os
from pathlib import Path
from threading import Thread
import time
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, Request, build_opener

from cloud.web_beta import create_server
from plagiarism.search_credentials import key_for

ROOT = Path(__file__).resolve().parents[1]
MATERIALS = ROOT / 'data/private/web-evaluation/materials/final-sealed'
OUT = ROOT / 'data/private/web-evaluation/final-70-firstuse'
ORIGIN = 'http://127.0.0.1:19070'
FROZEN = '5996c3adaeab60d8e0a8df17f4962e2afdc502aa'


def selected():
    cases = json.loads((MATERIALS / 'key.json').read_text())['cases']
    grouped = defaultdict(list)
    for case in cases:
        grouped[case['scenario']].append(case)
    chosen = [case for name in sorted(grouped) for case in grouped[name][:2]]
    chosen += grouped['short_insertion'][2:6]
    assert len(chosen) == 20 and len({c['id'] for c in chosen}) == 20
    return chosen


def request(opener, path, payload=None):
    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode()
    headers = {'Origin': ORIGIN}
    if body is not None:
        headers['Content-Type'] = 'application/json'
    try:
        with opener.open(Request(ORIGIN + path, body, headers), timeout=30) as result:
            return result.status, json.load(result)
    except HTTPError as error:
        try:
            return error.code, json.load(error)
        finally:
            error.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--live', action='store_true')
    args = parser.parse_args()
    chosen = selected()
    OUT.mkdir(parents=True, exist_ok=True)
    manifest_path = OUT / 'manifest.json'
    manifest = {'frozen_application_commit': FROZEN, 'sample_rule': 'first two ID-sorted per scenario plus next four short insertions',
                'maximum_primary_queries': 120, 'cases': [{'id': c['id'], 'scenario': c['scenario'],
                    'request_id': sha256(('final70-firstuse:' + c['id']).encode()).hexdigest()[:32],
                    'text_sha256': c['text_sha256']} for c in chosen]}
    if manifest_path.exists():
        assert json.loads(manifest_path.read_text()) == manifest
    else:
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
    if not args.live:
        print('Frozen 20 cases, maximum 120 free primary search requests; no data sent.');return
    if not key_for('tavily'):
        raise SystemExit('No configured search key')
    os.environ['ISTOK_TAVILY_API_KEY'] = key_for('tavily')
    os.environ['ISTOK_SEARCH_PROVIDER'] = 'tavily'
    credential = json.loads((ROOT / '.runtime/owner-credentials.json').read_text())
    os.environ.update(ISTOK_OWNER_USERNAME=credential['username'],ISTOK_OWNER_SALT=credential['salt'],
                      ISTOK_OWNER_DIGEST=credential['digest'])
    root = OUT / 'isolated-site'
    service = create_server(root, ORIGIN, host='127.0.0.1', port=19070, allow_http=True)
    thread = Thread(target=service.serve_forever, daemon=True);thread.start()
    cookies = LWPCookieJar(str(OUT / 'cookies.txt'))
    if cookies.filename and Path(cookies.filename).exists():
        cookies.load(ignore_discard=True, ignore_expires=True)
    opener = build_opener(HTTPCookieProcessor(cookies))
    try:
        status, _ = request(opener, '/api/session')
        assert status == 200
        status, result = request(opener, '/api/owner/login', {'username': credential['username'],
                                                               'password': credential['password']})
        assert status == 200, result
        cookies.save(ignore_discard=True, ignore_expires=True)
        for case, entry in zip(chosen, manifest['cases']):
            identifier = entry['request_id']
            file = OUT / (case['id'] + '.json')
            if file.exists():
                print(case['id'], 'saved', flush=True);continue
            status, job = request(opener, '/api/jobs/' + identifier)
            if status == 404:
                text = (MATERIALS / case['input_path']).read_text()
                assert sha256(text.encode()).hexdigest() == entry['text_sha256']
                (OUT / (case['id'] + '.intent')).write_text(identifier)
                status, job = request(opener, '/api/web-check-jobs',
                    {'request_id': identifier, 'text': text, 'internet_consent': True})
            if status != 200:
                file.write_text(json.dumps({'http_status': status, 'response': job}, ensure_ascii=False))
                print(case['id'], 'not accepted', status, flush=True);break
            for _ in range(180):
                status, job = request(opener, '/api/jobs/' + identifier)
                if status != 200 or job['job']['status'] in {'complete', 'failed', 'cancelled'}:
                    break
                time.sleep(2)
            file.write_text(json.dumps(job, ensure_ascii=False))
            print(case['id'], job.get('job', {}).get('status', status), flush=True)
            if status != 200 or job['job']['status'] != 'complete':
                break
    finally:
        cookies.save(ignore_discard=True, ignore_expires=True)
        service.shutdown();service.server_close();service.beta.tenants.close();thread.join()


if __name__ == '__main__':
    main()
