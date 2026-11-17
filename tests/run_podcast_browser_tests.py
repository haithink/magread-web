"""Desktop/mobile Podcast import UI using temporary data, fake website and LLM."""
import os
from pathlib import Path
import threading
import time
from unittest.mock import patch
from wsgiref.simple_server import make_server

from playwright.sync_api import sync_playwright, expect
from run_podcast_tests import WebTests, config, store, llm, podcasts, catalog_html, transcript_html, fake_model


def main():
    WebTests.setUpClass()
    calls = []
    def fetch(spec, url, control):
        control.check()
        calls.append(url)
        time.sleep(.25)
        if url == spec['url']:
            return catalog_html(3)
        return transcript_html(int(url.split('guest-')[1].split('-')[0]))
    def model(*args, **kwargs):
        time.sleep(.3)
        return fake_model(*args, **kwargs)
    server = make_server('127.0.0.1', 0, WebTests.app)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    base = f'http://127.0.0.1:{server.server_port}'
    errors = []
    try:
        with patch.object(podcasts, 'fetch_html', side_effect=fetch), patch.object(llm, 'conversation', side_effect=model), \
                sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, executable_path=os.environ['KANDU_TEST_CHROME'])
            for mobile in [False, True]:
                context = browser.new_context(viewport={'width':390 if mobile else 1440,'height':844 if mobile else 1000},
                    is_mobile=mobile, has_touch=mobile)
                page = context.new_page(); page.on('pageerror', lambda error:errors.append(str(error)))
                page.goto(base+'/#/library')
                page.locator('#password').fill(WebTests.authcode); page.locator('#login-form button').click()
                page.get_by_role('button', name='刊物来源', exact=True).click()
                expect(page.locator('#sheet-title')).to_have_text('刊物来源 · Podcast')
                expect(page.locator('.podcast-source h3')).to_have_text('Lex Fridman Podcast')
                page.get_by_role('button', name='导入 / 更新最近 50 期', exact=True).click()
                expect(page.locator('#jobbar')).to_be_visible()
                page.get_by_role('button', name='关闭', exact=True).click()
                page.get_by_role('button', name='刊物来源', exact=True).click()
                expect(page.locator('.podcast-status')).not_to_have_text('尚未导入')
                expect(page.get_by_role('button', name='导入 / 更新最近 50 期', exact=True)).to_be_enabled(timeout=20000)
                expect(page.locator('.podcast-status')).to_contain_text('已完成')
                page.get_by_role('button', name='关闭', exact=True).click()
                expect(page.locator('.book-card')).to_have_count(4)
                category = page.get_by_role('combobox', name='按刊物筛选')
                category.select_option('Lex Fridman Podcast')
                expect(page.locator('.book-card')).to_have_count(3)
                expect(page.locator('.cover-link img')).to_have_count(3)
                expect(page.locator('.cover-link img').first).to_have_attribute('loading','lazy')
                expect(page.locator('.book-card .count')).to_have_text(['1 篇文章 · 1 篇已有摘要']*3)
                expect(page.locator('.book-card h3')).to_have_text(['#3 · A conversation','#2 · A conversation','#1 · A conversation'])
                search = page.get_by_role('searchbox', name='搜索刊物')
                search.fill('#2')
                expect(page.locator('.book-card')).to_have_count(1)
                expect(page.locator('.cover-link img')).to_have_js_property('naturalWidth',480)
                page.locator('.cover-link').click()
                expect(page.locator('.digest-head h1')).to_have_text('#2 · A conversation')
                expect(page.get_by_role('link', name='官方 Transcript ↗')).to_have_attribute('href', 'https://lexfridman.com/guest-2-transcript')
                expect(page.locator('.article-card p')).to_contain_text('嘉宾讨论科学')
                page.locator('.card-bottom a').click()
                expect(page.locator('.reader-head h1')).to_have_text('#2 · A conversation')
                expect(page.locator('#para-0 p')).to_contain_text('Guest (00:00:00)')
                expect(page.locator('#vocab-button')).to_have_text('生成生词表')
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                if folder := os.environ.get('KANDU_TEST_ARTIFACTS'):
                    target = Path(folder); target.mkdir(parents=True, exist_ok=True)
                    page.screenshot(path=str(target/('podcast-phone.png' if mobile else 'podcast-desktop.png')))
                context.close()
            browser.close()
        assert not errors, errors
        print('PODCAST BROWSER PASS: desktop + touch phone; background progress/reopen, category, sorting/search, summary/source/reader, no automatic vocabulary')
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=5)
        WebTests.tearDownClass()


if __name__ == '__main__':
    main()
