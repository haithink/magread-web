"""Actual paragraph vocabulary UI: legacy cache, independent phrase pass, desktop and touch."""
import json
import os
import threading
from unittest.mock import patch
from wsgiref.simple_server import make_server

from playwright.sync_api import sync_playwright, expect
from run_phrase_tests import ARTICLE, old_vocab, WebTests, config, store, llm, core_dict


def main():
    WebTests.setUpClass()
    store._atomic_json_write(config.app_dir()/'cache/sample/content.json', dict(
        bookSize=WebTests.meta['size'], format='epub', articles=[ARTICLE]))
    calls = []
    def answer(cfg, system, user, **kwargs):
        calls.append(user)
        assert 'paper over' in user
        assert '### 段落 1' not in user
        return json.dumps(dict(paragraphs=[dict(index=0, entries=[
            ['paper over', 'phr.', '掩盖阿尔忒弥斯计划的不确定性，并未解决', '', False, '']])]))

    server = make_server('127.0.0.1', 0, WebTests.app)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    errors = []
    try:
        with (sync_playwright() as playwright, patch.object(llm, 'chat', side_effect=answer),
              patch.object(core_dict, 'ensure_db'), patch.object(core_dict, 'lookup_many', return_value={}),
              patch.dict(core_dict.STATE, ready=True)):
            chrome = os.environ.get('KANDU_TEST_CHROME')
            browser = playwright.chromium.launch(headless=True, **({'executable_path':chrome} if chrome else {}))
            for mobile in [False, True]:
                store.save_vocab('sample', old_vocab())
                context = browser.new_context(viewport={'width':390 if mobile else 1440, 'height':844 if mobile else 1000},
                    is_mobile=mobile, has_touch=mobile)
                page = context.new_page(); page.on('pageerror', lambda error:errors.append(str(error)))
                page.goto(f'http://127.0.0.1:{server.server_port}/#/read/sample/0')
                page.locator('#password').fill(WebTests.authcode); page.locator('#login-form button').click()
                expect(page.locator('#badge-0')).to_contain_text('2')
                badge = page.locator('#badge-0')
                badge.tap() if mobile else badge.click()
                expect(page.locator('.vocab-word')).to_have_text(['uncertainty', 'paper over'])
                expect(page.locator('.entry-sense').last).to_contain_text('掩盖')
                expect(page.get_by_text('本地短语提示 · 可补充本文含义', exact=True)).to_be_visible()
                count = len(calls)
                button = page.get_by_role('button', name='补充短语解释', exact=True)
                button.tap() if mobile else button.click()
                expect(page.locator('.entry-sense').last).to_have_text('掩盖阿尔忒弥斯计划的不确定性，并未解决')
                assert len(calls) == count+1, calls
                expect(page.locator('.vocab-word')).to_have_text(['uncertainty', 'paper over'])
                expect(page.get_by_text('本地短语提示 · 可补充本文含义', exact=True)).to_have_count(0)
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                page.locator('#sheet-close').click()
                expect(page.locator('#badge-2')).to_contain_text('待生成')
                context.close()
            assert not errors, errors
            browser.close()
        print('PHRASE BROWSER PASS: desktop + touch phone, legacy cache upgrade, paragraph anchoring, AI phrase refinement and preserved words')
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=5)
        WebTests.tearDownClass()


if __name__ == '__main__':
    main()
