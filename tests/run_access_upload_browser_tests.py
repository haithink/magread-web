"""Browser and real HTTP checks with a temporary library and local Waitress."""
from contextlib import closing
from io import BytesIO
import os
import sqlite3
import threading
import time
import zipfile

import requests
from playwright.sync_api import sync_playwright, expect
from waitress import create_server

from run_web_tests import WebTests, SANDBOX, config
from webapp.upload_security import UploadQuota, FILE_BYTES, DAILY_BYTES, REQUEST_BYTES


def main():
    WebTests.setUpClass()
    app = WebTests.app
    server = create_server(app, host='127.0.0.1', port=0, threads=4, max_request_body_size=REQUEST_BYTES)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    url = f'http://127.0.0.1:{server.effective_port}'
    errors = []
    try:
        with sync_playwright() as playwright:
            chrome = os.environ.get('KANDU_TEST_CHROME')
            browser = playwright.chromium.launch(**({'executable_path':chrome} if chrome else {}), headless=True)
            contexts = [browser.new_context(viewport={'width':1440,'height':1000}),
                        browser.new_context(viewport={'width':390,'height':844}, is_mobile=True, has_touch=True)]
            pages = []
            for context in contexts:
                page = context.new_page()
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.clock.install()
                page.goto(url)
                page.locator('#password').fill(WebTests.authcode)
                page.locator('#login-form button').click()
                page.get_by_role('link', name='设置', exact=True).click()
                expect(page.locator('.config-locked')).to_be_visible()
                expect(page.locator('.model-item')).to_have_count(0)
                expect(page.get_by_label('默认已掌握的词汇')).to_be_visible()
                pages.append(page)
            desktop, phone = pages
            for page in pages:
                page.get_by_label('模型配置独立密码').fill('wrong-isolated-password')
                page.get_by_role('button', name='解锁模型配置', exact=True).click()
                expect(page.locator('.config-unlock-form .error')).to_contain_text('独立密码不正确')
                expect(page.get_by_label('模型配置独立密码')).to_have_value('')
                page.get_by_label('模型配置独立密码').fill(WebTests.config_password)
                page.get_by_role('button', name='解锁模型配置', exact=True).click()
                expect(page.locator('.model-item')).to_have_count(1)
                assert 'SOURCE_SECRET_DO_NOT_EXPOSE' not in page.content()
                if page is desktop:
                    expect(phone.locator('.config-locked')).to_be_visible()
                page.reload()
                expect(page.locator('.model-item')).to_have_count(1)
                width = page.evaluate('() => ({scroll:document.documentElement.scrollWidth,width:innerWidth})')
                assert width['scroll'] <= width['width'], width
            desktop.get_by_role('button', name='立即锁定模型配置').click()
            expect(desktop.locator('.config-locked')).to_be_visible()
            expect(desktop.locator('.model-item')).to_have_count(0)
            expect(phone.locator('.model-item')).to_have_count(1)
            with closing(sqlite3.connect(config.app_dir()/'config-access.sqlite3')) as db, db:
                db.execute('UPDATE grants SET expires=?', (time.time()-1,))
            phone.clock.fast_forward(901_000)
            expect(phone.locator('.config-locked')).to_be_visible()
            expect(phone.locator('.model-item')).to_have_count(0)

            desktop.get_by_role('link', name='书库', exact=True).click()
            desktop.get_by_role('button', name='＋ 添加刊物').click()
            expect(desktop.locator('.upload-quota')).to_contain_text('50 MB')
            expect(desktop.locator('.upload-quota')).to_contain_text('500 MB')
            uploads = []
            desktop.on('request', lambda request: uploads.append(request) if request.method=='POST' and request.url==url+'/api/books' else None)
            desktop.get_by_label('选择刊物').evaluate('''input => {
                const file = new File(['x'], 'too-large.epub');
                Object.defineProperty(file, 'size', {value:50*1024**2+1});
                const transfer = new DataTransfer();transfer.items.add(file);input.files=transfer.files;
                Object.defineProperty(input, 'files', {value:[file],configurable:true});
            }''')
            desktop.get_by_role('button', name='上传并加入书库').click()
            expect(desktop.locator('#toast')).to_contain_text('不得超过 50 MB')
            assert not uploads
            desktop.get_by_label('选择刊物').evaluate("input => {delete input.files;}")
            stream = BytesIO()
            with zipfile.ZipFile(stream, 'w') as archive:
                archive.writestr('META-INF/container.xml','<container><rootfiles><rootfile full-path="content.opf"/></rootfiles></container>')
                archive.writestr('content.opf','<package><manifest><item id="text" href="text.xhtml"/></manifest><spine><itemref idref="text"/></spine></package>')
                archive.writestr('text.xhtml','<html><body><h1>A browser upload</h1><p>Original text for an isolated browser publication upload without any model requests.</p></body></html>')
            content = stream.getvalue()
            desktop.get_by_label('选择刊物').set_input_files({'name':'browser-upload.epub','mimeType':'application/epub+zip','buffer':content})
            desktop.get_by_role('button', name='上传并加入书库').click()
            # The page clock is controlled; trigger its job polling timer.
            desktop.clock.run_for(1500)
            expect(desktop.locator('#sheet')).not_to_be_visible()
            expect(desktop.locator('.article-card')).to_have_count(1)
            assert len(uploads)==1 and uploads[0].headers['x-upload-size']==str(len(content))

            session = requests.Session()
            session.trust_env = False
            login = session.post(url+'/api/login', json={'password':WebTests.authcode}, timeout=10)
            assert login.status_code==200
            headers = {'X-CSRF-Token':login.json()['csrf']}
            quota = UploadQuota(config.app_dir()/'upload-quota.sqlite3')
            # Seed only the isolated quota so exactly 50 MiB remain, then send
            # a real 50 MiB multipart body through Waitress, not Flask's client.
            used = quota.status()['usedBytes']
            seeded = DAILY_BYTES-used-FILE_BYTES
            ticket = quota.reserve(seeded)
            quota.finish(ticket, seeded)
            payload = BytesIO(b'x'*FILE_BYTES)
            response = session.post(url+'/api/books', files={'file':('boundary.exe',payload)},
                headers=dict(headers, **{'X-Upload-Size':str(FILE_BYTES)}), timeout=30)
            assert response.status_code==400, response.status_code  # File accepted; extension refused.
            assert quota.status()['usedBytes']==DAILY_BYTES
            response = session.post(url+'/api/books', files={'file':('over-budget.epub',b'x')},
                headers=dict(headers, **{'X-Upload-Size':'1'}), timeout=10)
            assert response.status_code==429, response.status_code
            desktop.get_by_role('link', name='书库', exact=True).click()
            desktop.get_by_role('button', name='＋ 添加刊物').click()
            expect(desktop.locator('.upload-quota')).to_contain_text('今天剩余 0 MB')
            assert not errors, errors
            browser.close()
        print('BROWSER/HTTP PASS: desktop + 390px phone; extra password, reload, lock, isolated sessions, timed expiry, no key in DOM; quota display, oversize precheck, real EPUB upload; exact 50 MiB multipart through Waitress, global 500 MiB limit enforced.')
    finally:
        server.close()
        server.task_dispatcher.shutdown()
        thread.join(timeout=5)
        WebTests.tearDownClass()
        SANDBOX.cleanup()


if __name__=='__main__':
    main()
