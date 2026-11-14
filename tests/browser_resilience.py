"""Run local Beta browser regressions without a search provider or model call.

Requires an already installed Playwright and Chromium/WebKit. No installation or
production access happens here. Example: python3 tests/browser_resilience.py
--output /absolute/private/output --engines chromium webkit
"""
import argparse
from hashlib import pbkdf2_hmac
import json
import os
from pathlib import Path
import socket
import statistics
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Event, Thread
import time

from playwright.sync_api import expect, sync_playwright


def web_payload(text):
    return {
        'text': text, 'scope': 'web_search', 'matches': [], 'sources': [], 'issues': [],
        'stats': {'uniqueness_percent': None, 'total_words': len(text.split()), 'matched_words': 0},
        'web': {'status': 'partial', 'sources_checked': 0, 'queries_planned': 1,
                'queries_completed': 1, 'urls_attempted': 1,
                'errors': [{'message': 'Учебный источник недоступен', 'url': 'https://example.test/missing'}],
                'limitations': []},
    }


def large_report():
    line = 'Учебная строка 🌿 — проверка сохранённого текста и его источника.\n'
    text = (line * 3600)[:220107]
    matches = []
    for index, start in enumerate(range(0, len(text) - 45, 110)):
        excerpt = text[start:start + 45]
        matches.append({
            'id': f'match-{index + 1}', 'start': start, 'end': start + 45,
            'source_id': 'local-source', 'source_title': 'Учебный источник',
            'source_url': 'https://example.test/source', 'excerpt': excerpt,
            'source_excerpt': excerpt, 'kind': 'similar' if index % 3 == 0 else 'exact', 'score': 1,
        })
    return {
        'id': 'c' * 32, 'text': text, 'created_at': '2026-11-14T18:00:00+00:00',
        'scope': 'web_search', 'matches': matches, 'issues': [],
        'sources': [{'id': 'local-source', 'title': 'Учебный источник', 'text': text,
                     'url': 'https://example.test/source'}],
        'stats': {'total_characters': len(text), 'total_words': 36000, 'matched_words': 12000,
                  'uniqueness_upper_bound_percent': 66.67},
        'web': {'status': 'partial', 'sources_checked': 1, 'queries_planned': 2,
                'queries_completed': 1, 'urls_attempted': 2,
                'errors': [{'message': 'Учебный источник недоступен'}], 'limitations': []},
    }


class BrowserChecks:
    def __init__(self, browser, engine, base, state_root, output, calls, queued, started, store):
        self.browser, self.engine, self.base = browser, engine, base
        self.root, self.output, self.calls = state_root, output / engine, calls
        self.output.mkdir(parents=True, exist_ok=True)
        self.queued, self.started, self.store = queued, started, store
        self.rows, self.external = [], []

    def context(self):
        context = self.browser.new_context(viewport={'width': 1440, 'height': 1000}, reduced_motion='reduce')
        def local_only(route):
            if route.request.url.startswith(self.base + '/'):
                route.continue_()
            else:
                self.external.append(route.request.url)
                route.abort()
        context.route('**/*', local_only)
        return context

    def run(self, name, action):
        context = self.context()
        page = context.new_page()
        errors, posts, reads = [], [], []
        page.on('pageerror', lambda error: errors.append(str(error)))
        def request_seen(request):
            if request.url.endswith('/api/web-check-jobs') and request.method == 'POST':
                posts.append(request.post_data_json)
            if '/api/jobs/' in request.url:
                reads.append(request.url)
        page.on('request', request_seen)
        before = len(self.calls)
        row = {'scenario': name, 'engine': self.engine}
        try:
            page.goto(self.base + '/#check')
            expect(page.locator('#check-text')).to_be_visible()
            detail = action(page, context, posts, reads)
            assert not errors, errors
            expected_calls = 1 if name in {'lost_accepted_reply', 'lost_before_acceptance', 'lost_poll_reply',
                'offline_after_acceptance', 'reload_queued', 'reload_complete', 'double_click', 'expired_owner'} else 0
            assert len(self.calls) - before == expected_calls, 'Unexpected repeated synthetic execution'
            row.update(verdict='PASS', detail=detail or {})
        except Exception as error:
            row.update(verdict='FAIL', error=str(error)[:900])
        finally:
            self.queued.set()
            screenshot = self.output / (name + '.png')
            page.screenshot(path=str(screenshot))
            row.update(screenshot=str(screenshot), POSTs=len(posts), job_GETs=len(reads),
                       synthetic_runner_calls=len(self.calls) - before, page_errors=errors)
            context.close()
            row['context_closed'] = True
            self.rows.append(row)
        return row

    @staticmethod
    def submit(page, text='Самостоятельный учебный текст для проверки состояния.'):
        page.locator('#check-text').fill(text)
        page.locator('#internet-consent').check()
        page.locator('#check-button').click()

    @staticmethod
    def result(page):
        expect(page.locator('#report-area .uniqueness-score')).to_be_visible(timeout=8000)
        expect(page.locator('#report-area .uniqueness-value')).to_have_text('Не установлена')

    def lost_accepted_reply(self, page, context, posts, reads):
        def lose(route):
            assert route.fetch().status == 200
            route.abort('failed')
        page.route('**/api/web-check-jobs', lose)
        self.submit(page)
        self.result(page)
        assert len(posts) == 1 and reads
        assert page.locator('#check-text').input_value().startswith('Самостоятельный')
        return {'score': 'Не установлена', 'recovered_without_reload': True}

    def lost_before_acceptance(self, page, context, posts, reads):
        page.route('**/api/web-check-jobs', lambda route: route.abort('failed'))
        self.submit(page, 'Первый учебный текст')
        expect(page.locator('[id^="model-job-retry-"]')).to_be_visible()
        assert len(posts) == 1
        page.locator('#check-text').fill('Новая правка во время восстановления')
        page.unroute('**/api/web-check-jobs')
        page.locator('[id^="model-job-retry-"]').click()
        expect(page.locator('[id^="model-job-result-"]')).to_be_attached()
        page.locator('[id^="model-job-result-"] summary').click()
        page.locator('[id^="open-web-report-"]').click()
        self.result(page)
        assert len(posts) == 2 and posts[0] == posts[1]
        expect(page.locator('#check-text')).to_have_value('Новая правка во время восстановления')
        expect(page.locator('#stale-report')).to_be_visible()
        return {'explicit_retry_same_body_and_id': True, 'new_draft_preserved': True}

    def lost_poll_reply(self, page, context, posts, reads):
        failures = []
        def lose_once(route):
            if not failures:
                failures.append(1)
                route.abort('failed')
            else:
                route.continue_()
        page.route('**/api/jobs/*', lose_once)
        self.submit(page)
        self.result(page)
        assert len(posts) == 1 and len(reads) >= 2
        return {'GET_retry_only': True}

    def offline_after_acceptance(self, page, context, posts, reads):
        def lose(route):
            assert route.fetch().status == 200
            context.set_offline(True)
            route.abort('failed')
        page.route('**/api/web-check-jobs', lose)
        self.submit(page)
        expect(page.locator('[id^="model-job-retry-"]')).to_be_visible()
        assert len(posts) == 1
        context.set_offline(False)
        page.unroute('**/api/web-check-jobs', lose)
        page.reload()
        self.result(page)
        assert len(posts) == 1
        return {'offline_ID_and_draft_restored_by_GET': True}

    def reload_queued(self, page, context, posts, reads):
        self.queued.clear()
        self.started.clear()
        self.submit(page, '[queue] Учебный текст, который ещё обрабатывается')
        assert self.started.wait(3)
        expect(page.locator('#model-jobs-panel')).to_contain_text('Учебная очередь')
        page.reload()
        expect(page.locator('#check-text')).to_have_value('[queue] Учебный текст, который ещё обрабатывается')
        self.queued.set()
        self.result(page)
        assert len(posts) == 1
        return {'reload_only_observes_existing_job': True}

    def reload_complete(self, page, context, posts, reads):
        self.submit(page)
        self.result(page)
        report_id = page.evaluate('state.report.id')
        page.reload()
        self.result(page)
        assert len(posts) == 1 and page.evaluate('state.report.id') == report_id
        return {'report_identical_after_reload': True}

    def double_click(self, page, context, posts, reads):
        page.locator('#check-text').fill('Учебный текст двойного нажатия')
        page.locator('#internet-consent').check()
        page.locator('#check-button').dblclick()
        self.result(page)
        assert len(posts) == 1

    def http_errors(self, page, context, posts, reads):
        observed = []
        for status in [401, 429]:
            for html in [False, True]:
                def fault(route, request=None, *, status=status, html=html):
                    route.fulfill(status=status, content_type='text/html' if html else 'application/json',
                                  body='<h1>Учебная ошибка</h1>' if html else json.dumps({'error': 'Учебная ошибка'}))
                page.route('**/api/jobs/*', fault)
                result = page.evaluate('''() => Promise.race([
                    readJobWithRetry('d'.repeat(32),()=>{}).then(()=>({unexpected:true}),e=>({status:e.status??null})),
                    new Promise(resolve=>setTimeout(()=>resolve({timeout:true}),2000))])''')
                assert result == {'status': status}, result
                observed.append({'status': status, 'body': 'html' if html else 'json'})
                page.unroute('**/api/jobs/*', fault)
        assert len(reads) == 4
        return {'terminal_HTTP_statuses': observed}

    def quota_rejection(self, page, context, posts, reads):
        page.route('**/api/web-check-jobs', lambda route: route.fulfill(status=429,
                   content_type='application/json', body=json.dumps({'error': 'Учебный дневной лимит исчерпан', 'code': 'beta_daily_limit'})))
        self.submit(page, 'Черновик, сохранённый при исчерпанной квоте')
        expect(page.locator('#toast')).to_contain_text('лимит исчерпан')
        expect(page.locator('#check-text')).to_have_value('Черновик, сохранённый при исчерпанной квоте')
        assert len(posts) == 1 and not reads and page.locator('.uniqueness-score').count() == 0

    def corrupt_document(self, page, context, posts, reads):
        page.locator('#check-text').fill('Черновик до повреждённого импорта')
        page.locator('#text-file').set_input_files({'name': 'corrupt.docx',
                   'mimeType': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', 'buffer': b'not a zip'})
        expect(page.locator('#check-text-import-hint')).to_be_visible()
        expect(page.locator('#check-text-import-hint')).not_to_contain_text('Извлекаем')
        expect(page.locator('#check-text')).to_have_value('Черновик до повреждённого импорта')
        assert not posts

    def expired_owner(self, page, context, posts, reads):
        initial_guest = context.request.get(self.base + '/api/session').json()['beta']['id']
        page.locator('#check-text').fill('Отдельный гостевой черновик')
        response = context.request.post(self.base + '/api/owner/login', headers={'Origin': self.base},
                                        data={'username': 'qa-owner', 'password': 'local-qa-password'})
        assert response.status == 200
        page.reload()
        expect(page.locator('#account-name')).to_contain_text('Владелец')
        owner_id = context.request.get(self.base + '/api/session').json()['beta']['id']
        page.locator('#check-text').fill('Отдельный черновик владельца')
        self.submit(page, 'Сохранённый учебный отчёт владельца')
        self.result(page)
        report_id = page.evaluate('state.report.id')
        page.locator('#check-text').fill('Отдельный черновик владельца')
        cookies = context.cookies()
        context.clear_cookies()
        context.add_cookies([item for item in cookies if item['name'] != 'istok_owner'])
        page.evaluate('refreshSession()')
        expect(page.locator('#account-name')).to_have_text('Гостевое пространство')
        denied = context.request.get(self.base + '/api/reports/' + report_id, headers={'Origin': self.base})
        status = context.request.get(self.base + '/api/session').json()['beta']
        assert denied.status == 404, {'expired_owner_report_access': denied.status, 'owner': status['owner']}
        expect(page.locator('#check-text')).to_have_value('' if owner_id == initial_guest else 'Отдельный гостевой черновик')
        drafts = page.evaluate("Object.entries(localStorage).filter(([key])=>key.startsWith('istok.draft.v1'))")
        assert any(value == 'Отдельный черновик владельца' for key, value in drafts)
        response = context.request.post(self.base + '/api/owner/login', headers={'Origin': self.base},
                                        data={'username': 'qa-owner', 'password': 'local-qa-password'})
        assert response.status == 200
        page.reload()
        expect(page.locator('#check-text')).to_have_value('Отдельный черновик владельца')
        assert context.request.get(self.base + '/api/reports/' + report_id).status == 200
        return {'expired_owner_report_access': 404, 'owner_draft_and_report_restored_after_login': True}

    def overlapping_unicode(self, page, context, posts, reads):
        guest = context.request.get(self.base + '/api/session').json()['beta']['id']
        text = 'Начало 🌿 цитаты и фраза с другим источником. Конец.'
        report = large_report()
        report.update(id='f' * 32, text=text)
        report['matches'] = []
        for identifier, start, end, title, kind in [('a', 7, 30, 'Первый источник', 'exact'),
                                                    ('b', 12, 44, 'Второй источник', 'similar')]:
            report['matches'].append({'id': identifier, 'start': start, 'end': end, 'source_id': identifier,
                    'source_title': title, 'source_url': 'https://example.test/' + identifier,
                    'excerpt': text[start:end], 'source_excerpt': text[start:end], 'kind': kind, 'score': 1})
        self.store(self.root / 'tenants' / guest / 'istok.sqlite3').save('report', report)
        page.goto(self.base + '/#check/' + report['id'])
        expect(page.locator('[data-match="b"]')).to_be_visible()
        page.locator('[data-match="b"]').click()
        expect(page.locator('#comparison-area')).to_contain_text('Второй источник')
        assert page.locator('.annotated-text').inner_text() == text
        highlighted = page.locator('.annotated-text mark.selected').all_text_contents()
        assert ''.join(highlighted) == text[12:44]
        assert page.locator('.annotated-text mark.selected.fuzzy').count() > 0
        page.locator('.annotated-text [data-highlight="a"]').first.press('Enter')
        expect(page.locator('#comparison-area')).to_contain_text('Первый источник')
        highlighted = page.locator('.annotated-text mark.selected').all_text_contents()
        assert ''.join(highlighted) == text[7:30]
        assert not posts
        return {'Unicode_and_overlaps_preserved': True}

    def async_review_badge(self, page, context, posts, reads):
        guest = context.request.get(self.base + '/api/session').json()['beta']['id']
        store = self.store(self.root / 'tenants' / guest / 'istok.sqlite3')
        report = large_report()
        report.update(id='d' * 32, text=report['text'][:300])
        report['matches'] = report['matches'][:2]
        report['sources'][0]['text'] = report['text']
        store.save('report', report)
        page.goto(self.base + '/#check/' + report['id'])
        first = page.locator('[data-match="match-1"]')
        second = page.locator('[data-match="match-2"]')
        first.click()
        card = first.element_handle()
        listing = page.locator('.match-list').element_handle()
        pending, submitted = [], []
        def delay_reply(route):
            submitted.append(route.request.post_data_json)
            response = route.fetch()
            assert response.status == 200
            pending.append((route, response))
        page.route('**/api/report-reviews', delay_reply)
        def release():
            deadline = time.monotonic() + 5
            while not pending and time.monotonic() < deadline:
                page.wait_for_timeout(10)
            assert len(pending) == 1, 'Expected exactly one held review response'
            route, response = pending.pop()
            route.fulfill(response=response)
        page.locator('#finding-comment').fill('Сохранённый комментарий')
        page.locator('[data-finding-decision="confirmed"]').click()
        second.click()
        release()
        badge = first.locator('.finding-badge')
        expect(badge).to_have_text('Подтверждено редактором')
        expect(badge).to_have_attribute('role', 'status')
        expect(first).to_have_attribute('aria-pressed', 'false')
        expect(second).to_have_attribute('aria-pressed', 'true')
        expect(page.locator('#finding-save-status')).to_be_empty()
        assert 'Подтверждено' not in page.locator('.finding-review').inner_text()
        assert store.match_reviews(report['id'])['match-1']['decision'] == 'confirmed'
        first.scroll_into_view_if_needed()
        page.screenshot(path=str(self.output / 'async_review_badge-card.png'))
        first.click()
        expect(page.locator('#finding-comment')).to_have_value('Сохранённый комментарий')
        expect(page.locator('.finding-review')).to_contain_text('Подтверждено редактором')
        for decision, label in [('dismissed', 'Отклонено редактором'), ('pending', '')]:
            page.locator('[data-finding-decision="' + decision + '"]').click()
            release()
            page.wait_for_function('(decision) => state.report.reviews["match-1"].decision === decision', arg=decision)
            if label:
                expect(badge).to_have_text(label)
                expect(badge).to_have_class('finding-badge dismissed')
            else:
                expect(badge).to_have_count(0)
                expect(page.locator('[data-finding-decision="pending"]')).to_be_hidden()
            expect(page.locator('#finding-save-status')).to_have_text('Решение по находке сохранено.')
            expect(page.locator('#finding-save-status')).to_have_attribute('role', 'status')
            assert card.evaluate('(node) => node.isConnected')
            assert listing.evaluate('(node) => node.isConnected')
        # A fresh comparison panel must receive the reply without replacing a newer draft.
        page.locator('[data-finding-decision="confirmed"]').click()
        second.click()
        first.click()
        page.locator('#finding-comment').fill('Новая правка во время сохранения')
        release()
        expect(badge).to_have_text('Подтверждено редактором')
        expect(page.locator('#finding-comment')).to_have_value('Новая правка во время сохранения')
        expect(page.locator('#finding-comment')).to_be_focused()
        expect(page.locator('#finding-save-status')).to_contain_text('Новые изменения комментария ещё не сохранены')
        assert page.evaluate('state.findingDrafts["' + report['id'] + ':match-1"]') == 'Новая правка во время сохранения'
        assert all(row['report_id'] == report['id'] and row['match_id'] == 'match-1' for row in submitted)
        assert [row['decision'] for row in submitted] == ['confirmed', 'dismissed', 'pending', 'confirmed']
        assert card.evaluate('(node) => node.isConnected')
        assert listing.evaluate('(node) => node.isConnected')
        card.dispose()
        listing.dispose()
        page.set_viewport_size({'width': 375, 'height': 900})
        page.locator('.finding-review').scroll_into_view_if_needed()
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        assert not posts
        return {'saved_decisions': [row['decision'] for row in submitted], 'card_and_list_retained': True,
                'newer_draft_preserved': True, 'correct_report_and_match': True, 'mobile_no_overflow': True}

    def review_report_guard(self, page, context, posts, reads):
        guest = context.request.get(self.base + '/api/session').json()['beta']['id']
        store = self.store(self.root / 'tenants' / guest / 'istok.sqlite3')
        for identifier in ('d' * 32, 'e' * 32):
            report = large_report()
            report.update(id=identifier, text=report['text'][:300])
            report['matches'] = report['matches'][:2]
            report['sources'][0]['text'] = report['text']
            store.save('report', report)
        page.goto(self.base + '/#check/' + 'd' * 32)
        page.locator('[data-match="match-1"]').click()
        pending = []
        def delay_reply(route):
            response = route.fetch()
            assert response.status == 200
            pending.append((route, response))
        page.route('**/api/report-reviews', delay_reply)
        page.locator('[data-finding-decision="confirmed"]').click()
        page.goto(self.base + '/#check/' + 'e' * 32)
        page.locator('[data-match="match-1"]').click()
        assert len(pending) == 1
        route, response = pending.pop()
        with page.expect_response(self.base + '/api/report-reviews') as reply:
            route.fulfill(response=response)
        # Flush completion of the old handler before checking the new report.
        assert reply.value.json()['review']['decision'] == 'confirmed'
        page.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
        assert page.evaluate('state.report.id') == 'e' * 32
        expect(page.locator('[data-finding-decision="confirmed"]')).to_be_enabled()
        expect(page.locator('.finding-badge')).to_have_count(0)
        expect(page.locator('#finding-save-status')).to_be_empty()
        assert not page.evaluate('state.report.reviews?.["match-1"]')
        assert store.match_reviews('d' * 32)['match-1']['decision'] == 'confirmed'
        assert not store.match_reviews('e' * 32)
        assert not posts
        return {'old_report_saved': True, 'new_report_with_same_match_id_unchanged': True}

    def score_states(self, page, context, posts, reads):
        guest = context.request.get(self.base + '/api/session').json()['beta']['id']
        store = self.store(self.root / 'tenants' / guest / 'istok.sqlite3')
        labels = []
        for index, (kind, label) in enumerate([('exact', '0%'), ('bound', 'Не выше 0%'), ('unknown', 'Не установлена')]):
            report = web_payload('Учебный текст с нулевой или неизвестной оригинальностью')
            report.update(id=str(index + 1) * 32, created_at='2026-11-14T18:00:00+00:00')
            report['stats']['uniqueness_percent'] = 0 if kind == 'exact' else 100
            if kind != 'unknown':
                report['sources'] = [{'id': 'sample', 'title': 'Учебный источник', 'text': 'Сохранённый учебный источник', 'url': ''}]
                report['web']['sources_checked'] = 1
            if kind == 'exact':
                report['web'].update(status='complete', errors=[])
            else:
                report['stats']['uniqueness_upper_bound_percent'] = 0 if kind == 'bound' else 100
            store.save('report', report)
            page.goto(self.base + '/#check/' + report['id'])
            expect(page.locator('#report-area .uniqueness-value')).to_have_text(label)
            labels.append(label)
        assert not posts
        return {'distinct_states': labels}

    def large(self, page, context, posts, reads):
        guest = context.request.get(self.base + '/api/session').json()['beta']['id']
        report = large_report()
        self.store(self.root / 'tenants' / guest / 'istok.sqlite3').save('report', report)
        page.goto(self.base + '/#check/' + report['id'])
        expect(page.locator('.annotated-text')).to_be_visible()
        card = page.locator('.match-item').first.element_handle()
        render = page.evaluate('''()=>{let t=performance.now();renderReport();return performance.now()-t}''')
        card.dispose()
        card = page.locator('.match-item').first.element_handle()
        timings = []
        for index in [2, 3, 4]:
            timings.append(page.evaluate('''index=>new Promise(resolve=>{let t=performance.now();
                document.querySelectorAll('.match-item')[index].click();
                requestAnimationFrame(()=>requestAnimationFrame(()=>resolve(performance.now()-t)))})''', index))
        retained_card = card.evaluate('(element)=>element.isConnected')
        card.dispose()
        assert retained_card, 'Choosing a fragment recreated the whole list'
        assert page.locator('.match-item').count() == len(report['matches'])
        expect(page.locator('.match-item[aria-pressed="true"]')).to_have_count(1)
        selected = page.locator('.annotated-text mark').nth(2)
        selected.press('Enter')
        expect(page.locator('#comparison-area')).to_be_focused()
        expect(page.locator('#comparison-area')).to_contain_text('Учебный источник')
        page.locator('#finding-comment').fill('Учебный комментарий к выбранному совпадению')
        page.locator('.annotated-text mark').nth(3).press('Space')
        page.locator('.annotated-text mark').nth(2).press('Enter')
        expect(page.locator('#finding-comment')).to_have_value('Учебный комментарий к выбранному совпадению')
        snapshots = []
        for width in [320, 375, 768, 1024, 1440]:
            page.set_viewport_size({'width': width, 'height': 1000})
            page.locator('.uniqueness-score').scroll_into_view_if_needed()
            metrics = page.evaluate('''()=>({viewport:innerWidth,scroll_width:document.documentElement.scrollWidth,
                score:document.querySelector('.uniqueness-value').textContent})''')
            assert metrics['viewport'] == metrics['scroll_width']
            assert metrics['score'] == 'Не выше 66,7%'
            path = self.output / f'large-{width}.png'
            page.screenshot(path=str(path))
            snapshots.append({**metrics, 'screenshot': str(path)})
        memory = {}
        if self.engine == 'chromium':
            cdp = context.new_cdp_session(page)
            cdp.send('Performance.enable')
            cdp.send('HeapProfiler.collectGarbage')
            values = {item['name']: item['value'] for item in cdp.send('Performance.getMetrics')['metrics']}
            memory = {'retained_JS_heap_after_GC_bytes': values['JSHeapUsedSize'], 'DOM_nodes_after_GC': values['Nodes']}
            cdp.detach()
        assert not posts
        return {'characters': len(report['text']), 'matches': len(report['matches']), 'render_cpu_ms': render,
                'selection_next_frame_ms': timings, 'selection_median_ms': statistics.median(timings),
                'card_identity_retained': retained_card, 'viewports': snapshots, **memory}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--engines', nargs='+', choices=['chromium', 'webkit'], default=['chromium', 'webkit'])
    args = parser.parse_args()
    source, output = args.source.resolve(), args.output.resolve()
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    sys.path.insert(0, str(source))
    from cloud.web_beta import create_server
    from plagiarism.storage import Store
    os.environ.update(ISTOK_TAVILY_API_KEY='local-synthetic-fixture', ISTOK_SEARCH_PROVIDER='tavily', ISTOK_MODEL_PROVIDER='local',
                      ISTOK_OWNER_USERNAME='qa-owner', ISTOK_OWNER_SALT='00' * 16,
                      ISTOK_OWNER_DIGEST=pbkdf2_hmac('sha256', b'local-qa-password', bytes(16), 600000).hex())
    os.environ.pop('ISTOK_CORRECTOR_DEPLOYMENT', None)
    calls, queued, started, rows, external = [], Event(), Event(), [], []
    queued.set()
    def runner(text, identifier, progress, **kwargs):
        calls.append(identifier)
        if text.startswith('[queue]'):
            progress('Учебная очередь', 0, 1)
            started.set()
            if not queued.wait(15):
                raise AssertionError('Queue fixture was not released')
        return web_payload(text)
    with TemporaryDirectory(prefix='istok-browser-resilience-') as directory:
        root = Path(directory)
        service = create_server(root, 'http://127.0.0.1', host='127.0.0.1', port=0, allow_http=True, runner=runner)
        base = f'http://127.0.0.1:{service.server_port}'
        service.beta.origin, service.beta.host = base, base.split('//')[1]
        thread = Thread(target=service.serve_forever, daemon=True)
        thread.start()
        try:
            with sync_playwright() as playwright:
                for engine in args.engines:
                    browser = getattr(playwright, engine).launch()
                    try:
                        checks = BrowserChecks(browser, engine, base, root, output, calls, queued, started, Store)
                        for name in ['lost_accepted_reply', 'lost_before_acceptance', 'lost_poll_reply', 'offline_after_acceptance', 'reload_queued',
                                     'reload_complete', 'double_click', 'http_errors', 'quota_rejection',
                                     'corrupt_document', 'expired_owner', 'score_states', 'overlapping_unicode',
                                     'async_review_badge', 'review_report_guard', 'large']:
                            row = checks.run(name, getattr(checks, name))
                            print(json.dumps({k: row[k] for k in ['engine', 'scenario', 'verdict']}), flush=True)
                        rows.extend(checks.rows)
                        external.extend(checks.external)
                    finally:
                        browser.close()
        finally:
            queued.set()
            service.shutdown()
            service.server_close()
            service.beta.tenants.close()
            thread.join()
        proof = {'source_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=source, text=True).strip(),
                 'source_root': str(source), 'at': time.time(), 'rows': rows, 'external_browser_requests': external,
                 'real_searches': 0, 'models': 0, 'cash_spend': 0,
                 'teardown': {'server_thread_stopped': not thread.is_alive(), 'browsers_closed': True,
                              'server_port': service.server_port, 'temporary_state_only': True}}
        with socket.socket() as probe:
            proof['teardown']['listening_socket_closed'] = probe.connect_ex(('127.0.0.1', service.server_port)) != 0
    proof['teardown']['temporary_state_removed'] = not root.exists()
    (output / 'results.json').write_text(json.dumps(proof, ensure_ascii=False, indent=2) + '\n')
    failures = sum(row['verdict'] == 'FAIL' for row in rows)
    print(json.dumps({'scenarios': len(rows), 'failed': failures, 'external_browser_requests': len(external), 'output': str(output)}))
    return 1 if failures or external else 0


if __name__ == '__main__':
    raise SystemExit(main())
