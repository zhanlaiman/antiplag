"""Actual local BetaHandler + Chromium; synthetic runner, no external requests.

Run with an already installed browser: python3 -B -m tests.browser_report_reuse --output /private/output
Race gates delay actual native responses; they never fabricate API payloads.
"""
import argparse
from hashlib import sha256
import json
from pathlib import Path
import time
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright

from plagiarism import report_reuse as reuse
from tests.test_report_reuse import JOB, TEXT, NativeReuseScenario, report


class NativeBrowser:
    def __init__(self, scenario, context):
        self.scenario, self.context = scenario, context
        self.requests, self.external, self.errors, self.held = [], [], [], []
        self.hold = None
        context.route('**/*', self.route)
        self.page = context.new_page()
        self.page.on('pageerror', lambda error: self.errors.append(str(error)))

    def route(self, route):
        request = route.request
        parsed = urlsplit(request.url)
        if f'{parsed.scheme}://{parsed.netloc}' != self.scenario.origin:
            self.external.append(request.url); route.abort(); return
        self.requests.append((request.method, parsed.path))
        if self.hold and parsed.path == self.hold:
            response = route.fetch()
            self.held.append((route, response))
        else: route.continue_()

    def wait_held(self):
        until = time.monotonic() + 5
        while not self.held and time.monotonic() < until: self.page.wait_for_timeout(20)
        assert self.held, 'Native response was not delayed'

    def release(self):
        self.hold = None
        for route, response in self.held: route.fulfill(response=response)
        self.held.clear()
        self.page.wait_for_timeout(100)

    def reset_rate(self):
        limiter = self.scenario.service.beta.tenants.report_reuse
        with limiter.lock: limiter.buckets.clear(); limiter.recent.clear()

    def find(self, surface):
        self.reset_rate()
        self.page.locator(f'#{surface}-reuse-find').click()
        expect(self.page.locator(f'#{surface}-reuse-open')).to_be_visible()

    def snapshot(self, output, name, width):
        self.page.evaluate('document.fonts.ready')
        metrics = self.page.evaluate('({width:innerWidth,scrollWidth:document.documentElement.scrollWidth})')
        assert metrics['width'] == width and metrics['scrollWidth'] <= width, metrics
        path = output / (name + '.png'); self.page.screenshot(path=str(path))
        return {'name': name, 'screenshot': str(path), 'sha256': sha256(path.read_bytes()).hexdigest(), **metrics}


def run(output):
    output.mkdir(parents=True, exist_ok=True)
    rows, gates, totals = [], [], []
    scenario = NativeReuseScenario()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            try:
                for width in (1440, 390):
                    client = scenario.client(); tenant = client.bootstrap()
                    original_id = ('2' if width == 1440 else '3') * 32
                    scenario.run_report = lambda text: {**report(text), 'created_at': '2024-02-01T12:00:00+00:00'}
                    client.check(job_id=original_id); scenario.wait(client, original_id); scenario.published(tenant, original_id)
                    context = browser.new_context(viewport={'width': width, 'height': 1000 if width == 1440 else 844}, reduced_motion='reduce')
                    context.add_cookies([{'name': key, 'value': value, 'url': scenario.origin, 'httpOnly': True} for key, value in client.cookies.items() if value])
                    ui = NativeBrowser(scenario, context); page = ui.page
                    try:
                        page.goto(scenario.origin + '/#check')
                        page.locator('#check-text').fill('Отдельный синтетический черновик редактора.')
                        page.evaluate("location.hash = '#home'")
                        page.locator('#home-text').fill(TEXT)
                        page.wait_for_timeout(100)
                        assert not any(path == '/api/reports/reuse' for _, path in ui.requests)
                        expect(page.locator('#home-report-reuse')).to_contain_text('Поиск работает для проверок после этого обновления')
                        expect(page.locator('#home-report-reuse a[href="#history"]')).to_be_visible()
                        page.evaluate('state.session.beta.report_reuse.version = 99; renderHome()')
                        expect(page.locator('#home-reuse-find')).to_have_count(0)
                        page.evaluate('state.session.beta.report_reuse.version = 1; renderHome()')
                        expect(page.locator('#home-check-button')).to_be_disabled()
                        # The feature stays available independently of external readiness/consent.
                        page.evaluate('state.status.web.configured = false; updateHomeControls()')
                        expect(page.locator('#home-reuse-find')).to_be_enabled()
                        ui.find('home')
                        expect(page.locator('#home-reuse-result')).to_contain_text('2024')
                        expect(page.locator('#home-reuse-result')).to_contain_text('неполная проверка')
                        expect(page.locator('#home-reuse-result')).to_contain_text('Яндекс через SerpApi')
                        expect(page.locator('#home-reuse-result')).to_contain_text('6 из 6')
                        expect(page.locator('#home-reuse-result')).to_contain_text('автоматический выбор SerpApi')
                        page.locator('#home-reuse-result').scroll_into_view_if_needed()
                        rows.append(ui.snapshot(output, f'chromium-{width}-home-hit', width))
                        drafts = page.evaluate('({home: state.homeText, check: state.text})')
                        before = len(scenario.calls)
                        page.locator('#home-reuse-open').click()
                        expect(page.locator('#home-report-result')).to_contain_text('Открыт сохранённый отчёт')
                        expect(page.locator('#home-report-result')).to_contain_text('2024')
                        assert page.evaluate('({home: state.homeText, check: state.text})') == drafts
                        assert len(scenario.calls) == before
                        expect(page.locator('#home-report-result .warning').filter(has_text='Открыт сохранённый отчёт')).to_be_in_viewport(ratio=1)
                        rows.append(ui.snapshot(output, f'chromium-{width}-home-open', width))
                        page.evaluate('state.status.web.configured = true; updateHomeControls()')
                        ui.find('home'); page.locator('#home-reuse-new').click()
                        expect(page.locator('#toast')).to_contain_text('Подтвердите отправку')
                        assert len(scenario.calls) == before
                        page.locator('#home-consent').check()
                        scenario.run_report = report
                        ui.find('home'); previous_report = page.evaluate('state.homeReport.id')
                        page.locator('#home-reuse-new').click()
                        page.wait_for_function('(old) => state.homeReport && state.homeReport.id !== old', arg=previous_report)
                        assert len(scenario.calls) == before + 1
                        sent = [path for method, path in ui.requests if method == 'POST' and path == '/api/web-check-jobs']
                        assert len(sent) == 1
                        expect(page.locator('#toast')).not_to_be_visible(timeout=10000)
                        gates.append({'width': width, 'gate': 'home-hit-open-zero-new-search-current-consent-new-id', 'pass': True})

                        for surface in ('home', 'check'):
                            page.evaluate('(surface) => { location.hash = "#" + surface; }', surface)
                            field = page.locator('#home-text' if surface == 'home' else '#check-text')
                            field.fill(TEXT)
                            if surface == 'check': page.locator('#mode-internet').click()
                            field.fill(TEXT + ' Другой текст для отсутствующего совпадения.')
                            ui.reset_rate(); page.locator(f'#{surface}-reuse-find').click()
                            expect(page.locator(f'#{surface}-reuse-result')).to_contain_text('прежние отчёты доступны')
                            expect(page.locator(f'#{surface}-reuse-result a[href="#history"]')).to_be_visible()
                            field.fill(TEXT)
                            for phase in ('lookup', 'open'):
                                for change in ('text-aba', 'route-aba', 'session', 'import-same-text'):
                                    field.fill(TEXT)
                                    ui.reset_rate()
                                    if phase == 'open':
                                        ui.find(surface)
                                        # The native lookup ID is known from its own persisted record.
                                        current = reuse.lookup(scenario.root, scenario.service.beta.key, tenant, TEXT)
                                        ui.hold = '/api/reports/' + current['report_id']
                                        page.evaluate('(surface) => { window.__beforeReuse = surface === "home" ? state.homeReport : state.report; }', surface)
                                        page.locator(f'#{surface}-reuse-open').click()
                                    else:
                                        ui.hold = '/api/reports/reuse'
                                        page.locator(f'#{surface}-reuse-find').click()
                                    ui.wait_held()
                                    if change == 'text-aba':
                                        field.fill(TEXT + ' Б'); field.fill(TEXT)
                                    elif change == 'route-aba':
                                        page.evaluate("location.hash = '#history'"); expect(page.locator('#history-list')).to_be_visible()
                                        page.evaluate('(surface) => { location.hash = "#" + surface; }', surface)
                                        field = page.locator('#home-text' if surface == 'home' else '#check-text')
                                        expect(field).to_be_visible()
                                    elif change == 'session': page.evaluate('refreshSession()')
                                    else:
                                        page.locator('#home-file' if surface == 'home' else '#text-file').set_input_files(
                                            {'name': 'same-synthetic.txt', 'mimeType': 'text/plain', 'buffer': TEXT.encode()})
                                        expect(field).to_have_value(TEXT)
                                    ui.release()
                                    expect(field).to_have_value(TEXT)
                                    expect(page.locator(f'#{surface}-reuse-open')).to_have_count(0)
                                    if phase == 'open':
                                        assert page.evaluate('(surface) => (surface === "home" ? state.homeReport : state.report) === window.__beforeReuse', surface)
                                    gates.append({'width': width, 'surface': surface, 'phase': phase, 'change': change, 'pass': True})
                                # Let the newer explicit request finish before the old response.
                                field.fill(TEXT)
                                if phase == 'open':
                                    ui.find(surface)
                                    current = reuse.lookup(scenario.root, scenario.service.beta.key, tenant, TEXT)
                                    ui.hold = '/api/reports/' + current['report_id']
                                    page.locator(f'#{surface}-reuse-open').click()
                                else:
                                    ui.reset_rate(); ui.hold = '/api/reports/reuse'
                                    page.locator(f'#{surface}-reuse-find').click()
                                ui.wait_held(); field.fill(TEXT + ' Б'); field.fill(TEXT); ui.hold = None
                                ui.find(surface)
                                if phase == 'open':
                                    page.locator(f'#{surface}-reuse-open').click()
                                    expect(page.locator(f'#{surface}-reuse-open')).to_have_count(0)
                                    page.evaluate('(surface) => { window.__newerReuse = surface === "home" ? state.homeReport : state.report; }', surface)
                                else: page.evaluate('(surface) => { window.__newerReuse = document.getElementById(surface + "-reuse-open"); }', surface)
                                ui.release()
                                if phase == 'open':
                                    assert page.evaluate('(surface) => (surface === "home" ? state.homeReport : state.report) === window.__newerReuse', surface)
                                else:
                                    assert page.evaluate('(surface) => document.getElementById(surface + "-reuse-open") === window.__newerReuse', surface)
                                gates.append({'width': width, 'surface': surface, 'phase': phase, 'change': 'new-request-finishes-first', 'pass': True})
                            ui.find(surface); page.locator(f'#{surface}-reuse-open').click()
                            area = page.locator('#home-report-result' if surface == 'home' else '#report-area')
                            expect(area).to_contain_text('Открыт сохранённый отчёт')
                            if surface == 'check':
                                expect(area.locator('.warning').filter(has_text='Открыт сохранённый отчёт')).to_be_in_viewport(ratio=1)
                                rows.append(ui.snapshot(output, f'chromium-{width}-check-open', width))
                        assert not ui.external and not ui.errors, (ui.external, ui.errors)
                        totals.append({'width': width, 'native_requests': len(ui.requests),
                            'lookup_requests': sum(path == '/api/reports/reuse' for _, path in ui.requests),
                            'external_requests': len(ui.external), 'page_errors': len(ui.errors)})
                    finally:
                        if ui.held: ui.release()
                        context.close()
            finally: browser.close()
    finally: scenario.close()
    proof = {'status': 'PASS', 'engine': 'chromium', 'native_api': True, 'fabricated_api_payloads': False,
             'screenshots': rows, 'gates': gates, 'totals': totals, 'server_and_publisher_stopped': True}
    (output / 'proof.json').write_text(json.dumps(proof, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'status': 'PASS', 'gates': len(gates), 'screenshots': len(rows), 'proof': str(output / 'proof.json')}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(); parser.add_argument('--output', type=Path, required=True)
    run(parser.parse_args().output.resolve())
