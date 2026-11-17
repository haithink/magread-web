"""Earlier issue filtering in the actual desktop and touch browser UI."""
import os
from pathlib import Path
import threading
from wsgiref.simple_server import make_server

from playwright.sync_api import sync_playwright, expect
from run_article_duplicate_tests import publication, WebTests, config, store, duplicates


def main():
    WebTests.setUpClass()
    older, _ = publication('old', 'Wired_2026.08.02.epub', ['An older story', "You've Never Heard"])
    current, _ = publication('new', 'Wired_2026.11.02.epub', ['You’ve Never—Heard!', 'A new story', 'Another new story'])
    all_duplicates, _ = publication('all_dup', 'Wired_2026.09.02.epub', ['An older story', "You've Never Heard"])
    store.save_library([current, older, all_duplicates])
    duplicates._cached_titles.cache_clear()
    errors, api_calls = [], []
    server = make_server('127.0.0.1', 0, WebTests.app)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    base = f'http://127.0.0.1:{server.server_port}'
    try:
        with sync_playwright() as playwright:
            chrome = os.environ.get('KANDU_TEST_CHROME')
            browser = playwright.chromium.launch(headless=True, **({'executable_path':chrome} if chrome else {}))
            for mobile in [False, True]:
                store._atomic_json_write(config.app_dir()/'article_states/new.json', {'0':'read','1':'unread','2':'uninterested'})
                context = browser.new_context(viewport={'width':390 if mobile else 1440, 'height':844 if mobile else 1000},
                    is_mobile=mobile, has_touch=mobile)
                page = context.new_page(); page.on('pageerror', lambda error:errors.append(str(error)))
                page.on('request', lambda req:api_calls.append(req.url) if '/api/' in req.url else None)
                page.goto(base+'/#/book/new')
                page.locator('#password').fill(WebTests.authcode); page.locator('#login-form button').click()
                expect(page.locator('.article-card')).to_have_count(2)
                expect(page.locator('.article-card h2')).to_have_text(['A new story','Another new story'])
                expect(page.locator('#issue-article-count')).to_have_text('共 3 篇 · 已隐藏 1 篇重复文章')
                expect(page.get_by_role('button', name='全部 2', exact=True)).to_be_visible()
                expect(page.get_by_role('button', name='已读 0', exact=True)).to_be_visible()
                check = page.get_by_role('checkbox', name='显示重复文章（1）', exact=True)
                expect(check).not_to_be_checked()
                before = len(api_calls)
                check.tap() if mobile else check.check()
                expect(page.locator('.article-card')).to_have_count(3)
                expect(page.get_by_role('button', name='全部 3', exact=True)).to_be_visible()
                expect(page.get_by_role('button', name='已读 1', exact=True)).to_be_visible()
                expect(page.locator('.duplicate-source')).to_have_attribute('href','#/read/old/1')
                # Filtering is entirely local. Existing cards survive toggle/search/tab changes.
                page.locator('.article-card').nth(1).evaluate('(node)=>node.dataset.retained="yes"')
                check.tap() if mobile else check.uncheck()
                expect(page.locator('.article-card').first).to_have_attribute('data-retained','yes')
                search = page.get_by_role('searchbox', name='搜索文章')
                search.fill('Never')
                expect(page.locator('.article-card')).to_have_count(0)
                check.tap() if mobile else check.check()
                expect(page.locator('.article-card')).to_have_count(1)
                expect(page.locator('.article-card h2')).to_have_text('You’ve Never—Heard!')
                search.fill('')
                page.get_by_role('button', name='未读 1', exact=True).click()
                expect(page.locator('.article-card h2')).to_have_text('A new story')
                self_count = len(api_calls)
                assert self_count == before, (before,self_count,api_calls)
                page.locator('.card-bottom select').select_option('read')
                expect(page.locator('.article-card')).to_have_count(0)
                page.get_by_role('button', name='已读 2', exact=True).click()
                expect(page.locator('.article-card')).to_have_count(2)
                # Original article index is retained; compacting the directory must not misroute readers.
                page.locator('.card-bottom a[href="#/read/new/1"]').click()
                expect(page.locator('.reader-head h1')).to_have_text('A new story')
                page.goto(base+'/#/book/new')
                expect(page.locator('.article-card')).to_have_count(2)
                expect(page.locator('#show-duplicate-articles')).not_to_be_checked()
                page.locator('#show-duplicate-articles').check()
                page.locator('.duplicate-source').click()
                expect(page.locator('.reader-head h1')).to_have_text("You've Never Heard")
                page.goto(base+'/#/book/all_dup')
                expect(page.locator('.article-card')).to_have_count(0)
                expect(page.get_by_role('heading', name='本期文章均已在更早刊物出现')).to_be_visible()
                page.get_by_role('checkbox', name='显示重复文章（2）', exact=True).check()
                expect(page.locator('.article-card')).to_have_count(2)
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                if folder := os.environ.get('KANDU_TEST_ARTIFACTS'):
                    target = Path(folder); target.mkdir(parents=True, exist_ok=True)
                    page.screenshot(path=str(target/('duplicate-phone.png' if mobile else 'duplicate-desktop.png')))
                context.close()
            browser.close()
        assert not errors, errors
        print('DUPLICATE BROWSER PASS: desktop + touch phone, default hiding, counts/search/tabs, instant local toggle, preserved cards/indices, earlier-source links and all-duplicate empty state')
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=5)
        WebTests.tearDownClass()


if __name__ == '__main__':
    main()
