#!/usr/bin/env python3
"""Freeze and run a bounded local evaluation; never POST to production.

prepare and conditional are offline. run uses only the explicitly frozen inputs,
the native Yandex adapter, and an initially empty cohort response cache.
"""
import argparse
from contextlib import contextmanager
import fcntl
from importlib.metadata import version
import os
from pathlib import Path
import platform
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.night_evaluation_io import (CachedSearch, EvaluationStopped, digest, now,
    read_hashed, read_json, verify_corpus, write_hashed, write_json)


def select_prefix(cases, order, plans, adaptive, cap):
    """Maximal whole first-seen family prefix, before observing any result."""
    by_id = {case['id']: case for case in cases}
    if len(by_id) != len(cases) or len(order) != len(cases) or set(order) != set(by_id):
        raise ValueError('Run order must contain every unique case once')
    families = list(dict.fromkeys(by_id[cid]['paired_group_id'] for cid in order))
    selected, queries, dynamic, bounds = [], set(), 0, []
    for family in families:
        members = [cid for cid in order if by_id[cid]['paired_group_id'] == family]
        if sorted(by_id[cid]['expected_insertion_words'] for cid in members) != [0, 6, 9, 12, 20]:
            raise ValueError('A family must contain four lengths and its negative control')
        union = queries | {q['query'] for cid in members for q in plans[cid]}
        additional = sum(adaptive[cid] for cid in members)
        bound = len(union) + dynamic + additional
        bounds.append({'family': family, 'prefix_physical_upper': bound})
        if bound > cap:
            break
        selected.append(family)
        queries, dynamic = union, dynamic + additional
    ids = [cid for cid in order if by_id[cid]['paired_group_id'] in selected]
    return {'families': selected, 'case_ids': ids, 'allowed_queries': sorted(queries),
            'adaptive_slots_upper': dynamic, 'physical_upper': len(queries) + dynamic,
            'prefix_bounds': bounds}


def app_identity(receipt):
    from cloud.modal_web import source_manifest
    expected = receipt['source_manifest']
    actual = source_manifest(ROOT)
    if actual != expected:
        raise EvaluationStopped('Application differs from frozen production source')
    return actual


def input_text(corpus, case):
    path = (corpus / case['input_file']).resolve()
    if not path.is_relative_to(corpus.resolve()):
        raise ValueError('Input path outside corpus')
    text = path.read_text(encoding='utf-8')
    if digest(text) != case['text_sha256']:
        raise EvaluationStopped('Input checksum changed')
    return text


def prepare(args):
    from plagiarism import web_search
    from plagiarism.matching import MATCHER_VERSION
    corpus = args.corpus.resolve()
    verified = verify_corpus(corpus)
    receipt = read_json(args.deployment)
    if receipt['source_commit'] != args.expected_commit:
        raise ValueError('Unexpected production revision')
    identity = app_identity(receipt)
    manifest = read_json(corpus / 'manifest.json')
    order = read_json(corpus / 'run-order.json')['case_ids']
    plans, adaptive = {}, {}
    for case in manifest['cases']:
        text = input_text(corpus, case)
        plan = web_search.plan_queries(text, strategy=web_search.SHORT_CENTER_STRATEGY)
        plans[case['id']] = plan
        adaptive[case['id']] = int(len(web_search.plan_queries(
            text, strategy=web_search.SHORT_SENTENCE_STRATEGY)) < len(plan))
    selection = select_prefix(manifest['cases'], order, plans, adaptive, args.cap)
    # Arbitrary observed-title strings cannot be allowlisted before discovery.
    # Their upper bound is counted, but this initial implementation fails closed
    # rather than running an unregistered adaptive query.
    if selection['adaptive_slots_upper']:
        raise ValueError('Selected prefix has dynamic queries; freeze a smaller safe prefix')
    config = {'schema_version': 1, 'created_at': now(), 'track': 'local_first_use',
        'production_commit': args.expected_commit, 'application_files': identity,
        'python_version': platform.python_version(), 'query_strategy': web_search.SHORT_CENTER_STRATEGY,
        'matcher_version': MATCHER_VERSION, 'provider': 'serpapi-yandex',
        'corpus': str(corpus), 'corpus_identity': verified, 'physical_cap': args.cap,
        'selection': selection, 'all_case_ids': order, 'plans': plans,
        'request_ids': {cid: uuid4().hex for cid in selection['case_ids']},
        'driver_files': {f'scripts/{name}': digest((ROOT / 'scripts' / name).read_bytes()) for name in
                         ['night_evaluation.py', 'night_evaluation_io.py', 'night_evaluation_score.py']},
        'transport_policy': 'first response per exact provider/query; unknown outcomes never retried',
        'public_POSTs': 0, 'models': 0, 'GPU': 0, 'text_ru': 0}
    write_hashed(args.out / 'config.json', config)
    print({'prepared_cases': len(selection['case_ids']), 'families': len(selection['families']),
           'physical_upper': selection['physical_upper'], 'physical_cap': args.cap,
           'unexecuted_quota': len(order) - len(selection['case_ids'])})


def checked_config(out):
    config = read_hashed(out / 'config.json')
    if verify_corpus(Path(config['corpus'])) != config['corpus_identity']:
        raise EvaluationStopped('Corpus identity changed')
    app_identity({'source_manifest': config['application_files']})
    for file, expected in config['driver_files'].items():
        if digest((ROOT / file).read_bytes()) != expected:
            raise EvaluationStopped('Frozen driver changed: ' + file)
    return config


@contextmanager
def run_lock(out):
    out.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (out / 'runner.lock').open('a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def live_run(out):
    from plagiarism import web_search
    from plagiarism.search_serpapi import YandexSerpAPISearch
    from plagiarism.search_mediawiki import MediaWikiSearch
    from plagiarism.web_fetch import Fetcher
    from plagiarism.pdf_source import PARSER_VERSION
    import pypdf
    config = checked_config(out)
    if (out / 'run-complete.json').exists():
        receipt = read_hashed(out / 'run-complete.json')
        for cid in config['selection']['case_ids']:
            read_hashed(out / 'results' / cid / 'report.json')
        print({'reused_completed_run': True, **receipt})
        return
    corpus = Path(config['corpus'])
    cases = {c['id']: c for c in read_json(corpus / 'manifest.json')['cases']}
    completed_queries = []
    for cid in config['selection']['case_ids']:
        directory = out / 'results' / cid
        if (directory / 'report.json').exists():
            read_hashed(directory / 'report.json')
            completed_queries.extend(read_hashed(directory / 'queries.json'))
    budget = web_search.SearchBudget(out / 'budget.sqlite3', limit=config['physical_cap'])
    client = YandexSerpAPISearch(budget)
    if not (out / 'quota-before.json').exists():
        write_hashed(out / 'quota-before.json', client.confirm_free(1))
        write_hashed(out / 'environment.json', {'python': platform.python_version(),
            'pypdf': pypdf.__version__, 'pypdfium2': version('pypdfium2'),
            'parser_version': PARSER_VERSION, 'pdf_engine': os.environ.get('ISTOK_PDF_ALTERNATIVE_ENGINE')})
    search = CachedSearch(client, out, config['selection']['allowed_queries'], config['physical_cap'],
                          completed_queries=completed_queries)
    for cid in config['selection']['case_ids']:
        directory = out / 'results' / cid
        if (directory / 'report.json').exists():
            read_hashed(directory / 'report.json')
            continue
        if (directory / 'intent.json').exists():
            raise EvaluationStopped('Existing case has an unresolved first outcome; no rerun')
        case = cases[cid]
        text = input_text(corpus, case)
        request_id = config['request_ids'][cid]
        write_hashed(directory / 'intent.json', {'id': cid, 'request_id': request_id,
            'text_sha256': case['text_sha256'], 'started_at': now(), 'automatic_retry': False})
        first_event = len(search.logical)
        print({'id': cid, 'phase': 'first_run', 'physical_used': search.physical}, flush=True)
        try:
            report = web_search.run(text, request_id=request_id, searcher=search, fetcher=Fetcher(),
                query_strategy=config['query_strategy'], use_wiki_followup=True,
                wiki_searcher=MediaWikiSearch(), use_related_fallback=True)
            if report['text'] != text or report['web']['matcher_version'] != config['matcher_version']:
                raise EvaluationStopped('Unexpected report identity')
            report.setdefault('id', request_id)
            write_hashed(directory / 'report.json', report)
            write_hashed(directory / 'queries.json', search.logical[first_event:])
            print({'id': cid, 'phase': 'saved', 'pages': report['stats']['checked_source_count'],
                   'physical_used': search.physical, 'cache_hits': search.hits}, flush=True)
        except Exception as error:
            if not (directory / 'failure.json').exists():
                write_hashed(directory / 'failure.json', {'id': cid, 'request_id': request_id,
                    'failed_at': now(), 'error_type': type(error).__name__,
                    'code': getattr(error, 'code', None), 'automatic_retry': False})
            if not (directory / 'queries.json').exists():
                write_hashed(directory / 'queries.json', search.logical[first_event:])
            raise
    write_hashed(out / 'run-complete.json', {'completed_at': now(),
        'selected_cases': len(config['selection']['case_ids']),
        'logical_primary_calls': len(search.logical), 'physical_calls': search.physical,
        'cache_hits': search.hits, 'models': 0, 'public_POSTs': 0, 'text_ru': 0})
    write_hashed(out / 'quota-after.json', client.confirm_free(1))


def conditional(out):
    """Supply known snapshots to matcher, under a process-wide socket denial."""
    from plagiarism.matching import analyze
    config = checked_config(out)
    corpus = Path(config['corpus'])
    sources = read_json(corpus / 'sources.json')['sources']
    known = [{'id': s['id'], 'url': s['article_url'], 'title': 'Supplied source snapshot',
              'text': (corpus / s['source_snapshot_path']).read_text()} for s in sources]
    denied = []

    def no_network(event, arguments):
        if event.startswith(('socket.', 'urllib.', 'http.client.')):
            denied.append(event)
            raise EvaluationStopped('Network forbidden in conditional matcher track')
    sys.addaudithook(no_network)
    cases = {c['id']: c for c in read_json(corpus / 'manifest.json')['cases']}
    for cid in config['all_case_ids']:
        path = out / 'conditional' / cid / 'report.json'
        if path.exists():
            read_hashed(path)
            continue
        text = input_text(corpus, cases[cid])
        result = analyze(text, known)
        write_hashed(path, {**result, 'id': cid, 'track': 'conditional_known_sources',
            'text': text, 'sources': known, 'network_attempts': len(denied), 'search_calls': 0})
    write_hashed(out / 'conditional-complete.json', {'cases': len(cases), 'search_calls': 0,
                 'network_attempts': len(denied), 'known_sources': len(known)})
    print({'conditional_cases': len(cases), 'network_attempts': len(denied)})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['prepare', 'run', 'conditional'])
    parser.add_argument('--out', required=True, type=Path)
    parser.add_argument('--corpus', type=Path)
    parser.add_argument('--deployment', type=Path)
    parser.add_argument('--expected-commit')
    parser.add_argument('--cap', type=int, default=36)
    args = parser.parse_args(argv)
    if not 1 <= args.cap <= 36:
        parser.error('Physical cap must be between 1 and 36')
    os.umask(0o077)
    with run_lock(args.out):
        if args.command == 'prepare':
            if not all([args.corpus, args.deployment, args.expected_commit]):
                parser.error('prepare requires corpus, deployment and expected commit')
            prepare(args)
        elif args.command == 'run':
            live_run(args.out)
        else:
            conditional(args.out)


if __name__ == '__main__':
    main()
