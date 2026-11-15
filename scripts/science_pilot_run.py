#!/usr/bin/env python3
"""Bind one Git-pinned candidate offline, then execute only explicitly authorized first cases."""
import argparse
from datetime import datetime
import io
from pathlib import Path
import re
import subprocess
import sys
import tarfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.night_evaluation_io import (CachedSearch, EvaluationStopped, digest, now,
    read_hashed, read_json, write_bytes, write_hashed)
from scripts.science_pilot import verify_dataset, inside
from scripts.night_evaluation import run_lock

APPROVED_STRATEGIES = frozenset({'hybrid-citation-v13-empty-nine-suffix-six'})


class CaseBoundSearch(CachedSearch):
    """The pinned candidate may generate adaptive phrases, within six primary slots."""
    def begin_case(self):
        self.case_calls = 0

    def search(self, query, request_id):
        if not hasattr(self, 'case_calls') or self.case_calls >= 6:
            raise EvaluationStopped('Case primary limit reached before transport')
        self.case_calls += 1
        self.allowed = self.allowed | {query}
        return super().search(query, request_id)


def bind(corpus, repo, candidate, out, plan, strategy):
    if strategy not in APPROVED_STRATEGIES:
        raise EvaluationStopped('Explicit approved strategy required before binding')
    if not re.fullmatch('[a-f0-9]{40}', candidate):
        raise EvaluationStopped('Exact full candidate SHA required')
    identity, manifest, sources = verify_dataset(corpus)
    plan_bytes = plan.read_bytes()
    if plan.resolve().is_relative_to(corpus.resolve()):
        raise EvaluationStopped('Approved plan must be outside immutable data')
    pilot_id = identity['payload_sha256']
    # Dataset copies share the approved plan's stable allocation namespace.
    # Authorization additionally pins all canonical locations, so copying the
    # approved plan cannot turn the same approval into another budget.
    allocation = plan.resolve().parent / 'pilot-allocations' / pilot_id / 'candidate-allocation.json'
    locations = {'pilot_id': pilot_id, 'authorized_corpus_root': str(corpus.resolve()),
                 'allocation_anchor': str(allocation), 'output_root': str(out.resolve()),
                 'plan_origin': str(plan.resolve())}
    if allocation.exists():
        raise EvaluationStopped('This frozen pilot already has a candidate allocation')
    write_hashed(allocation, {**locations, 'candidate_sha': candidate, 'dataset': identity,
        'out': str(out.resolve()), 'at': now(), 'physical_cap': 18, 'query_strategy': strategy})
    archive = subprocess.check_output(['git', '-C', str(repo), 'archive', candidate])
    source = out / 'source'
    files = {}
    with tarfile.open(fileobj=io.BytesIO(archive)) as stream:
        for member in stream:
            relative = Path(member.name)
            if relative.is_absolute() or '..' in relative.parts or member.issym() or member.islnk():
                raise EvaluationStopped('Unsafe candidate archive')
            if member.isfile():
                data = stream.extractfile(member).read()
                write_bytes(source / relative, data)
                files[member.name] = digest(data)
    write_bytes(out / 'approved-plan.md', plan_bytes)
    write_hashed(out / 'binding.json', {**locations, 'at': now(), 'candidate_sha': candidate,
        'query_strategy': strategy,
        'corpus': str(corpus.resolve()), 'dataset': identity, 'source': str(source.resolve()),
        'source_files': files, 'git_archive_sha256': digest(archive),
        'plan_sha256': digest(plan_bytes), 'plan_origin': str(plan.resolve()),
        'driver_files': {name: digest((ROOT / 'scripts' / name).read_bytes()) for name in
            ['science_pilot.py', 'science_pilot_run.py', 'night_evaluation_io.py',
             'night_evaluation.py', 'night_evaluation_score.py']},
        'candidate_count': 1, 'physical_cap': 18, 'case_primary_cap': 6,
        'query_policy': 'only pinned native candidate-generated strings; adaptive phrases journalled at dispatch',
        'public_POSTs': 0, 'text_ru': 0, 'application_models': 0, 'GPU': 0})


def authorized_binding(out, authorization):
    config = read_hashed(out / 'binding.json')
    if config.get('query_strategy') not in APPROVED_STRATEGIES:
        raise EvaluationStopped('Binding has missing or wrong strategy')
    auth = read_hashed(authorization)
    for key, expected in [('approved', True), ('candidate_sha', config['candidate_sha']),
            ('dataset_payload_sha256', config['dataset']['payload_sha256']),
            ('plan_sha256', config['plan_sha256']), ('physical_cap', 18), ('case_primary_cap', 6)]:
        if type(auth.get(key)) is not type(expected) or auth[key] != expected:
            raise EvaluationStopped('Missing/mismatched candidate, audit or budget authorization')
    if auth.get('query_strategy') != config['query_strategy']:
        raise EvaluationStopped('Strategy authorization mismatch before transport')
    if not auth.get('audit_receipt') or not auth.get('owner_message_reference'):
        raise EvaluationStopped('Common audit and actual owner authorization evidence required')
    if datetime.fromisoformat(auth['authorized_at']) < datetime.fromisoformat(config['at']):
        raise EvaluationStopped('Authorization predates candidate binding')
    expected_locations = {
        'pilot_id': config['dataset']['payload_sha256'],
        'authorized_corpus_root': str(Path(config['corpus']).resolve()),
        'output_root': str(out.resolve()), 'plan_origin': str(Path(config['plan_origin']).resolve())}
    expected_locations['allocation_anchor'] = str(Path(expected_locations['plan_origin']).parent
        / 'pilot-allocations' / expected_locations['pilot_id'] / 'candidate-allocation.json')
    for key, expected in expected_locations.items():
        if config.get(key) != expected or auth.get(key) != expected:
            raise EvaluationStopped('Authorization does not pin exact pilot roots/allocation/output')
    allocation = read_hashed(Path(expected_locations['allocation_anchor']))
    if any(allocation.get(k) != v for k, v in expected_locations.items()):
        raise EvaluationStopped('Stable allocation identity differs')
    if (allocation['candidate_sha'] != config['candidate_sha'] or allocation['dataset'] != config['dataset']
            or allocation['out'] != str(out.resolve()) or allocation['physical_cap'] != 18
            or allocation['query_strategy'] != config['query_strategy']):
        raise EvaluationStopped('Pilot allocation does not authorize this output/budget')
    if digest((out / 'approved-plan.md').read_bytes()) != config['plan_sha256']:
        raise EvaluationStopped('Bound approved plan changed')
    for name, checksum in config['driver_files'].items():
        if digest((ROOT / 'scripts' / name).read_bytes()) != checksum:
            raise EvaluationStopped('Bound driver changed')
    source = Path(config['source'])
    for name, checksum in config['source_files'].items():
        if digest(inside(source, name).read_bytes()) != checksum:
            raise EvaluationStopped('Candidate source changed')
    return config


def run_bound(out, authorization, strategy):
    if strategy not in APPROVED_STRATEGIES:
        raise EvaluationStopped('Explicit approved strategy required before transport')
    config = authorized_binding(out, authorization)
    if config['query_strategy'] != strategy:
        raise EvaluationStopped('CLI strategy differs from candidate binding')
    # A fresh subprocess prevents a baseline module already in sys.modules from
    # silently being used instead of the pinned candidate.
    if any(k == 'plagiarism' or k.startswith('plagiarism.') for k in sys.modules):
        raise EvaluationStopped('Run requires a fresh process before native imports')
    sys.path.insert(0, config['source'])
    identity, manifest, sources = verify_dataset(Path(config['corpus']))
    if identity != config['dataset']:
        raise EvaluationStopped('Dataset differs from candidate binding')
    execute_cases(Path(config['corpus']), manifest, out, strategy=config['query_strategy'])


def validate_case_receipt(corpus, case, directory, strategy, queries=None):
    """Validate the first report itself; a missing failure marker proves nothing."""
    from plagiarism.web_evaluation import _report
    try:
        report = read_hashed(directory / 'report.json')
        intent = read_hashed(directory / 'intent.json')
        text = inside(corpus, case['input_file']).read_text()
        expected = {'id': case['id'], 'request_id': case['request_id'],
                    'text_sha256': case['text_sha256'], 'query_strategy': strategy, 'primary_max': 6}
        if any(intent.get(k) != v for k, v in expected.items()):
            raise EvaluationStopped('Retained case intent differs from frozen context')
        if (digest(text) != case['text_sha256'] or report.get('text') != text
                or report.get('web', {}).get('query_strategy') != strategy
                or report.get('id') not in (None, case['request_id'])):
            raise EvaluationStopped('Retained report text/strategy/request context differs')
        checked = _report(report, case)
        if checked['invalid_highlights']:
            raise EvaluationStopped('Retained report has unconfirmed highlights')
        for source in report['sources']:
            if source.get('provenance', {}).get('text_sha256') != digest(source['text']):
                raise EvaluationStopped('Retained native source checksum differs')
        stats = report['stats']
        pages = stats['checked_source_count']
        if type(pages) is not int or pages < 0 or (pages == 0 and stats.get('uniqueness_percent') is not None):
            raise EvaluationStopped('Retained report has invalid reading/uniqueness context')
        queries = read_hashed(directory / 'queries.json') if queries is None else queries
        # Native post-Wiki telemetry counts both providers in queries_attempted.
        # The journal/cap here covers only the primary provider.
        wiki = report['web'].get('post_wiki_enabled')
        if wiki is True:
            primary_attempts = report['web'].get('primary_queries_attempted')
        elif wiki is False:
            primary_attempts = report['web'].get('queries_attempted')
        else:
            raise EvaluationStopped('Retained report does not declare its provider mode')
        if (not isinstance(queries, list) or len(queries) > 6
                or type(primary_attempts) is not int or primary_attempts != len(queries)):
            raise EvaluationStopped('Retained primary query context differs')
        for index, event in enumerate(queries):
            if event.get('request_id') != digest(case['request_id'] + '\0' + str(index)):
                raise EvaluationStopped('Retained logical request identity differs')
        return queries
    except (AttributeError, KeyError, TypeError, ValueError) as error:
        raise EvaluationStopped('Invalid retained closed report/context: ' + type(error).__name__) from None


def execute_cases(corpus, manifest, out, *, strategy):
    if strategy not in APPROVED_STRATEGIES:
        raise EvaluationStopped('Missing/wrong strategy rejected before native transport')
    from plagiarism import web_search
    from plagiarism.search_serpapi import YandexSerpAPISearch
    from plagiarism.search_mediawiki import MediaWikiSearch
    from plagiarism.web_fetch import Fetcher
    from scripts.night_evaluation_score import score_case
    order = manifest['internet_case_ids']
    cases = {c['id']: c for c in manifest['cases']}
    if (out / 'run-complete.json').exists():
        receipt = read_hashed(out / 'run-complete.json')
        if receipt.get('query_strategy') != strategy:
            raise EvaluationStopped('Saved run strategy differs')
        for cid in order:
            validate_case_receipt(corpus, cases[cid], out / 'results' / cid, strategy)
        return receipt
    completed = []
    for cid in order:
        path = out / 'results' / cid
        if (path / 'failure.json').exists():
            read_hashed(path / 'failure.json')
            raise EvaluationStopped('First case failure preserved; no automatic continuation')
        if (path / 'report.json').exists():
            completed.extend(validate_case_receipt(corpus, cases[cid], path, strategy))
        elif (path / 'intent.json').exists():
            raise EvaluationStopped('Unknown first case outcome; no resume/retry')
    budget = web_search.SearchBudget(out / 'budget.sqlite3', limit=18)
    client = YandexSerpAPISearch(budget)
    search = CaseBoundSearch(client, out, [], 18, completed_queries=completed)
    needed = 18 - search.physical
    if needed > 0:
        # Native confirm_free validates an active$0 plan and extra credits0.
        quota = client.confirm_free(1)
        if quota['total_searches_left'] < needed:
            raise EvaluationStopped('Insufficient fresh free balance for remaining frozen bound')
        if not (out / 'quota-before.json').exists():
            write_hashed(out / 'quota-before.json', quota)
    for cid in order:
        directory = out / 'results' / cid
        if (directory / 'report.json').exists():
            continue
        case = cases[cid]
        text = inside(corpus, case['input_file']).read_text()
        write_hashed(directory / 'intent.json', {'id': cid, 'request_id': case['request_id'],
            'query_strategy': strategy,
            'at': now(), 'text_sha256': case['text_sha256'], 'primary_max': 6, 'automatic_retry': False})
        search.begin_case()
        offset = len(search.logical)
        try:
            report = web_search.run(text, request_id=case['request_id'], searcher=search,
                fetcher=Fetcher(), query_strategy=strategy,
                use_wiki_followup=True, wiki_searcher=MediaWikiSearch(), use_related_fallback=True)
            write_hashed(directory / 'report.json', report)
            validate_case_receipt(corpus, case, directory, strategy, search.logical[offset:])
        except Exception as error:
            write_hashed(directory / 'failure.json', {'error_type': type(error).__name__,
                'code': getattr(error, 'code', None), 'automatic_retry': False, 'at': now()})
            raise
        finally:
            write_hashed(directory / 'queries.json', search.logical[offset:])
    rows = [score_case(cases[cid], read_hashed(out / 'results' / cid / 'report.json'),
            [q['query'] for q in read_hashed(out / 'results' / cid / 'queries.json')]) for cid in order]
    receipt = {'at': now(), 'cases': 3, 'query_strategy': strategy, 'logical_primary_calls': len(search.logical),
               'physical_calls': search.physical, 'cache_hits': search.hits, 'rows': rows,
               'baseline_comparison': 'not_tested', 'public_POSTs': 0, 'application_models': 0}
    write_hashed(out / 'run-complete.json', receipt)
    write_hashed(out / 'quota-after.json', client.confirm_free(1))
    return receipt


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('command', choices=['bind', 'run'])
    p.add_argument('--out', required=True, type=Path)
    p.add_argument('--corpus', type=Path)
    p.add_argument('--repo', type=Path)
    p.add_argument('--candidate-sha')
    p.add_argument('--plan', type=Path)
    p.add_argument('--strategy', choices=sorted(APPROVED_STRATEGIES))
    p.add_argument('--authorization', type=Path)
    a = p.parse_args()
    if a.command == 'bind':
        if not all([a.corpus, a.repo, a.candidate_sha, a.plan, a.strategy]):
            p.error('bind requires explicit corpus, repo, candidate-sha, approved plan, strategy')
        bind(a.corpus, a.repo, a.candidate_sha, a.out, a.plan, a.strategy)
    else:
        if not a.authorization or not a.strategy:
            p.error('run requires explicit authorization evidence and strategy')
        with run_lock(a.out):
            print(run_bound(a.out, a.authorization, a.strategy))


if __name__ == '__main__':
    main()
