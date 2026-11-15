#!/usr/bin/env python3
"""Offline validation and known-source matching for a frozen scientific pilot."""
import argparse
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.night_evaluation_io import (EvaluationStopped, digest, read_json,
    verify_corpus, write_hashed)


def inside(root, relative):
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        raise EvaluationStopped('Payload path outside frozen corpus')
    return path


def verify_dataset(corpus):
    from plagiarism.matching import _tokens
    corpus = Path(corpus)
    identity = verify_corpus(corpus)
    manifest = read_json(corpus / 'manifest.json')
    sources = read_json(corpus / 'sources.json')['sources']
    by_source = {s['id']: s for s in sources}
    cases = {c['id']: c for c in manifest['cases']}
    order = manifest['internet_case_ids']
    if (len(cases) != len(manifest['cases']) or len(order) != 3 or len(set(order)) != 3
            or set(order) - cases.keys() or len(by_source) != 2
            or len({s['family_id'] for s in sources}) != 2
            or [cases[c]['expected_insertion_words'] for c in order] != [0, 9, 9]
            or manifest['physical_cap'] != 18 or manifest['per_case_primary_cap'] != 6
            or manifest['candidate_count'] != 1 or manifest['candidate_received'] is not False
            or read_json(corpus / 'run-order.json')['case_ids'] != order):
        raise EvaluationStopped('Unexpected pilot identity, order or caps')
    ids = [c['request_id'] for c in cases.values()]
    if len(set(ids)) != len(ids) or any(not re.fullmatch('[a-f0-9]{32}', x) for x in ids):
        raise EvaluationStopped('Invalid or repeated frozen request UUID')
    for s in sources:
        if digest(inside(corpus, s['source_snapshot_path']).read_bytes()) != s['source_text_sha256']:
            raise EvaluationStopped('Source snapshot changed')
    for c in cases.values():
        text = inside(corpus, c['input_file']).read_text()
        n = c['expected_insertion_words']
        if digest(text) != c['text_sha256'] or len(_tokens(text)) != c['total_words']:
            raise EvaluationStopped('Input identity changed')
        if n not in {0, 6, 9, 12, 20} or len(c['expected_evidence']) != int(n > 0):
            raise EvaluationStopped('Unexpected insertion size or Gold count')
        for e in c['expected_evidence']:
            source = by_source[e['source_id']]
            source_text = inside(corpus, source['source_snapshot_path']).read_text()
            a, b = e['target_span']; x, y = e['source_span']
            if not (0 <= a < b <= len(text) and 0 <= x < y <= len(source_text)):
                raise EvaluationStopped('Out-of-range exact bounds')
            quote = text[a:b]
            if (quote != source_text[x:y] or quote != e['quote'] or len(_tokens(quote)) != n
                    or e['source_urls'] != source['source_urls']):
                raise EvaluationStopped('Unconfirmed exact excerpt')
            for value, start, end, key in [(text, a, b, 'target'), (source_text, x, y, 'source')]:
                utf8 = [len(value[:start].encode()), len(value[:end].encode())]
                tokens = _tokens(value)
                indices = [i for i, t in enumerate(tokens) if t.start >= start and t.end <= end]
                if (len(indices) != n or e[key + '_utf8_span'] != utf8
                        or e[key + '_token_span'] != [indices[0], indices[-1] + 1]):
                    raise EvaluationStopped('UTF8/token boundaries disagree')
    if set(manifest['conditional_case_ids']) != set(cases):
        raise EvaluationStopped('Conditional denominator changed')
    return identity, manifest, sources


def deny_network():
    denied = []
    def guard(event, args):
        if event.startswith(('socket.connect', 'socket.getaddrinfo', 'urllib.', 'http.client.')):
            denied.append(event)
            raise EvaluationStopped('Conditional evaluation is strictly offline')
    sys.addaudithook(guard)
    return denied


def conditional(corpus, out):
    denied = deny_network()
    from plagiarism.matching import analyze
    from scripts.night_evaluation_score import score_case
    identity, manifest, sources = verify_dataset(corpus)
    known = [{'id': s['id'], 'url': s['source_urls'][0],
              'text': inside(corpus, s['source_snapshot_path']).read_text()} for s in sources]
    rows = []
    by_id = {c['id']: c for c in manifest['cases']}
    for cid in manifest['conditional_case_ids']:
        c = by_id[cid]
        text = inside(corpus, c['input_file']).read_text()
        report = {**analyze(text, known), 'text': text, 'sources': known}
        write_hashed(out / cid / 'report.json', report)
        # Supply no discovery claim; reuse the exact-pair validator only.
        rows.append(score_case(c, report, conditional=True))
    if denied:
        raise EvaluationStopped('Conditional matcher attempted network')
    receipt = {'track': 'known_source_conditional_only', 'dataset': identity,
               'cases': len(rows), 'rows': rows, 'network_attempts': 0,
               'internet_discovery_claim': False}
    write_hashed(out / 'complete.json', receipt)
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['verify', 'conditional'])
    parser.add_argument('--corpus', required=True, type=Path)
    parser.add_argument('--out', type=Path)
    args = parser.parse_args()
    if args.command == 'verify':
        identity, manifest, sources = verify_dataset(args.corpus)
        print({'verified': identity, 'internet_cases': len(manifest['internet_case_ids']),
               'conditional_cases': len(manifest['conditional_case_ids'])})
    else:
        if args.out is None:
            parser.error('conditional requires --out')
        receipt = conditional(args.corpus, args.out)
        print({k: v for k, v in receipt.items() if k != 'rows'})


if __name__ == '__main__':
    main()
