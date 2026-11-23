"""Actual model labels survive streaming, fallback, dismissal and cached vocabulary."""
import os
import threading
from unittest.mock import patch
from wsgiref.simple_server import make_server

from playwright.sync_api import sync_playwright, expect
from run_web_tests import WebTests, config, store, gen, core_dict
from run_mobile_reading_browser_tests import BrowserServer


def main():
    expect.set_options(timeout=15000)
    WebTests.setUpClass()
    settings = config.load_settings()
    preferred = dict(settings['llms'][0], name='selected-deepseek-configuration', model='deepseek-v4-flash')
    backup = dict(preferred, name='backup-deepseek-configuration', model='deepseek-flash')
    config.save_settings(dict(settings, llms=[preferred, backup], defaultLlm=preferred['name']))
    paragraphs = ['A candid reader follows the argument. ' * 12 for _ in range(16)]
    store._atomic_json_write(store._cache_dir('sample')/'content.json', dict(bookSize=WebTests.meta['size'],
        format='epub', articles=[dict(index=0, title='Model progress', paragraphs=paragraphs)]))
    server = make_server('127.0.0.1', 0, WebTests.app, server_class=BrowserServer)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    primary_done, backup_done = threading.Event(), threading.Event()

    def generate(meta, article, level, cfg, **callbacks):
        assert cfg['name'] == preferred['name']
        callbacks['on_model']('chunk', cfg, True)
        callbacks['on_chars'](100)
        if not primary_done.wait(20):
            raise AssertionError('Primary test stage timed out')
        callbacks['on_model']('chunk', cfg, False)
        callbacks['on_model']('chunk', backup, True)
        callbacks['on_chars'](250)
        if not backup_done.wait(20):
            raise AssertionError('Backup test stage timed out')
        callbacks['on_model']('chunk', backup, False)
        value = dict(articleIndex=0, level=level, titleEntries=[], idioms=[], processedParagraphs=[-1, *range(16)],
            generationStats=dict(modelsUsed=[dict(name=m['name'], model=m['model']) for m in [cfg, backup]]),
            paragraphs=[dict(index=i, entries=[dict(word='candid', sense='坦率的')]) for i in range(16)])
        store.save_vocab(meta['id'], value)
        return value

    try:
        with (sync_playwright() as playwright, patch.object(gen, 'generate_vocab', side_effect=generate),
              patch.object(core_dict, 'ensure_db'), patch.object(core_dict, 'lookup_many', return_value={})):
            engine = os.environ.get('KANDU_TEST_ENGINE', 'chromium')
            browser = getattr(playwright, engine).launch(headless=True, **(
                {'executable_path':os.environ['KANDU_TEST_CHROME']} if engine=='chromium' else {}))
            for mobile in [False, True]:
                primary_done.clear(); backup_done.clear()
                store.save_vocab('sample', dict(articleIndex=0, level='cet4', titleEntries=[], paragraphs=[],
                    idioms=[], processedParagraphs=[]))
                context = browser.new_context(viewport=dict(width=393 if mobile else 1440, height=852),
                    is_mobile=mobile, has_touch=mobile, service_workers='block')
                page = context.new_page()
                errors = []
                page.on('pageerror', lambda error:(errors.append(str(error)), print('PAGE ERROR:', str(error), flush=True)))
                page.goto(f'http://127.0.0.1:{server.server_port}/#/read/sample/0')
                page.locator('#password').fill(WebTests.authcode)
                page.locator('#login-form button').click()
                page.wait_for_function('() => state.reader?.ready&&state.reader.vocab', polling=100)
                page.locator('#vocab-button').click()
                status = page.locator('.sheet-wait-status')
                expect(status).to_contain_text('selected-deepseek-configuration（deepseek-v4-flash）')
                expect(status).to_contain_text('100')
                primary_done.set()
                expect(status).to_contain_text('backup-deepseek-configuration（deepseek-flash）')
                expect(status).to_contain_text('备用切换')
                expect(status).to_contain_text('250')
                page.wait_for_timeout(100)
                metrics = page.evaluate('''() => {
                    const card=document.querySelector('#sheet-wait'), text=card.querySelector('.sheet-wait-status');
                    const rect=card.getBoundingClientRect();return {
                        top:rect.top,bottom:rect.bottom,left:rect.left,right:rect.right,
                        width:innerWidth,height:innerHeight,scroll:text.scrollHeight,client:text.clientHeight,
                        whiteSpace:getComputedStyle(text).whiteSpace};
                }''')
                assert metrics['whiteSpace']=='normal' and metrics['scroll']<=metrics['client']+1, metrics
                assert metrics['top']>=0 and metrics['bottom']<=metrics['height'], metrics
                assert metrics['left']>=0 and metrics['right']<=metrics['width'], metrics
                page.get_by_role('button', name='关闭等待提示').click()
                expect(page.locator('#jobbar')).to_be_visible()
                expect(page.locator('#job-status')).to_contain_text('backup-deepseek-configuration')
                expect(page.locator('#job-status')).to_contain_text('备用切换')
                backup_done.set()
                page.wait_for_function('() => !state.activeJobs.size&&state.reader.vocab.generated', polling=100)
                expect(page.locator('#sheet')).not_to_be_visible()
                page.locator('#vocab-button').click()
                expect(page.locator('.generation-models')).to_contain_text('selected-deepseek-configuration')
                expect(page.locator('.generation-models')).to_contain_text('backup-deepseek-configuration')
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                assert not errors, errors
                print(f'MODEL PROGRESS PASS: {engine}, mobile={mobile}, selected model, fallback, full labels, dismissal, saved provenance.', flush=True)
                context.close()
            browser.close()
    finally:
        primary_done.set(); backup_done.set()
        server.shutdown(); server.server_close(); thread.join(timeout=5)
        WebTests.tearDownClass()


if __name__ == '__main__':main()
