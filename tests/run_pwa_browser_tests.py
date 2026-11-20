"""Real service worker installation/offline/update using temporary public assets/data."""
import os
import re
from pathlib import Path
import shutil
import tempfile
import threading
from unittest.mock import patch

from flask import request
from playwright.sync_api import sync_playwright, expect
from werkzeug.serving import make_server

from run_web_tests import WebTests, store
from webapp import pwa
import webapp.server as server_module


def main():
    WebTests.setUpClass()
    paragraphs=['A candid reader follows the argument. '+('We read and think about science. '*20) for _ in range(24)]
    article=dict(index=0,title='Reading as an installed application',paragraphs=paragraphs)
    store._atomic_json_write(store._cache_dir('sample')/'content.json',dict(
        bookSize=WebTests.meta['size'],format='epub',articles=[article]))
    failed_asset=[False]
    worker_requests=[]

    @WebTests.app.before_request
    def fail_shell_install():
        if request.path=='/sw.js':worker_requests.append(request.path)
        if failed_asset[0] and request.path=='/static/pwa.js':
            return 'Simulated incomplete shell',503

    errors=[]
    with tempfile.TemporaryDirectory(prefix='kandu-pwa-browser-') as folder:
        assets=Path(folder)/'static';shutil.copytree(server_module.STATIC,assets)
        with patch.object(server_module,'STATIC',assets):
            server=make_server('127.0.0.1',0,WebTests.app,threaded=True)
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            base=f'http://127.0.0.1:{server.server_port}'
            try:
                with sync_playwright() as playwright:
                    engine=os.environ.get('KANDU_TEST_ENGINE','chromium')
                    expect.set_options(timeout=30_000 if engine=='webkit' else 10_000)
                    browser=getattr(playwright,engine).launch(headless=True,**(
                        {'executable_path':os.environ['KANDU_TEST_CHROME']} if engine=='chromium' else {}))
                    context=browser.new_context(viewport=dict(width=393,height=852),is_mobile=True,has_touch=True)
                    page=context.new_page();page.on('pageerror',lambda error:errors.append(str(error)))
                    page.goto(base+'/#/read/sample/0')
                    page.locator('#password').fill(WebTests.authcode);page.locator('#login-form button').click()
                    expect(page.locator('#reader-reading-time')).to_be_visible()
                    page.wait_for_function('() => !!navigator.serviceWorker.controller',timeout=60_000)
                    page.wait_for_function('() => state.reader?.vocab && ReadingSync.pending()===0')
                    # Home offers an explicit check in both browser and installed
                    # windows. Consecutive clicks bypass the background throttle.
                    page.evaluate("location.hash='#/library'")
                    control=page.locator('.pwa-check')
                    expect(control.get_by_role('button',name='检查更新',exact=True)).to_be_visible()
                    previous_checks=len(worker_requests)
                    for _ in range(2):
                        control.get_by_role('button',name='检查更新',exact=True).click()
                        expect(control).to_contain_text('已是最新版本')
                        expect(control.get_by_role('button')).to_be_enabled()
                    assert len(worker_requests)>=previous_checks+2,worker_requests
                    page.evaluate('''() => {
                        window.originalWorkerUpdate=ServiceWorkerRegistration.prototype.update;
                        ServiceWorkerRegistration.prototype.update=()=>Promise.reject(new TypeError('simulated failure'));
                    }''')
                    control.get_by_role('button',name='检查更新',exact=True).click()
                    expect(control).to_contain_text('检查失败，请确认网络连接后重试')
                    expect(control.get_by_role('button')).to_be_enabled()
                    page.evaluate('() => {ServiceWorkerRegistration.prototype.update=originalWorkerUpdate;}')
                    context.set_offline(True)
                    control.get_by_role('button',name='检查更新',exact=True).click()
                    expect(control).to_contain_text('当前离线')
                    expect(control.get_by_role('button')).to_be_enabled()
                    context.set_offline(False)
                    page.evaluate("location.hash='#/read/sample/0'")
                    expect(control).to_have_count(0)
                    expect(page.locator('#reader-reading-time')).to_be_visible()
                    if engine=='chromium':
                        manifest=context.new_cdp_session(page).send('Page.getAppManifest')
                        assert not manifest['errors'],manifest['errors']
                    entries=page.evaluate('''async () => {
                        const names=(await caches.keys()).filter(name=>name.startsWith('kandu-public-shell-'));
                        return (await (await caches.open(names[0])).keys()).map(request=>new URL(request.url).pathname);
                    }''')
                    assert '/' in entries and '/static/pwa.js' in entries
                    assert all(path=='/' or path.startswith('/static/') or path=='/manifest.webmanifest' for path in entries),entries
                    shell=page.evaluate("async () => (await caches.match('/')).text()")
                    assert article['title'] not in shell and 'SOURCE_SECRET_DO_NOT_EXPOSE' not in shell
                    # Opening cached content still works while the authenticated
                    # page is alive. Relaunching offline shows a useful public
                    # shell without inventing or caching an authenticated session.
                    context.set_offline(True)
                    page.evaluate('() => {saveProgress(true);flushReadingClock();}')
                    page.wait_for_function('() => ReadingSync.pending()>0')
                    journals=page.evaluate("() => Object.keys(localStorage).filter(key=>key.startsWith('kandu-reading-outbox:')).length")
                    assert journals>0
                    page.reload()
                    expect(page.get_by_role('heading',name=re.compile('当前离线|暂时无法连接书库'))).to_be_visible()
                    assert page.evaluate("() => Object.keys(localStorage).filter(key=>key.startsWith('kandu-reading-outbox:')).length")>=journals
                    assert page.evaluate("async () => {try {await fetch('/api/session');return false;}catch{return true;}}")
                    context.set_offline(False)
                    page.evaluate("dispatchEvent(new Event('online'))")
                    expect(page.locator('#reader-reading-time')).to_be_visible()
                    page.wait_for_function('() => ReadingSync.pending()===0')
                    page.evaluate("location.hash='#/settings'")
                    expect(page.get_by_role('heading',name='安装到主屏幕',exact=True)).to_be_visible()
                    page.get_by_role('button',name='查看安装步骤').click()
                    expect(page.locator('.pwa-settings')).to_contain_text('分享按钮')
                    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                    if artifacts:=os.environ.get('KANDU_TEST_ARTIFACTS'):
                        target=Path(artifacts);target.mkdir(parents=True,exist_ok=True)
                        page.screenshot(path=str(target/f'pwa-install-{engine}.png'),full_page=True)
                    page.evaluate("Object.defineProperty(navigator,'standalone',{configurable:true,value:true});route()")
                    expect(page.locator('.pwa-settings')).to_contain_text('独立窗口')
                    page.evaluate("location.hash='#/read/sample/0'")
                    expect(page.locator('#reader-reading-time')).to_be_visible()
                    page.locator('#para-3').scroll_into_view_if_needed()
                    y=page.evaluate('scrollY')
                    # A partial deployment cannot replace the working bundle.
                    failed_asset[0]=True
                    css=assets/'app.css';css.write_text(css.read_text(encoding='utf-8')+'\n/* PWA test update */\n',encoding='utf-8')
                    page.evaluate('''async () => {
                        const registration=await navigator.serviceWorker.getRegistration();
                        const redundant=new Promise(resolve=>registration.addEventListener('updatefound',()=>{
                            const worker=registration.installing;
                            worker.addEventListener('statechange',()=>{if(worker.state==='redundant')resolve();});
                        },{once:true}));
                        await registration.update();await redundant;
                    }''')
                    expect(page.locator('.pwa-update')).to_have_count(0)
                    assert page.evaluate('scrollY')==y
                    page.evaluate("location.hash='#/library'")
                    control.get_by_role('button',name='检查更新',exact=True).click()
                    expect(control).to_contain_text('新版下载失败')
                    expect(control.get_by_role('button')).to_be_enabled()
                    failed_asset[0]=False
                    await_version=pwa.bundle(assets)[0]
                    control.get_by_role('button',name='检查更新',exact=True).click()
                    expect(page.locator('.pwa-update')).to_be_visible()
                    expect(control.get_by_role('button',name='更新刊读',exact=True)).to_be_enabled()
                    page.evaluate("state.activeJobs.set('home-update-job',{})")
                    control.get_by_role('button',name='更新刊读',exact=True).click()
                    expect(control).to_contain_text('任务正在运行')
                    page.evaluate("state.activeJobs.delete('home-update-job')")
                    page.evaluate("location.hash='#/read/sample/0'")
                    expect(page.locator('#reader-reading-time')).to_be_visible()
                    if page.locator('#reader-remote-progress').is_visible():
                        page.get_by_role('button',name='保持当前位置',exact=True).click()
                    page.evaluate("document.querySelector('#reading-cache-notice')?.remove()")
                    page.evaluate('(y)=>scrollTo(0,y)',y)
                    assert page.evaluate('scrollY')==y
                    if artifacts:=os.environ.get('KANDU_TEST_ARTIFACTS'):
                        page.screenshot(path=str(Path(artifacts)/f'pwa-update-{engine}.png'))
                    page.evaluate("state.activeJobs.set('pwa-test-job',{})")
                    page.locator('.pwa-update').get_by_role('button',name='更新',exact=True).click()
                    expect(page.locator('.pwa-update')).to_contain_text('任务正在运行')
                    page.evaluate("state.activeJobs.delete('pwa-test-job')")
                    context.set_offline(True)
                    page.evaluate('() => saveProgress(true)')
                    page.locator('.pwa-update').get_by_role('button',name='更新',exact=True).click()
                    expect(page.locator('.pwa-update')).to_contain_text('阅读记录尚未同步')
                    assert page.evaluate('scrollY')==y
                    context.set_offline(False)
                    with page.expect_navigation(wait_until='domcontentloaded'):
                        page.locator('.pwa-update').get_by_role('button',name='更新',exact=True).click()
                    page.wait_for_function('(version) => navigator.serviceWorker.controller && caches.keys().then(keys=>keys.includes("kandu-public-shell-"+version)&&keys.filter(key=>key.startsWith("kandu-public-shell-")).length===1)',arg=await_version,timeout=60_000)
                    expect(page.locator('#reader-reading-time')).to_be_visible()
                    assert page.url.endswith('#/read/sample/0')
                    expect(page.locator('.pwa-update')).to_have_count(0)
                    # A subsequent update can be completed entirely from Home.
                    css.write_text(css.read_text(encoding='utf-8')+'\n/* Home update test */\n',encoding='utf-8')
                    home_version=pwa.bundle(assets)[0]
                    page.evaluate("location.hash='#/library'")
                    control.get_by_role('button',name='检查更新',exact=True).click()
                    expect(control.get_by_role('button',name='更新刊读',exact=True)).to_be_enabled()
                    expect(page.locator('.pwa-update')).to_be_visible()
                    page.evaluate("document.querySelector('#reading-cache-notice')?.remove()")
                    page.locator('.pwa-update').get_by_role('button',name='稍后',exact=True).click()
                    expect(page.locator('.pwa-update')).to_have_count(0)
                    with page.expect_navigation(wait_until='domcontentloaded'):
                        control.get_by_role('button',name='更新刊读',exact=True).click()
                    page.wait_for_function('(version) => caches.keys().then(keys=>keys.includes("kandu-public-shell-"+version)&&keys.filter(key=>key.startsWith("kandu-public-shell-")).length===1)',arg=home_version,timeout=60_000)
                    expect(page.get_by_role('heading',name='我的阅读室',exact=True)).to_be_visible()
                    expect(control.get_by_role('button',name='检查更新',exact=True)).to_be_visible()
                    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                    if artifacts:=os.environ.get('KANDU_TEST_ARTIFACTS'):
                        page.screenshot(path=str(Path(artifacts)/f'pwa-home-{engine}.png'))
                    # Logout clears reading data but retains a public launch shell.
                    page.locator('#logout').click()
                    expect(page.locator('#login-form')).to_be_visible()
                    assert (page.evaluate('() => ReadingCache.info()'))['articles']==0
                    assert page.evaluate("async () => (await caches.keys()).filter(name=>name.startsWith('kandu-public-shell-')).length")==1
                    assert not errors,errors
                    context.close();browser.close()
                print(f'PWA BROWSER PASS ({engine}): manifest, icons, public-only shell, offline launch/reconnect, unsent journal retention, install help/standalone, incomplete install rollback, explicit update/save, logout')
            finally:
                server.shutdown();server.server_close();thread.join(timeout=5)
                WebTests.tearDownClass()


if __name__=='__main__':
    main()
