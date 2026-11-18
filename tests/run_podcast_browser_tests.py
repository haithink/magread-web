"""Desktop/mobile Podcast import UI using temporary data, fake website and LLM."""
import os
from pathlib import Path
import threading
import time
from unittest.mock import patch
from wsgiref.simple_server import make_server

from playwright.sync_api import sync_playwright, expect
from PIL import Image
from run_podcast_tests import WebTests, config, store, llm, podcasts, catalog_html, transcript_html, fake_model
from webapp import podcast_covers


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
                patch.object(podcast_covers, '_official_image', side_effect=lambda *a:Image.new('RGB',(640,640),'navy')), \
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
                expect(page.locator('.book-card')).to_have_count(2)
                category = page.get_by_role('combobox', name='按刊物筛选')
                category.select_option('Lex Fridman Podcast')
                expect(page.locator('.book-card')).to_have_count(1)
                expect(page.locator('.cover-link img')).to_have_count(1)
                expect(page.locator('.cover-link img').first).to_have_attribute('loading','lazy')
                expect(page.locator('.book-card .count')).to_have_text(f'3 篇文章 · 未读 {2 if mobile else 3} 篇 · 3 篇已有摘要')
                expect(page.locator('.book-card h3')).to_have_text('Lex Fridman Podcast')
                expect(page.locator('.cover-link img')).to_have_js_property('naturalWidth',640)
                page.locator('.cover-link').click()
                expect(page.locator('.digest-head h1')).to_have_text('Lex Fridman Podcast')
                expect(page.get_by_role('link', name='播客官网 ↗')).to_have_attribute('href', 'https://lexfridman.com/podcast/')
                expect(page.locator('.article-card h2')).to_have_text(['#3 · A conversation','#2 · A conversation','#1 · A conversation'])
                expect(page.locator('.card-top').first).to_contain_text('2026-11-10')
                for episode,tags in [(3,'AI 精选，AI 产业'),(2,'AI 精选，AI 编程')]:
                    card=page.locator('.article-card').filter(has=page.get_by_role('heading',name=f'#{episode} · A conversation',exact=True))
                    card.get_by_role('button',name='标签',exact=True).click()
                    page.get_by_role('textbox',name='文章标签',exact=True).fill(tags)
                    page.get_by_role('button',name='保存标签',exact=True).click()
                    expect(page.locator('#sheet')).not_to_be_visible()
                topics=page.get_by_role('group',name='主题标签')
                requests=[]
                page.on('request',lambda req:requests.append(req.url) if '/api/' in req.url else None)
                page.locator('.article-card').first.evaluate('(node)=>node.dataset.retained="yes"')
                topics.get_by_role('button',name='AI 精选 2',exact=True).click()
                expect(page.locator('.article-card')).to_have_count(2)
                expect(page.locator('.article-card').first).to_have_attribute('data-retained','yes')
                expect(page.get_by_role('button',name=f'未读 {1 if mobile else 2}',exact=True)).to_be_visible()
                page.get_by_role('button',name=f'未读 {1 if mobile else 2}',exact=True).click()
                expect(page.locator('.article-card')).to_have_count(1 if mobile else 2)
                page.get_by_role('button',name='全部 2',exact=True).click()
                topics.get_by_role('button',name='AI 编程 1',exact=True).click()
                expect(page.locator('.article-card h2')).to_have_text('#2 · A conversation')
                topics.get_by_role('button',name='全部主题 3',exact=True).click()
                expect(page.locator('.article-card')).to_have_count(3)
                page.get_by_role('button',name='筛选标签：AI 精选',exact=True).first.click()
                expect(page.locator('.article-card')).to_have_count(2)
                assert not requests,requests
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                if folder := os.environ.get('KANDU_TEST_ARTIFACTS'):
                    target=Path(folder);target.mkdir(parents=True,exist_ok=True)
                    page.screenshot(path=str(target/('podcast-tags-phone.png' if mobile else 'podcast-tags-desktop.png')))
                search = page.get_by_role('searchbox', name='搜索文章')
                search.fill('AI 编程')
                expect(page.locator('.article-card h2')).to_have_text('#2 · A conversation')
                search.fill('#2')
                expect(page.locator('.article-card')).to_have_count(1)
                expect(page.locator('.article-card p')).to_contain_text('嘉宾讨论科学')
                page.get_by_role('combobox', name='文章标记').select_option('read')
                expect(page.locator('.state-pill')).to_have_text('已读')
                page.locator('.card-bottom a').click()
                expect(page.locator('.reader-head h1')).to_have_text('#2 · A conversation')
                expect(page.locator('#para-0 p')).to_contain_text('Guest (00:00:00)')
                expect(page.locator('#vocab-button')).to_have_text('生成生词表')
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                if folder := os.environ.get('KANDU_TEST_ARTIFACTS'):
                    target = Path(folder); target.mkdir(parents=True, exist_ok=True)
                    page.screenshot(path=str(target/('podcast-phone.png' if mobile else 'podcast-desktop.png')))
                page.get_by_role('link', name='← 目录', exact=True).click()
                expect(page.locator('.digest-head h1')).to_have_text('Lex Fridman Podcast')
                expect(page.locator('.article-card')).to_have_count(2)
                expect(page.get_by_role('group',name='主题标签').get_by_role('button',name='AI 精选 2',exact=True)).to_have_attribute('aria-pressed','true')
                expect(page.locator('.article-card').nth(1).locator('.state-pill')).to_have_text('已读')
                page.get_by_role('button', name='已读 1', exact=True).click()
                expect(page.locator('.article-card')).to_have_count(1)
                expect(page.locator('.article-card h2')).to_have_text('#2 · A conversation')
                page.get_by_role('link', name='书库', exact=True).click()
                series_card = page.locator('.book-card').filter(has=page.locator('a[href="#/book/podcast-series-lex-fridman"]'))
                expect(series_card.locator('.count')).to_have_text('3 篇文章 · 未读 2 篇 · 3 篇已有摘要')
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                context.close()
            browser.close()
        assert not errors, errors
        print('PODCAST BROWSER PASS: desktop + touch phone; background progress/reopen, category, sorting/search, summary/source/reader, no automatic vocabulary')
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=5)
        WebTests.tearDownClass()


if __name__ == '__main__':
    main()
