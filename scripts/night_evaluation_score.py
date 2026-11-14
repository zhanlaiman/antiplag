#!/usr/bin/env python3
"""Offline evidence scoring, preserving unexecuted cases in the denominator."""
import argparse
from collections import Counter
from copy import deepcopy
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from plagiarism.matching import _tokens
from plagiarism.web_evaluation import _report, _url, score_manifest
from scripts.night_evaluation_io import digest, now, read_hashed, read_json, verify_corpus, write_hashed


def word_coverage(text, spans, expected):
    return sum(any(a <= token.start and token.end <= b for a, b in expected)
               and any(a <= token.start and token.end <= b for a, b in spans)
               for token in _tokens(text))


def query_coverage(text, queries, expected):
    """Count actual query words at literal input occurrences, not title anchors."""
    target = _tokens(text)
    values = [token.value for token in target]
    spans = []
    for query in queries:
        words = [token.value for token in _tokens(query)]
        if not words:
            continue
        for index in range(len(values) - len(words) + 1):
            if values[index:index + len(words)] == words:
                spans.append([target[index].start, target[index + len(words) - 1].end])
    return word_coverage(text, spans, expected)


def score_case(case, report, queries=(), *, conditional=False):
    # Reuse the native two-excerpt/provenance validator. Conditional matcher
    # output is projected to its evidence shape only; it is never a web run.
    projection = {**report, 'web': {} if conditional else report['web']}
    checked = _report(projection, case)
    expected = [e['target_span'] for e in case['expected_evidence']]
    urls = {_url(url) for e in case['expected_evidence'] for url in e['source_urls']}
    exact = checked['matches']
    expected_spans = [m['target_span'] for m in exact if m['url'] in urls]
    other_spans = [m['target_span'] for m in exact if m['url'] not in urls]
    all_spans = [m['target_span'] for m in exact]
    length = case['expected_insertion_words']
    covered = word_coverage(report['text'], expected_spans, expected)
    any_covered = word_coverage(report['text'], all_spans, expected)
    read = {_url(s['url']) for s in report['sources'] if not s.get('representation_of')}
    web = report.get('web', {})
    discovered = {_url(url) for url in web.get('discovered_urls', [])}
    pages = report['stats'].get('checked_source_count', 0)
    source_discovered = bool(urls & (discovered | read)) if length else None
    source_read = bool(urls & read) if length else None
    invalid_exact = [e for e in checked['invalid_highlights']
                     if report['matches'][e['match_index']].get('kind') == 'exact']
    if not length:
        stage = ('negative_unexpected_exact' if exact else
                 'negative_known_sources_only' if conditional else
                 'negative_indeterminate_zero_pages' if not pages else 'negative_no_exact_observed')
    elif covered == length:
        stage = 'fully_found'
    elif not source_discovered:
        stage = 'source_not_discovered'
    elif not source_read:
        stage = 'source_unread'
    else:
        stage = 'span_partial_or_missed'
    return {'execution_status': 'conditional_complete' if conditional else 'complete',
        'coverage_status': 'known_sources_only' if conditional else web.get('status', 'unknown'),
        'expected_words_matched': covered if length else None,
        'any_source_words_matched': any_covered if length else None,
        'other_source_words_matched': word_coverage(report['text'], other_spans, expected) if length else None,
        'fully_found_expected_source': bool(length) and covered == length,
        'fully_found_any_source': bool(length) and any_covered == length,
        'source_discovered': None if conditional else source_discovered, 'source_read': source_read,
        'query_coverage_words': None if conditional or not length else query_coverage(report['text'], queries, expected),
        'pages_read': pages, 'zero_pages': not pages,
        'false_100': None if conditional else not pages and report['stats'].get('uniqueness_percent') == 100,
        'valid_exact_pairs': len(exact), 'invalid_exact_pairs': len(invalid_exact),
        'invalid_highlights': checked['invalid_highlights'],
        'negative_has_exact': bool(exact) if not length else None,
        'primary_attempts': None if conditional else web.get('primary_queries_attempted', web.get('queries_attempted')),
        'secondary_attempts': None if conditional else web.get('post_wiki_queries_attempted', 0),
        'errors_by_code': dict(Counter(e.get('code', 'unknown') for e in web.get('errors', []))),
        'expected_fetch_errors': [e.get('code') for e in web.get('errors', [])
                                  if isinstance(e.get('url'), str) and _url(e['url']) in urls],
        'miss_stage': stage}


def aggregate(rows):
    groups = []
    for length in [6, 9, 12, 20, 0]:
        all_rows = [r for r in rows if r['insertion_words'] == length]
        executed = [r for r in all_rows if r['execution_status'] in {'complete', 'conditional_complete', 'failure'}]
        groups.append({'insertion_words': length, 'total_cases': len(all_rows),
            'executed': len(executed), 'unexecuted': len(all_rows) - len(executed),
            'complete_reports': sum(r['execution_status'] in {'complete', 'conditional_complete'} for r in all_rows),
            'failures': sum(r['execution_status'] == 'failure' for r in all_rows),
            'fully_found_expected_source': sum(r.get('fully_found_expected_source') is True for r in all_rows),
            'fully_found_any_source': sum(r.get('fully_found_any_source') is True for r in all_rows),
            'expected_words_matched': sum(r.get('expected_words_matched') or 0 for r in all_rows),
            'any_source_words_matched': sum(r.get('any_source_words_matched') or 0 for r in all_rows),
            'other_source_words_matched': sum(r.get('other_source_words_matched') or 0 for r in all_rows),
            'expected_words_executed': length * len(executed), 'expected_words_total': length * len(all_rows),
            'unknown_expected_words_executed': sum(length for r in executed if r.get('expected_words_matched') is None),
            'query_coverage_words': sum(r.get('query_coverage_words') or 0 for r in all_rows),
            'expected_source_discovered': sum(r.get('source_discovered') is True for r in all_rows),
            'expected_source_read': sum(r.get('source_read') is True for r in all_rows),
            'zero_page_cases': sum(r.get('zero_pages') is True for r in all_rows),
            'false_100_cases': sum(r.get('false_100') is True for r in all_rows),
            'negative_exact_cases': sum(r.get('negative_has_exact') is True for r in all_rows),
            'invalid_exact_pairs': sum(r.get('invalid_exact_pairs') or 0 for r in all_rows),
            'coverage_statuses': dict(Counter(r.get('coverage_status', 'none') for r in executed)),
            'miss_stages': dict(Counter(r['miss_stage'] for r in executed if r.get('miss_stage')))})
    return groups


def evaluate(out):
    config = read_hashed(out / 'config.json')
    corpus = Path(config['corpus'])
    if verify_corpus(corpus) != config['corpus_identity']:
        raise ValueError('Corpus changed before scoring')
    gold = {c['id']: c for c in read_json(corpus / 'gold.json')['cases']}
    selected = set(config['selection']['case_ids'])
    live, conditional, scorer_cases = [], [], []
    for cid in config['all_case_ids']:
        case = gold[cid]
        base = {'id': cid, 'family_id': case['family_id'], 'insertion_words': case['expected_insertion_words']}
        directory = out / 'results' / cid
        report_path = directory / 'report.json'
        if report_path.exists():
            report = read_hashed(report_path)
            query_events = read_hashed(directory / 'queries.json')
            row = {**base, **score_case(case, report, [q['query'] for q in query_events]),
                   'request_id': config['request_ids'][cid], 'report_sha256': digest(report_path.read_bytes())}
            score_input = deepcopy(case)
            score_input['report_path'] = str(report_path.resolve())
            for evidence in score_input['expected_evidence']:
                evidence['source_snapshot_path'] = str((corpus / evidence['source_snapshot_path']).resolve())
            scorer_cases.append(score_input)
        else:
            failed = (directory / 'failure.json').exists()
            if failed:
                read_hashed(directory / 'failure.json')
            row = {**base, 'execution_status': 'failure' if failed else 'not_executed',
                   'reason': 'first_run_failed' if failed else 'quota' if cid not in selected else 'blocked_before_execution',
                   'expected_words_matched': None, 'fully_found_expected_source': None,
                   'query_coverage_words': None, 'pages_read': None, 'miss_stage': None}
        live.append(row)
        path = out / 'conditional' / cid / 'report.json'
        if path.exists():
            report = read_hashed(path)
            conditional.append({**base, **score_case(case, report, conditional=True)})
        else:
            conditional.append({**base, 'execution_status': 'not_executed',
                                'reason': 'conditional_not_run', 'expected_words_matched': None})
    validation = None
    if scorer_cases:
        manifest = {'schema_version': 1, 'frozen_config_sha256': digest((out / 'config.json').read_bytes()),
                    'cases': scorer_cases}
        # A new scoring projection; original corpus/gold are never rewritten.
        path = out / 'validation-manifest.json'
        if not path.exists():
            write_hashed(path, manifest)
        elif read_hashed(path) != manifest:
            raise ValueError('Changed validation projection')
        validation = score_manifest(path, split='test', allow_test=True,
                                    frozen_config_sha256=manifest['frozen_config_sha256'])
    return {'schema_version': 1, 'computed_at': now(), 'source_commit': config['production_commit'],
        'query_strategy': config['query_strategy'], 'matcher_version': config['matcher_version'],
        'corpus_identity': config['corpus_identity'],
        'live': {'groups': aggregate(live), 'cases': live},
        'conditional': {'groups': aggregate(conditional), 'cases': conditional},
        'native_validation': validation,
        'limitations': ['Constructed abstract insertions, not population accuracy.',
                        'Family variants and cached phrases are dependent.',
                        'Unexecuted cases are null; zero pages do not prove uniqueness.',
                        'Conditional known-source matching is not internet discovery.']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = evaluate(args.out)
    write_hashed(args.output, result)
    print({'live': result['live']['groups'], 'conditional': result['conditional']['groups']})


if __name__ == '__main__':
    main()
