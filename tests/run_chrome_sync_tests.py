"""Optional real extension + Web integration using an isolated temporary library.

Set KANDU_TEST_EXTENSION to an unpacked magread-chrome folder and
KANDU_TEST_CHROME to an extension-capable Chrome executable.
"""
import os
from pathlib import Path
import tempfile
import threading
from wsgiref.simple_server import make_server

from playwright.sync_api import sync_playwright, expect
from run_web_tests import WebTests, config, store


def main():
    extension = os.environ.get('KANDU_TEST_EXTENSION')
    if not extension:
        print('SKIP: set KANDU_TEST_EXTENSION to test the actual extension against Web')
        return
    WebTests.setUpClass()
    app = WebTests.app

    @app.get('/fixture-article')
    def fixture_article():
        return ('<!doctype html><meta charset="utf-8"><title>A candid view</title>'
                '<article><h1>A candid view</h1><p>' +
                'Reading regularly opens new windows into science and culture. ' * 14 +
                '</p><p>Reading regularly opens new windows into science and culture.</p></article>')

    server = make_server('127.0.0.1', 0, app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f'http://127.0.0.1:{server.server_port}'
    try:
        with tempfile.TemporaryDirectory(prefix='kandu-sync-chrome-') as profile, sync_playwright() as playwright:
            browser = playwright.chromium.launch_persistent_context(profile,
                executable_path=os.environ.get('KANDU_TEST_CHROME'), headless=True,
                ignore_default_args=['--disable-extensions'], viewport={'width':1360, 'height':950},
                args=[f'--disable-extensions-except={extension}', f'--load-extension={extension}'])
            errors = []
            worker = browser.service_workers[0] if browser.service_workers else browser.wait_for_event('serviceworker')
            popup_url = worker.evaluate('chrome.runtime.getURL("popup.html")')
            login = browser.request.post(base + '/api/login', data={'password':WebTests.authcode})
            assert login.status == 200
            csrf = login.json()['csrf']
            headers = {'X-CSRF-Token':csrf}
            pair = browser.request.post(base + '/api/reading-sync', data={}, headers=headers)
            assert pair.status == 200
            page = browser.new_page()
            page.on('pageerror', lambda error:errors.append(str(error)))
            page.goto(popup_url)
            page.locator('#syncServer').fill(base)
            page.locator('#syncToken').fill(pair.json()['token'])
            page.locator('#bSyncSave').click()
            expect(page.locator('#syncStatus')).to_contain_text('待同步 0 篇')
            assert '暂时无法' not in page.locator('#syncStatus').inner_text()
            page.goto(base + '/fixture-article')
            page.locator('#kandu-pill').click()
            page.locator('.kandu-complete-reading').click()
            expect(page.locator('.kandu-complete-status')).to_contain_text('已同步到')
            assert browser.request.post(base + '/api/books/sample/state',
                data={'articleIndex':0, 'state':'read'}, headers=headers).status == 200
            page.goto(base + '/#/reading-stats')
            expect(page.locator('[data-stat="articles"]')).to_have_text('1')
            expect(page.locator('[data-stat="words"]')).to_have_text('135')
            expect(page.locator('.completed-sources a')).to_have_count(2)
            expect(page.locator('.completed-sources a[target="_blank"]')).to_have_attribute('href',base+'/fixture-article')
            assert page.locator('#kandu-pill').count() == 0
            for width in [1360,390]:
                page.set_viewport_size({'width':width, 'height':950 if width>400 else 844})
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                artifacts = os.environ.get('KANDU_TEST_ARTIFACTS')
                if artifacts:
                    target = Path(artifacts)
                    target.mkdir(parents=True, exist_ok=True)
                    page.screenshot(path=str(target/f'reading-stats-{width}.png'), full_page=True)
            assert not errors, errors
            browser.close()
            print('INTEGRATION PASS: actual Chrome popup pairing, authenticated cross-origin upload, Web title dedup, 135 words, 2 sources, desktop/mobile layout; no LLM calls')
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        WebTests.tearDownClass()


if __name__ == '__main__':
    main()
