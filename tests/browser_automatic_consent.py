"""Exercise only local frontend assets with synthetic API routes; never a search.

Needs an already installed Playwright/browser. No dependencies are installed.
Example: python3 tests/browser_automatic_consent.py --output /private/output
"""
import argparse
from copy import deepcopy
from functools import partial
from hashlib import sha256
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
from threading import Thread
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright

POOL = {'enabled': True, 'consent_version': 'search-pool-v1',
        'providers': ['serpapi-yandex', 'serper', 'tavily', 'exa']}
NAMES = ['SerpApi (Яндекс)', 'Serper (Google)', 'Tavily', 'Exa', 'Википедии']


class StaticAssets(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == '/favicon.ico':
            self.send_response(204); self.end_headers()
        else:
            super().do_GET()


class FakeAPI:
    def __init__(self, base):
        self.base, self.pool = base, deepcopy(POOL)
        self.posts, self.jobs, self.unknown, self.external = [], {}, [], []
        self.lose_next = False

    def route(self, route):
        request = route.request
        if not request.url.startswith(self.base + '/'):
            self.external.append(request.url); route.abort(); return
        path = urlsplit(request.url).path
        if not path.startswith('/api/'):
            route.continue_(); return
        status = 200
        if path == '/api/session':
            payload = {'mode': 'local', 'beta': {'id': 'a' * 64, 'max_characters': 10000,
                       'daily_remaining': 6, 'daily_checks': 6, 'monthly_remaining': 144},
                       'web': {'configured': True, 'provider': 'serpapi-yandex'}}
        elif path == '/api/status':
            payload = {'model': {'provider': 'local', 'available': False, 'models': [], 'local_inference_disabled': True},
                       'web': {'configured': True, 'provider': 'serpapi-yandex', 'automatic_search': self.pool}}
        elif path in {'/api/sources', '/api/source-packs', '/api/examples', '/api/evaluations'}:
            payload = { {'/api/sources': 'sources', '/api/source-packs': 'packs', '/api/examples': 'examples', '/api/evaluations': 'runs'}[path]: []}
        elif path == '/api/web-check-jobs' and request.method == 'POST':
            body = request.post_data_json
            self.posts.append(deepcopy(body))
            if self.lose_next:
                self.lose_next = False
                route.abort('failed'); return
            identifier = body['request_id']
            report = {'id': identifier, 'text': body['text'], 'created_at': '2026-11-15T00:00:00+00:00',
                      'scope': 'web_search', 'matches': [], 'sources': [], 'issues': [],
                      'stats': {'uniqueness_percent': None, 'total_words': len(body['text'].split()), 'matched_words': 0},
                      'web': {'status': 'partial', 'sources_checked': 0, 'queries_planned': 0,
                              'queries_completed': 0, 'urls_attempted': 0, 'errors': [], 'limitations': []}}
            self.jobs[identifier] = {'id': identifier, 'status': 'complete', 'result': {'report': report}}
            payload = {'job': self.jobs[identifier]}
        elif path.startswith('/api/jobs/'):
            identifier = path.rsplit('/', 1)[1]
            if identifier in self.jobs:
                payload = {'job': self.jobs[identifier]}
            else:
                status, payload = 404, {'error': 'Synthetic request was not received'}
        elif path.startswith('/api/reports/'):
            identifier = path.rsplit('/', 1)[1]
            payload = {'report': self.jobs[identifier]['result']['report']}
        else:
            self.unknown.append([request.method, path]); status, payload = 500, {'error': 'Unexpected synthetic API path'}
        route.fulfill(status=status, content_type='application/json', body=json.dumps(payload, ensure_ascii=False))


def settled(page, count):
    page.wait_for_function('(count) => Object.values(state.modelJobs).filter(entry => entry.status === "complete").length >= count', arg=count)


def capture(page, output, name, width):
    page.evaluate('document.fonts.ready')
    metrics = page.evaluate('({width:innerWidth,scrollWidth:document.documentElement.scrollWidth})')
    assert metrics['width'] == width and metrics['scrollWidth'] <= width, metrics
    path = output / (name + '.png')
    page.screenshot(path=str(path))
    return {'name': name, 'screenshot': str(path), **metrics}


def run(source, output, engines):
    output.mkdir(parents=True, exist_ok=True)
    service = ThreadingHTTPServer(('127.0.0.1', 0), partial(StaticAssets, directory=str(source / 'web')))
    base = f'http://127.0.0.1:{service.server_port}'
    thread = Thread(target=service.serve_forever, daemon=True); thread.start()
    rows, external, errors, unknown = [], [], [], []
    try:
        with sync_playwright() as playwright:
            for engine in engines:
                browser = getattr(playwright, engine).launch()
                try:
                    for width in [375, 1440]:
                        context = browser.new_context(viewport={'width': width, 'height': 900 if width == 375 else 1000}, reduced_motion='reduce')
                        api = FakeAPI(base); context.route('**/*', api.route)
                        page = context.new_page(); page.on('pageerror', lambda error: errors.append(str(error)))
                        try:
                            page.goto(base + '/#home')
                            expect(page.locator('.home-consent')).to_contain_text('Serper (Google)')
                            for name in NAMES: expect(page.locator('.home-consent')).to_contain_text(name)
                            page.locator('#home-text').fill('Учебный авторский текст для локальной проверки согласия.')
                            expect(page.locator('#home-check-button')).to_be_disabled()
                            page.locator('#home-consent').check()
                            expect(page.locator('#home-check-button')).to_be_enabled()
                            rows.append(capture(page, output, f'{engine}-{width}-home', width))
                            page.locator('#home-check-button').click(); settled(page, 1)
                            assert api.posts[-1]['search_provider'] == 'auto' and api.posts[-1]['provider_consent_version'] == 'search-pool-v1'
                            assert 'provider_consent' not in api.posts[-1]
                            page.evaluate("location.hash='#check'")
                            expect(page.locator('#check-text')).to_be_visible()
                            page.locator('#check-text').fill('Другой самостоятельный учебный текст для рабочей области.')
                            expect(page.locator('#check-button')).to_be_disabled()
                            page.locator('#internet-consent').check()
                            expect(page.locator('#check-button')).to_be_enabled()
                            page.locator('#internet-consent').scroll_into_view_if_needed()
                            rows.append(capture(page, output, f'{engine}-{width}-check', width))
                            page.locator('#check-button').click(); settled(page, 2)
                            page.evaluate("location.hash='#batch'")
                            expect(page.locator('#batch-files')).to_be_attached()
                            page.locator('#batch-files').set_input_files([
                                {'name': 'one.txt', 'mimeType': 'text/plain', 'buffer': 'Первый учебный документ.'.encode()},
                                {'name': 'two.txt', 'mimeType': 'text/plain', 'buffer': 'Второй учебный документ.'.encode()},
                            ])
                            expect(page.locator('.batch-document')).to_have_count(2)
                            expect(page.locator('#batch-start')).to_be_disabled()
                            page.locator('#batch-consent').check(); expect(page.locator('#batch-start')).to_be_enabled()
                            for name in NAMES: expect(page.locator('label.consent-row').first).to_contain_text(name)
                            rows.append(capture(page, output, f'{engine}-{width}-batch', width))
                            page.locator('#batch-start').click(); settled(page, 4)
                            assert len(api.posts) == 4 and len({post['request_id'] for post in api.posts}) == 4
                            assert all(post['search_provider'] == 'auto' and post['provider_consent_version'] == 'search-pool-v1' and 'provider_consent' not in post for post in api.posts)
                            page.goto(base + '/privacy.html')
                            paragraph = page.locator('p').filter(has_text='В автоматическом режиме')
                            for name in NAMES: expect(paragraph).to_contain_text(name)
                            paragraph.scroll_into_view_if_needed()
                            rows.append(capture(page, output, f'{engine}-{width}-privacy', width))
                        finally:
                            external.extend(api.external); unknown.extend(api.unknown); context.close()
                    context = browser.new_context(viewport={'width': 375, 'height': 900}, reduced_motion='reduce')
                    api = FakeAPI(base); api.pool['consent_version'] = 'search-pool-v2'; context.route('**/*', api.route)
                    page = context.new_page(); page.on('pageerror', lambda error: errors.append(str(error)))
                    try:
                        page.goto(base + '/#home')
                        expect(page.locator('#home-search-unavailable')).to_contain_text('Условия')
                        expect(page.locator('#home-consent')).to_be_disabled()
                        expect(page.locator('#home-check-button')).to_be_disabled()
                        page.locator('#home-search-unavailable').scroll_into_view_if_needed()
                        rows.append(capture(page, output, f'{engine}-375-unknown', 375))
                        assert not api.posts
                        # An explicit retry under an unknown new contract keeps the saved old body/ID.
                        api.pool = deepcopy(POOL); page.reload()
                        expect(page.locator('.home-consent')).to_contain_text('Serper (Google)')
                        page.locator('#home-text').fill('Сохранённый учебный текст для явного повтора.')
                        page.locator('#home-consent').check(); api.lose_next = True
                        page.locator('#home-check-button').click()
                        page.wait_for_function('Object.values(state.modelJobs).some(entry => entry.status === "delivery_unknown")')
                        original = deepcopy(api.posts[0]); api.pool['consent_version'] = 'search-pool-v2'; page.reload()
                        expect(page.locator('#home-search-unavailable')).to_contain_text('Условия')
                        page.evaluate("location.hash='#check'")
                        expect(page.locator('#check-text')).to_be_visible()
                        expect(page.locator('#model-job-retry-' + original['request_id'])).to_be_visible()
                        assert len(api.posts) == 1
                        page.locator('#model-job-retry-' + original['request_id']).click(); settled(page, 1)
                        assert api.posts == [original, original]
                        rows.append({'name': engine + '-retry-immutable', 'request_id_preserved': True, 'body_preserved': True, 'POSTs': 2})
                    finally:
                        external.extend(api.external); unknown.extend(api.unknown); context.close()
                finally:
                    browser.close()
    finally:
        service.shutdown(); service.server_close(); thread.join()
    with socket.socket() as connection: socket_closed = connection.connect_ex(('127.0.0.1', service.server_port)) != 0
    proof = {'source_root': str(source), 'runtime_sha256': sha256((source / 'web/app.js').read_bytes()).hexdigest(),
             'rows': rows, 'external_requests': external, 'page_errors': errors, 'unexpected_API': unknown,
             'real_searches': 0, 'models': 0, 'credentials': 0, 'cash_KZT': 0,
             'teardown': {'server_stopped': not thread.is_alive(), 'socket_closed': socket_closed, 'browsers_closed': True}}
    (output / 'results.json').write_text(json.dumps(proof, ensure_ascii=False, indent=2) + '\n')
    assert not external and not errors and not unknown, proof
    print(json.dumps({'captures': sum('screenshot' in row for row in rows), 'retry_scenarios': 2,
                      'external_requests': 0, 'page_errors': 0, 'teardown': proof['teardown']}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--engines', nargs='+', choices=['chromium', 'webkit'], default=['chromium', 'webkit'])
    args = parser.parse_args(); run(args.source.resolve(), args.output.resolve(), args.engines)
