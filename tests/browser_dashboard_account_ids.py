"""Native dashboard identifier smoke; synthetic state, loopback network only.

python3 tests/browser_dashboard_account_ids.py --output /absolute/evidence
Requires an already installed Playwright/Chromium. No production accounts or SDKs.
"""
import argparse
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import secrets
import socket
import sys
from tempfile import TemporaryDirectory
from threading import Thread
from urllib.parse import urlsplit
from unittest.mock import patch

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from cloud.dashboard import DashboardConfig, password_digest
from cloud.web_beta import BetaHandler, create_server
from playwright.sync_api import expect, sync_playwright


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True)
    output = Path(parser.parse_args().output)
    output.mkdir(parents=True, exist_ok=True)
    files = ['web/dashboard.js', 'web/dashboard.html', 'web/dashboard.css']
    hashes = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in files}
    report = {'checks': [], 'screenshots': [], 'foreign_network': [], 'page_errors': [], 'work_calls': [],
              'native_handler': 'cloud.web_beta.BetaHandler', 'source_hashes': hashes}

    def check(value, name):
        report['checks'].append({'name': name, 'passed': bool(value)})
        assert value, name

    def deny_work(*args, **kwargs):
        report['work_calls'].append('forbidden')
        raise AssertionError('Provider/model work forbidden')

    with TemporaryDirectory(prefix='synthetic-dashboard-ids-') as state:
        with socket.socket() as available:
            available.bind(('127.0.0.1', 0))
            port = available.getsockname()[1]
        origin = f'http://127.0.0.1:{port}'
        password, salt = secrets.token_urlsafe(32), secrets.token_hex(32)
        config = DashboardConfig('synthetic_ids_admin', salt, password_digest(password, salt))
        service = create_server(Path(state), origin, host='127.0.0.1', port=port, allow_http=True,
                                integrated=True, registration_enabled=True, business_writes_enabled=True,
                                dashboard_config=config, runner=deny_work, corrector=deny_work)
        check(service.RequestHandlerClass is BetaHandler, 'native-production-handler')
        thread = Thread(target=service.serve_forever, daemon=True)
        thread.start()
        try:
            users = []
            for number in (1, 2):
                guest = str(number) * 64
                service.beta.admit_space(guest)
                access = service.accounts.access
                _, user = access.register(f'synthetic_ids_{number}', secrets.token_urlsafe(32), guest,
                                          access.ledger.create_device(), '127.0.0.1')
                users.append(user)
            guest_only = '3' * 64
            service.beta.admit_space(guest_only)
            check(len({u['id'] for u in users} | {u['guest_id'] for u in users}) == 4,
                  'two-distinct-logical-and-canonical-pairs')
            original_connect, original_dns = socket.socket.connect, socket.getaddrinfo

            def connect(sock, address):
                if address != ('127.0.0.1', port):
                    report['foreign_network'].append('python')
                    raise AssertionError('Foreign network forbidden')
                return original_connect(sock, address)

            def dns(host, *args, **kwargs):
                if host != '127.0.0.1':
                    report['foreign_network'].append('dns')
                    raise AssertionError('Foreign DNS forbidden')
                return original_dns(host, *args, **kwargs)

            with ExitStack() as stack:
                stack.enter_context(patch('socket.socket.connect', connect))
                stack.enter_context(patch('socket.getaddrinfo', dns))
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    try:
                        context = browser.new_context(viewport={'width': 1440, 'height': 1000}, service_workers='block')

                        def local_only(route):
                            if route.request.url.startswith(origin + '/'):
                                route.continue_()
                            else:
                                report['foreign_network'].append('browser')
                                route.abort()

                        context.route('**/*', local_only)
                        page = context.new_page()
                        page.on('pageerror', lambda error: report['page_errors'].append(type(error).__name__))
                        page.goto(origin + '/dashboard')
                        expect(page.locator('#login-panel')).to_be_visible()
                        page.locator('#username').fill(config.username)
                        page.locator('#password').fill(password)
                        page.get_by_role('button', name='Войти', exact=True).click()
                        expect(page.locator('#accounts tr')).to_have_count(3)
                        principal = context.request.get(origin + '/api/dashboard/session').json()

                        def row(user):
                            return page.locator('#accounts tr').filter(
                                has=page.locator('.canonical-guest-id').filter(has_text=user['guest_id']))

                        def assert_pair(parent, user):
                            expect(parent.get_by_text('ID аккаунта', exact=True)).to_be_visible()
                            expect(parent.get_by_text('ID пространства', exact=True)).to_be_visible()
                            expect(parent.locator('.registered-account-id')).to_have_text(user['id'])
                            expect(parent.locator('.canonical-guest-id')).to_have_text(user['guest_id'])
                            check(len(parent.locator('.registered-account-id').inner_text()) == 64,
                                  'full-logical-id-in-dom')

                        def screenshot(name, width, dialog=False):
                            page.set_viewport_size({'width': width, 'height': 1000 if width == 1440 else 844})
                            size = page.locator('html').evaluate('(e)=>({scroll:e.scrollWidth,width:innerWidth})')
                            check(size['scroll'] <= size['width'], name + '-no-root-overflow')
                            file = output / f'{name}-{width}.png'
                            page.screenshot(path=str(file), full_page=not dialog)
                            report['screenshots'].append(str(file))

                        for width in (1440, 390):
                            page.set_viewport_size({'width': width, 'height': 1000 if width == 1440 else 844})
                            for user in users:
                                assert_pair(row(user), user)
                            guest = page.locator('#accounts tr').filter(
                                has=page.locator('.canonical-guest-id').filter(has_text=guest_only))
                            expect(guest.locator('.registered-account-id')).to_have_count(0)
                            screenshot('accounts', width)
                            row(users[0]).get_by_role('button', name='Блокировать', exact=True).click()
                            assert_pair(page.locator('#block-target'), users[0])
                            screenshot('confirmation', width, dialog=True)
                            page.locator('#cancel-block').click()

                        row(users[1]).get_by_role('button', name='Блокировать', exact=True).click()
                        assert_pair(page.locator('#block-target'), users[1])
                        expect(page.locator('#block-target')).not_to_contain_text(users[0]['id'])
                        reason = 'Synthetic UI identifier check'
                        page.locator('#reason').fill(reason)
                        with page.expect_request(lambda request: request.method == 'POST' and request.url.endswith('/block')) as sent:
                            page.locator('#confirm-block').click()
                        request = sent.value
                        check(request.url == origin + '/api/dashboard/accounts/' + users[1]['guest_id'] + '/block',
                              'mutation-target-remains-canonical-id')
                        check(request.post_data_json == {'reason': reason}, 'existing-request-body-unchanged')
                        check(request.headers.get('x-csrf-token') == principal['csrf_token'], 'existing-csrf-unchanged')
                        check(request.headers.get('origin') == origin, 'existing-origin-unchanged')
                        expect(page.locator('#block-dialog')).not_to_be_visible()
                        inventory = context.request.get(origin + '/api/dashboard/accounts').json()['accounts']
                        check({a['id']: a['blocked'] for a in inventory} == {
                            users[0]['guest_id']: False, users[1]['guest_id']: True, guest_only: False},
                            'only-selected-space-blocked')
                        page.locator('#logout').click()
                        expect(page.locator('#login-panel')).to_be_visible()
                        check(not report['foreign_network'] and not report['page_errors'] and not report['work_calls'],
                              'no-foreign-provider-model-or-page-errors')
                        report['browser'] = browser.version
                        context.close()
                    finally:
                        browser.close()
        finally:
            service.shutdown()
            service.server_close()
            service.beta.tenants.close()
            thread.join(5)
            check(not thread.is_alive(), 'server-closed')
    report['synthetic_state_removed'] = not Path(state).exists()
    report['source_stable'] = hashes == {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in files}
    check(report['synthetic_state_removed'] and report['source_stable'], 'state-cleaned-source-stable')
    report['verdict'] = 'PASS'
    (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'verdict': 'PASS', 'checks': len(report['checks']), 'report': str(output / 'report.json')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
