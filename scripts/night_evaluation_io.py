"""Append-only evidence and a bounded, first-response-only search cache.

This is operator tooling; it never changes the application's search algorithm.
"""
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit


class EvaluationStopped(RuntimeError):
    """An uncertain delivery or violated freeze must not trigger a new search."""


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    return sha256(value.encode('utf-8') if isinstance(value, str) else value).hexdigest()


def read_json(path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('Duplicate JSON key')
            result[key] = value
        return result
    data = Path(path).read_bytes()
    if len(data) > 32 * 1024 * 1024:
        raise ValueError('Evidence JSON exceeds 32 MiB')
    return json.loads(data, object_pairs_hook=unique)


def write_bytes(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'wb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    return digest(data)


def write_json(path, value):
    return write_bytes(path, (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode())


def write_hashed(path, value):
    checksum = write_json(path, value)
    write_bytes(str(path) + '.sha256', (checksum + '\n').encode())
    return checksum


def read_hashed(path):
    path = Path(path)
    if digest(path.read_bytes()) != Path(str(path) + '.sha256').read_text().strip():
        raise EvaluationStopped('Changed evidence: ' + path.name)
    return read_json(path)


def verify_corpus(corpus):
    """Verify every frozen file, without running a supplied verifier script."""
    corpus = Path(corpus).resolve()
    frozen = read_json(corpus / 'freeze.json')
    rows = frozen['payload_files']
    if rows != sorted(rows, key=lambda row: row['path']):
        raise ValueError('Unordered corpus inventory')
    for row in rows:
        path = corpus / row['path']
        if (path.is_symlink() or not path.resolve().is_relative_to(corpus)
                or path.stat().st_size != row['bytes'] or digest(path.read_bytes()) != row['sha256']):
            raise ValueError('Corpus payload changed: ' + row['path'])
    canonical = ''.join(f"{r['sha256']}  {r['path']}\n" for r in rows)
    if digest(canonical) != frozen['payload_sha256']:
        raise ValueError('Corpus root hash mismatch')
    names = set()
    for line in (corpus / 'SHA256SUMS').read_text().splitlines():
        checksum, name = line.split('  ', 1)
        if name in names or digest((corpus / name).read_bytes()) != checksum:
            raise ValueError('Corpus checksum mismatch')
        names.add(name)
    expected = {r['path'] for r in rows} | {'freeze.json'}
    actual = {p.relative_to(corpus).as_posix() for p in corpus.rglob('*') if p.is_file()}
    if names != expected or actual != expected | {'SHA256SUMS'}:
        raise ValueError('Corpus inventory mismatch')
    return {'payload_sha256': frozen['payload_sha256'], 'files_verified': len(names),
            'freeze_sha256': digest((corpus / 'freeze.json').read_bytes())}


def redact_response(data, secret):
    """Keep original response bytes unless credentials require redaction."""
    raw = data.decode('utf-8', errors='replace')
    redacted = raw.replace(secret, '[REDACTED]') if secret else raw
    redacted = re.sub(r'([?&](?:api_key|access_token|token)=)[^&\s"<>]+', r'\1[REDACTED]', redacted)
    changed = redacted != raw
    sensitive = {'api_key', 'authorization', 'access_token', 'password', 'secret'}

    def clean(value):
        nonlocal changed
        if isinstance(value, dict):
            result = {}
            for key, child in value.items():
                if key.casefold() in sensitive:
                    result[key] = '[REDACTED]'
                    changed = True
                else:
                    result[key] = clean(child)
            return result
        if isinstance(value, list):
            return [clean(child) for child in value]
        return value
    try:
        value = clean(json.loads(redacted))
        stored = (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode() if changed else data
    except (ValueError, UnicodeError):
        stored = redacted.encode() if changed else data
    return stored, changed


class RecordingOpener:
    """Capture the actual search response before native adapter normalization."""
    def __init__(self, original, secret):
        self.original, self.secret, self.destination = original, secret, None

    def open(self, request, *args, **kwargs):
        response = self.original.open(request, *args, **kwargs)
        if urlsplit(request.full_url).path != '/search.json':
            return response
        destination, secret = self.destination, self.secret
        if destination is None:
            response.close()
            raise EvaluationStopped('Unjournalled search transport')

        class Captured:
            def __enter__(self):
                response.__enter__()
                return self

            def __exit__(self, *exc):
                return response.__exit__(*exc)

            def read(self, maximum):
                data = response.read(maximum)
                stored, changed = redact_response(data, secret)
                checksum = write_bytes(destination / 'response.raw', stored)
                write_json(destination / 'response-metadata.json', {
                    'received_at': now(), 'raw_response_sha256': digest(data),
                    'stored_response_sha256': checksum, 'redacted': changed,
                    'bytes_read': len(data), 'read_limit': maximum,
                    'read_limit_reached': len(data) == maximum})
                return data
        return Captured()


class CachedSearch:
    provider = 'serpapi-yandex'

    def __init__(self, client, directory, allowed_queries, physical_cap, completed_queries=()):
        self.client, self.directory = client, Path(directory)
        self.allowed = frozenset(allowed_queries)
        self.cap = physical_cap
        # Count attempts, including unknown deliveries, from the durable journal.
        # Completed case receipts are the authority for prior logical/cache use.
        intents = {p.parent.name: read_hashed(p)
                   for p in (self.directory / 'physical').glob('*/intent.json')}
        self.physical, self.last_usage = len(intents), None
        self.logical = deepcopy(list(completed_queries))
        seen = set()
        for event in self.logical:
            physical_id = event['physical_request_id']
            intent = intents.get(physical_id)
            if (event['request_id'] in seen or intent is None
                    or intent['query'] != event['query']
                    or intent['request_id'] != physical_id
                    or not isinstance(event['cache_hit'], bool)):
                raise EvaluationStopped('Inconsistent completed query receipt')
            result = read_hashed(self.directory / 'physical' / physical_id / 'result.json')
            if result['state'] != 'complete' or result['query'] != event['query']:
                raise EvaluationStopped('Completed receipt references an unknown outcome')
            seen.add(event['request_id'])
        self.hits = sum(event['cache_hit'] for event in self.logical)
        self.opener = None
        if hasattr(client, 'opener'):
            self.opener = RecordingOpener(client.opener, client.key)
            client.opener = self.opener

    def search(self, query, request_id):
        if query not in self.allowed:
            raise EvaluationStopped('Unplanned query rejected before network')
        cache = self.directory / 'search-cache' / (digest(self.provider + '\0' + query) + '.json')
        event = {'request_id': request_id, 'query': query, 'at': now()}
        if cache.exists():
            completion = Path(str(cache) + '.complete')
            saved = read_hashed(completion if completion.exists() else cache)
            if saved['query'] != query or saved['state'] != 'complete':
                raise EvaluationStopped('First query outcome unknown; automatic retry forbidden')
            self.hits += 1
            self.last_usage = {'credits': 0}
            self.logical.append({**event, 'cache_hit': True, 'physical_request_id': saved['request_id']})
            return deepcopy(saved['results'])
        intents = list((self.directory / 'physical').glob('*/intent.json'))
        if len(intents) >= self.cap:
            raise EvaluationStopped('Frozen physical query cap reached before transport')
        # Account GET is not a search. Native guard confirms an active $0 plan.
        quota = self.client.confirm_free(1)
        physical = self.directory / 'physical' / request_id
        write_hashed(physical / 'intent.json', {**event, 'state': 'delivery_may_have_started',
                     'quota': quota, 'automatic_retry': False})
        write_hashed(cache, {**event, 'state': 'unknown', 'physical_path': str(physical)})
        if self.opener:
            self.opener.destination = physical
        self.physical += 1
        try:
            results = self.client.search(query, request_id)
        except Exception as error:
            write_hashed(physical / 'failure.json', {'state': 'failed_or_unknown',
                         'code': getattr(error, 'code', type(error).__name__), 'automatic_retry': False})
            raise EvaluationStopped('First search failed/unknown; no retry or substitute request') from None
        # The initial unknown entry is immutable. A separate completion is authoritative.
        completion = {**event, 'state': 'complete', 'results': results,
                      'usage': deepcopy(self.client.last_usage), 'physical_path': str(physical)}
        write_hashed(str(cache) + '.complete', completion)
        write_hashed(physical / 'result.json', completion)
        self.last_usage = deepcopy(self.client.last_usage)
        self.logical.append({**event, 'cache_hit': False, 'physical_request_id': request_id})
        return deepcopy(results)
