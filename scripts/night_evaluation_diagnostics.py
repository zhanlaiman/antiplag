#!/usr/bin/env python3
"""Post-hoc, offline query purity diagnostics; never changes primary scoring."""
import argparse
from collections import Counter
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from plagiarism.matching import _tokens
from scripts.night_evaluation_io import digest, read_hashed, read_json, verify_corpus, write_hashed


def query_profile(text, spans, queries):
    target = _tokens(text)
    values = [token.value for token in target]
    pure_words, touched_words = set(), set()
    pure = mixed = unrelated = nonliteral = empty = 0
    if not spans:
        return {'applicable': False, 'pure_queries': 0, 'mixed_queries': 0,
                'pure_source_word_coverage': 0, 'touched_source_word_coverage': 0}
    if any(not 0 <= a < b <= len(text) for a, b in spans):
        raise ValueError('Invalid diagnostic target span')
    for query in queries:
        words = [token.value for token in _tokens(query)]
        if not words:
            empty += 1
            continue
        occurrences, within, touches = 0, set(), set()
        for index in range(len(values) - len(words) + 1):
            if values[index:index + len(words)] != words:
                continue
            occurrences += 1
            indices = {j for j in range(index, index + len(words))
                       if any(a <= target[j].start and target[j].end <= b for a, b in spans)}
            touches.update(indices)
            if len(indices) == len(words):
                within.update(indices)
        touched_words.update(touches)
        pure_words.update(within)
        if within:
            pure += 1
        elif touches:
            mixed += 1
        elif occurrences:
            unrelated += 1
        else:
            nonliteral += 1
    return {'applicable': True, 'pure_queries': pure, 'mixed_queries': mixed,
            'unrelated_queries': unrelated, 'nonliteral_queries': nonliteral, 'empty_queries': empty,
            'pure_source_word_coverage': len(pure_words), 'touched_source_word_coverage': len(touched_words)}


def diagnose(out):
    config = read_hashed(out / 'config.json')
    corpus = Path(config['corpus'])
    if verify_corpus(corpus) != config['corpus_identity']:
        raise ValueError('Corpus changed')
    cases = {case['id']: case for case in read_json(corpus / 'gold.json')['cases']}
    profiles = []
    for cid in config['selection']['case_ids']:
        directory = out / 'results' / cid
        if not (directory / 'report.json').exists():
            continue
        report = read_hashed(directory / 'report.json')
        queries = [e['query'] for e in read_hashed(directory / 'queries.json')]
        case = cases[cid]
        spans = [e['target_span'] for e in case['expected_evidence']]
        profiles.append({'id': cid, 'insertion_words': case['expected_insertion_words'],
                         **query_profile(report['text'], spans, queries)})
    physical = []
    for path in sorted((out / 'physical').glob('*/intent.json')):
        intent = read_hashed(path)
        raw_path = path.with_name('response.raw')
        metadata = read_json(path.with_name('response-metadata.json'))
        if digest(raw_path.read_bytes()) != metadata['stored_response_sha256']:
            raise ValueError('Changed raw response')
        response = read_json(raw_path)
        parameters = response.get('search_parameters', {})
        if parameters.get('engine') != 'yandex' or parameters.get('text') != intent['query']:
            raise ValueError('Server echo differs from the actual frozen query')
        physical.append({'request_id': intent['request_id'], 'query_echo_identical': True,
                         'status': response.get('search_metadata', {}).get('status'),
                         'organic_results': len(response.get('organic_results', [])),
                         'provider_error': response.get('error'),
                         'response_sha256': metadata['stored_response_sha256']})
    groups = []
    for length in [6, 9, 12, 20]:
        rows = [p for p in profiles if p['insertion_words'] == length]
        groups.append({'insertion_words': length, 'executed': len(rows),
                       'cases_with_pure_query': sum(p['pure_queries'] > 0 for p in rows),
                       'cases_with_mixed_query': sum(p['mixed_queries'] > 0 for p in rows),
                       'pure_source_word_coverage': sum(p['pure_source_word_coverage'] for p in rows),
                       'touched_source_word_coverage': sum(p['touched_source_word_coverage'] for p in rows)})
    return {'track': 'post_hoc_offline_diagnostics', 'new_search_calls': 0,
            'code_sha256': digest(Path(__file__).read_bytes()), 'groups': groups, 'cases': profiles,
            'physical_responses_verified': len(physical), 'physical': physical,
            'organic_result_count_distribution': dict(Counter(p['organic_results'] for p in physical))}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    result = diagnose(args.out)
    write_hashed(args.output, result)
    print({'groups': result['groups'], 'verified_raw_responses': result['physical_responses_verified']})
