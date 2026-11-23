"""Incremental sentence explanations and safe review on desktop/iPhone views."""
import os
from pathlib import Path
import threading
from unittest.mock import patch
from wsgiref.simple_server import make_server

from playwright.sync_api import sync_playwright, expect
from flask import request, jsonify
from run_web_tests import WebTests, store, core_dict
from run_mobile_reading_browser_tests import BrowserServer
from run_vocab_hover_browser_tests import word_point


SENTENCE = 'It certainly won’t paper over the uncertainty.'
PARAGRAPHS = [f'Note {i}. 🌙 The answer was candid. '+SENTENCE+' Readers appreciate clear explanations. '*5 for i in range(16)]
RESULT = dict(context='OLD_MEANING', translation='OLD_TRANSLATION',
    grammarComponents=[dict(text='It', label='主语'), dict(text='certainly won’t paper over', label='谓语')],
    grammarPoints=[dict(title='短语动词', text='paper over 指掩盖问题。')], vocab=[])


def explain(page, mobile, paragraph=4):
    point = word_point(page, 'paper', paragraph)
    page.evaluate("y=>scrollBy({top:y-innerHeight/2,behavior:'instant'})", point['y'])
    point = word_point(page, 'paper', paragraph, scroll=False)
    if mobile:
        page.touchscreen.tap(**point)
        page.wait_for_timeout(80)
        page.touchscreen.tap(**point)
    else:
        page.mouse.dblclick(**point)


def main():
    expect.set_options(timeout=15000)
    WebTests.setUpClass()
    jobs = {}
    @WebTests.app.before_request
    def synthetic_job():
        if request.path.startswith('/api/jobs/sentence-'):
            return jsonify(jobs[request.path.rsplit('/', 1)[-1]])
    store._atomic_json_write(store._cache_dir('sample')/'content.json', dict(bookSize=WebTests.meta['size'],
        format='epub', articles=[dict(index=0, title='Context matters', paragraphs=PARAGRAPHS)]))
    server = make_server('127.0.0.1', 0, WebTests.app, server_class=BrowserServer)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with (sync_playwright() as playwright, patch.object(core_dict, 'ensure_db'),
              patch.object(core_dict, 'lookup_many', return_value={})):
            engine = os.environ.get('KANDU_TEST_ENGINE', 'chromium')
            browser = getattr(playwright, engine).launch(headless=True, **(
                {'executable_path':os.environ['KANDU_TEST_CHROME']} if engine == 'chromium' else {}))
            for mobile in [False, True]:
                context = browser.new_context(viewport=dict(width=393 if mobile else 1440, height=852),
                    is_mobile=mobile, has_touch=mobile, service_workers='block')
                page = context.new_page()
                page.bring_to_front()
                page.set_default_timeout(15000)
                calls, errors = [], []
                jobs.clear()
                page.on('pageerror', lambda error:(errors.append(str(error)), print('PAGE ERROR:', str(error), flush=True)))
                def start(route):
                    calls.append(route.request.post_data_json)
                    job = f'sentence-{len(calls)}'
                    jobs[job] = dict(state='running', status='等待模型响应', partial={})
                    route.fulfill(status=202, json=dict(jobId=job))
                page.route('**/api/books/sample/articles/0/analyze', start)
                page.goto(f'http://127.0.0.1:{server.server_port}/#/read/sample/0')
                page.locator('#password').fill(WebTests.authcode)
                page.locator('#login-form button').click()
                page.wait_for_function('() => state.reader?.ready&&state.reader.vocab', polling=100)
                explain(page, mobile)
                expect(page.locator('#sheet-wait')).to_be_visible()
                expect(page.locator('.sheet-wait-status')).to_have_text('等待模型响应')
                assert len(calls) == 1, calls
                assert calls[0]['sentence'] == SENTENCE and calls[0]['paragraph'] == 4, calls
                assert calls[0]['start'] == PARAGRAPHS[4].index(SENTENCE), calls
                assert calls[0]['deep'] is False and calls[0]['force'] is False
                # Meaning is available before grammar and before modal completion.
                jobs['sentence-1']['partial'] = dict(context='EARLY_MEANING')
                expect(page.locator('.sheet-wait-preview')).to_have_text('EARLY_MEANING')
                expect(page.locator('#sheet')).not_to_be_visible()
                page.locator('#sheet-wait').get_by_role('button', name='查看', exact=True).click()
                expect(page.locator('#sheet-content')).to_contain_text('EARLY_MEANING')
                expect(page.locator('.analysis-component')).to_have_count(0)
                expect(page.get_by_role('button', name='深度复核', exact=True)).to_be_disabled()
                jobs['sentence-1']['partial'] = dict(context='EARLY_MEANING', translation='EARLY_TRANSLATION')
                expect(page.locator('#sheet-content')).to_contain_text('EARLY_TRANSLATION')
                headings = page.locator('#sheet-content .analysis-block h3').all_text_contents()
                assert headings == ['语境与含义', '参考译文'], headings
                jobs['sentence-1'].update(state='done', result=RESULT)
                expect(page.get_by_role('button', name='深度复核', exact=True)).to_be_enabled()
                expect(page.locator('#sheet-wait')).to_have_count(0)
                expect(page.locator('#sheet-content')).to_contain_text('OLD_TRANSLATION')
                expect(page.locator('.analysis-component')).to_have_count(2)
                # Explicit review keeps the prior answer until full success.
                page.get_by_role('button', name='深度复核', exact=True).click()
                expect(page.locator('#sheet-wait')).to_be_visible()
                expect(page.locator('.sheet-wait-status')).to_have_text('等待模型响应')
                assert len(calls) == 2 and calls[-1]['deep'] is True and calls[-1]['force'] is True, calls
                assert calls[-1]['start'] == calls[0]['start']
                jobs['sentence-2']['partial'] = dict(context='NEW_PREVIEW')
                expect(page.locator('.sheet-wait-preview')).to_have_text('NEW_PREVIEW')
                page.locator('#sheet-wait').get_by_role('button', name='查看', exact=True).click()
                expect(page.locator('#sheet-content')).to_contain_text('OLD_TRANSLATION')
                expect(page.locator('.analysis-preview')).to_contain_text('NEW_PREVIEW')
                # An adaptive stream retry clears provisional fields only.
                jobs['sentence-2']['partial'] = {}
                expect(page.locator('.analysis-preview')).not_to_be_visible()
                expect(page.locator('#sheet-content')).to_contain_text('OLD_TRANSLATION')
                jobs['sentence-2'].update(state='error', error='模拟复核失败')
                expect(page.locator('.analysis-status.error')).to_contain_text('已有讲解已保留')
                expect(page.locator('#sheet-content')).to_contain_text('OLD_TRANSLATION')
                expect(page.get_by_role('button', name='重新讲解', exact=True)).to_be_enabled()
                page.locator('#sheet-close').click()
                explain(page, mobile)
                expect(page.locator('#sheet')).to_be_visible()
                expect(page.locator('#sheet-content')).to_contain_text('OLD_TRANSLATION')
                assert len(calls) == 2, 'Failed review must not overwrite the local cache'
                # Regeneration bypasses both saved answers and updates on success.
                page.get_by_role('button', name='重新讲解', exact=True).click()
                expect(page.locator('#sheet-wait')).to_be_visible()
                expect(page.locator('.sheet-wait-status')).to_have_text('等待模型响应')
                assert calls[-1]['force'] is True and calls[-1]['deep'] is False
                jobs['sentence-3'].update(state='done', result=dict(RESULT, translation='SUCCESSFUL_REPLACEMENT'))
                expect(page.locator('#sheet-content')).to_contain_text('SUCCESSFUL_REPLACEMENT')
                expect(page.get_by_role('button', name='重新讲解', exact=True)).to_be_enabled()
                if directory := os.environ.get('KANDU_TEST_ARTIFACTS'):
                    folder = Path(directory);folder.mkdir(parents=True, exist_ok=True)
                    page.screenshot(path=str(folder/f'sentence-review-{engine}-{mobile}.png'))
                page.locator('#sheet-close').click()
                explain(page, mobile)
                expect(page.locator('#sheet')).to_be_visible()
                expect(page.locator('#sheet-content')).to_contain_text('SUCCESSFUL_REPLACEMENT')
                assert len(calls) == 3
                page.locator('#sheet-close').click()
                # Identical sentences in another paragraph cannot reuse that answer.
                explain(page, mobile, 5)
                expect(page.locator('#sheet-wait')).to_be_visible()
                expect(page.locator('.sheet-wait-status')).to_have_text('等待模型响应')
                assert len(calls) == 4 and calls[-1]['paragraph'] == 5, calls
                jobs['sentence-4']['partial'] = dict(context='OTHER_CONTEXT')
                expect(page.locator('.sheet-wait-preview')).to_have_text('OTHER_CONTEXT')
                page.get_by_role('button', name='关闭等待提示').click()
                jobs['sentence-4'].update(state='done', result=RESULT)
                page.wait_for_function('() => !state.activeJobs.size', polling=100)
                expect(page.locator('#sheet')).not_to_be_visible()
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                assert not errors, errors
                print(f'SENTENCE BROWSER PASS: {engine}, mobile={mobile}, early meaning/translation, Unicode offset, safe deep review, cache reuse, regeneration, context separation and dismiss.', flush=True)
                context.close()
            browser.close()
    finally:
        server.shutdown();server.server_close();thread.join(timeout=5)
        WebTests.tearDownClass()


if __name__ == '__main__':main()
