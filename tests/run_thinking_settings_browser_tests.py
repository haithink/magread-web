"""Native model settings round-trip on desktop and iPhone layouts; no real models."""
import os
import json
import threading
import logging
from werkzeug.serving import make_server
from playwright.sync_api import sync_playwright, expect

from run_web_tests import WebTests, config


def main():
    logging.getLogger('werkzeug').setLevel(logging.ERROR)
    expect.set_options(timeout=15000)
    WebTests.setUpClass()
    server = make_server('127.0.0.1', 0, WebTests.app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with sync_playwright() as p:
            engine = os.environ.get('KANDU_TEST_ENGINE', 'chromium')
            browser = getattr(p, engine).launch(headless=True, **(
                {'executable_path':os.environ['KANDU_TEST_CHROME']} if engine == 'chromium' else {}))
            for mobile in [False, True]:
                context = browser.new_context(viewport=dict(width=393 if mobile else 1440, height=852),
                    is_mobile=mobile, has_touch=mobile, service_workers='block')
                page = context.new_page()
                page.bring_to_front()
                errors = []
                page.on('pageerror', lambda error:(errors.append(str(error)), print('PAGE ERROR:', str(error), flush=True)))
                page.goto(f'http://127.0.0.1:{server.server_port}/#/settings')
                page.locator('#password').fill(WebTests.authcode)
                page.locator('#login-form button').click()
                page.get_by_role('textbox', name='模型配置独立密码').fill(WebTests.config_password)
                page.get_by_role('button', name='解锁模型配置', exact=True).click()
                expect(page.locator('.model-item').first).to_contain_text('思考：沿用当前默认')
                for level, label in [('low','低 · low'), ('disabled','关闭思考'), ('auto','沿用当前默认')]:
                    page.locator('.model-item').first.get_by_role('button', name='编辑', exact=True).click()
                    select = page.get_by_role('combobox', name='思考强度', exact=True).last
                    expect(select).to_have_value('auto' if level == 'low' else 'low' if level == 'disabled' else 'disabled')
                    assert select.locator('option').evaluate_all('(nodes)=>nodes.map(n=>n.value)') == ['auto','disabled','low','medium','high']
                    select.select_option(level)
                    page.get_by_role('button', name='保存模型配置', exact=True).click()
                    expect(page.locator('#sheet')).not_to_be_visible()
                    expect(page.locator('.model-item').first).to_contain_text('思考：'+label)
                    assert config.load_settings()['llms'][0]['thinkingLevel'] == level
                    assert config.load_settings()['llms'][0]['apiKey'] == 'SOURCE_SECRET_DO_NOT_EXPOSE'
                    page.reload()
                    expect(page.locator('.model-item').first).to_contain_text('思考：'+label)
                # JSON import supports a choice before the model is first saved.
                page.get_by_role('combobox', name='思考强度', exact=True).select_option('medium')
                page.get_by_label('配置 JSON', exact=True).fill(json.dumps(dict(
                    baseUrl='https://example.invalid', apiKey='isolated-import-key', model='imported-model')))
                page.get_by_role('button', name='保存并设为默认', exact=True).click()
                expect(page.locator('.model-item').last).to_contain_text('思考：中 · medium')
                assert config.load_settings()['llms'][-1]['thinkingLevel'] == 'medium'
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                assert not errors, errors
                # Next viewport starts with the same legacy configuration shape.
                settings = config.load_settings()
                settings['llms'] = [dict(settings['llms'][0])]
                settings['llms'][0].pop('thinkingLevel', None)
                settings['defaultLlm'] = 'test'
                config.save_settings(settings)
                context.close()
                print(f'PASS: {engine}, {"iPhone" if mobile else "desktop"}', flush=True)
            browser.close()
    finally:
        server.shutdown()
        WebTests.tearDownClass()


if __name__ == '__main__':
    main()
