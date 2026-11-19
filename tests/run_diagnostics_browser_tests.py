"""Real mobile browser logs for success, blocked storage, errors and close taps."""
import json
import os
import threading
from unittest.mock import patch
from wsgiref.simple_server import make_server

from playwright.sync_api import sync_playwright, expect
from run_web_tests import WebTests, store, gen, core_dict
from run_mobile_reading_browser_tests import BrowserServer, tap_word, PARAGRAPHS


def main():
    WebTests.setUpClass()
    store._atomic_json_write(store._cache_dir('sample') / 'content.json', dict(bookSize=WebTests.meta['size'],
        format='epub', articles=[dict(index=0, title='Diagnostic test', paragraphs=PARAGRAPHS)]))
    server = make_server('127.0.0.1', 0, WebTests.app, server_class=BrowserServer)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    gate = threading.Event()
    def analyze(*args, **kwargs):
        gate.wait(15)
        return dict(translation='TEST_TRANSLATION', grammarComponents=[dict(text='A reader', label='主语')])
    try:
        with (sync_playwright() as playwright, patch.object(gen, 'analyze', side_effect=analyze),
              patch.object(core_dict, 'ensure_db'), patch.object(core_dict, 'lookup_many', return_value={})):
            engine = os.environ.get('KANDU_TEST_ENGINE', 'chromium')
            chrome = os.environ.get('KANDU_TEST_CHROME')
            browser = getattr(playwright, engine).launch(headless=True,
                **({'executable_path': chrome} if engine == 'chromium' and chrome else {}))
            context = browser.new_context(viewport=dict(width=393, height=852), device_scale_factor=3,
                is_mobile=True, has_touch=True)
            page = context.new_page()
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(f'http://127.0.0.1:{server.server_port}/#/read/sample/0')
            page.locator('#password').fill(WebTests.authcode)
            page.locator('#login-form button').click()
            page.wait_for_function('() => state.reader?.ready&&state.reader.vocab')
            path = WebTests.app.extensions['diagnostics'].path
            def rows():
                return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
            def wait_event(event):
                for _ in range(100):
                    if any(row.get('event') == event for row in rows()):
                        return
                    page.wait_for_timeout(100)
                raise AssertionError((event, rows()))
            def start():
                tap_word(page)
                page.get_by_role('button', name='句子讲解', exact=True).tap()
                expect(page.locator('#sheet-wait')).to_be_visible()
            # A successful real (mock-model) server job can be joined to browser events.
            start()
            wait_event('analysis.accepted')
            page.get_by_role('button', name='查看', exact=True).tap()
            expect(page.locator('#sheet')).to_be_visible()
            page.locator('#sheet-background').tap()
            expect(page.locator('#sheet-wait')).to_be_visible()
            gate.set()
            expect(page.locator('#sheet-content')).to_contain_text('TEST_TRANSLATION', timeout=15000)
            wait_event('analysis.rendered')
            page.locator('#sheet-close').tap()
            wait_event('sheet.closed')
            values = rows()
            job = next(row['job'] for row in values if row['event'] == 'analysis.accepted')
            assert any(row.get('job') == job and row['event'] == 'job.done' for row in values)
            assert any(row['event'] == 'sheet.pointer' and row['control'] == 'sheet-close' for row in values)
            assert any(row['event'] == 'sheet.click' and row['control'] == 'sheet-close' for row in values)
            assert not any('TEST_TRANSLATION' in json.dumps(row) or 'candid' in json.dumps(row) for row in values)
            # Simulate a hung local cache read. The last completed phase identifies it;
            # closing the card remains observable and no model request is submitted.
            page.evaluate('''() => {
                window.originalCacheGet=ReadingCache.get;
                ReadingCache.get=key=>key.startsWith('analysis:')?new Promise(()=>{}):originalCacheGet(key);
            }''')
            start()
            page.wait_for_timeout(500)
            page.get_by_role('button', name='关闭等待提示').tap()
            expect(page.locator('#sheet-wait')).to_have_count(0)
            page.wait_for_timeout(500)
            starts = [row for row in rows() if row['event'] == 'analysis.start']
            latest = [row for row in rows() if row.get('trace') == starts[-1]['trace']]
            assert any(row['event'] == 'analysis.cache-read' for row in latest)
            assert not any(row['event'] == 'analysis.submit' for row in latest)
            assert any(row['event'] == 'sheet.click' and row['control'] == 'wait-close' for row in latest)
            # Raw error strings/stacks must not be transmitted, including credentials.
            page.evaluate("window.dispatchEvent(new ErrorEvent('error',{error:new TypeError('PRIVATE_ERROR_TOKEN')}))")
            wait_event('page.error')
            assert 'PRIVATE_ERROR_TOKEN' not in path.read_text()
            assert not errors, errors
            context.close()
            browser.close()
            print(f'DIAGNOSTICS BROWSER PASS: {engine}, real task/log correlation, modal/close touch events, hung cache and error privacy.')
    finally:
        gate.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        WebTests.tearDownClass()


if __name__ == '__main__':
    main()
