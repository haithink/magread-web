"""Hidden Gems import and provenance on desktop and iPhone-sized screens, fake ASR/LLM."""
import os
import threading
import time
from unittest.mock import patch
from wsgiref.simple_server import make_server

from playwright.sync_api import sync_playwright, expect
from PIL import Image
from run_hidden_gems_tests import WebTests, config, store, llm, podcasts, hidden_gems, catalog_html, fake_transcript, fake_model
from webapp import podcast_covers


def main():
    WebTests.setUpClass()
    server = make_server('127.0.0.1', 0, WebTests.app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    errors, calls = [], []
    def speech(*args, **kwargs):
        calls.append(args[0]['id'])
        value = fake_transcript(*args, **kwargs)
        time.sleep(1)
        return value
    try:
        with patch.object(podcasts, 'fetch_html', return_value=catalog_html()), \
                patch.object(hidden_gems, 'transcript', side_effect=speech), \
                patch.object(llm, 'conversation', side_effect=fake_model), \
                patch.object(podcast_covers, '_official_image', side_effect=lambda *a:Image.new('RGB',(640,640),'navy')), \
                sync_playwright() as playwright:
            engine = os.environ.get('KANDU_TEST_ENGINE', 'chromium')
            browser = (playwright.webkit.launch(headless=True) if engine == 'webkit' else
                playwright.chromium.launch(headless=True, executable_path=os.environ['KANDU_TEST_CHROME']))
            for mobile in (False, True):
                context = browser.new_context(viewport={'width':393 if mobile else 1440, 'height':852 if mobile else 1000},
                    is_mobile=mobile, has_touch=mobile)
                page = context.new_page()
                page.set_default_timeout(30000)
                expect.set_options(timeout=30000)
                page.on('pageerror', lambda error:errors.append(str(error)))
                page.goto(f'http://127.0.0.1:{server.server_port}/#/library')
                page.locator('#password').fill(WebTests.authcode)
                page.locator('#login-form button').click()
                page.get_by_role('button', name='刊物来源', exact=True).click()
                source = page.locator('.podcast-source').filter(has=page.get_by_role('heading',name='Motley Fool Hidden Gems Investing',exact=True))
                expect(source).to_be_visible()
                expect(source.get_by_label('Motley Fool Hidden Gems Investing 导入期数')).to_have_value('1')
                expect(source).to_contain_text('机器转写')
                if not mobile:
                    source.get_by_role('button',name='导入 / 更新所选期数',exact=True).click()
                    expect(source.locator('progress')).to_be_visible()
                    expect(source.locator('.podcast-status')).to_contain_text('已完成')
                page.get_by_role('button', name='关闭', exact=True).click()
                # A new browser session loads the already imported series.
                if mobile:
                    page.reload()
                card = page.locator('.book-card').filter(has=page.locator('a[href="#/book/podcast-series-hidden-gems"]'))
                expect(card).to_have_count(1)
                expect(card).to_contain_text('1 篇文章')
                card.locator('a').first.click()
                expect(page.locator('.digest-head h1')).to_have_text('Motley Fool Hidden Gems Investing')
                expect(page.locator('.podcast-provenance')).to_contain_text('机器转写')
                expect(page.locator('.article-card')).to_have_count(1)
                page.locator('.article-card a').first.click()
                expect(page.get_by_role('link',name='机器转写 · 官方音频 ↗',exact=True)).to_be_visible()
                expect(page.locator('.paragraph p').first).to_contain_text('We discuss')
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                context.close()
            browser.close()
        assert len(calls) == 1, calls
        assert not errors, errors
        print('HIDDEN GEMS BROWSER PASS: desktop + iPhone viewport; source/count/progress, one series, machine provenance, reader')
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=5)
        WebTests.tearDownClass()


if __name__ == '__main__':
    main()
