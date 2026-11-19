"""Two independent browsers receive live positions without jumping or stale writes."""
import os
import threading
from unittest.mock import patch
from wsgiref.simple_server import make_server

from playwright.sync_api import sync_playwright, expect
from run_mobile_reading_browser_tests import BrowserServer, snapshot, shot
from run_web_tests import WebTests, config, store, core_dict
from webapp import reading_activity


PARAGRAPHS=[f'Paragraph {i}. '+('A candid reader follows the argument and reflects on science. '*18) for i in range(25)]


def saved(page):
    return page.request.get('/api/books/sample/articles/0/progress').json()['record']


def go(page,paragraph):
    page.locator(f'#para-{paragraph}').scroll_into_view_if_needed()
    snapshot(page)
    page.evaluate('saveProgress()')
    page.wait_for_function('() => ReadingSync.pending()===0')
    return saved(page)


def main():
    WebTests.setUpClass()
    store._atomic_json_write(config.app_dir()/'cache/sample/content.json',dict(
        bookSize=WebTests.meta['size'],format='epub',articles=[dict(
            index=0,title='Live progress between devices',paragraphs=PARAGRAPHS)]))
    server=make_server('127.0.0.1',0,WebTests.app,server_class=BrowserServer)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    errors=[]
    try:
        with sync_playwright() as p,patch.object(core_dict,'ensure_db'),patch.object(core_dict,'lookup_many',return_value={}):
            engine=os.environ.get('KANDU_TEST_ENGINE','chromium')
            browser=getattr(p,engine).launch(headless=True,**(
                {'executable_path':os.environ['KANDU_TEST_CHROME']} if engine=='chromium' else {}))
            expect.set_options(timeout=15000 if engine=='webkit' else 5000)
            base=f'http://127.0.0.1:{server.server_port}'
            phone_context=browser.new_context(base_url=base,viewport=dict(width=393,height=659),device_scale_factor=3,
                                              is_mobile=True,has_touch=True)
            pc_context=browser.new_context(base_url=base,viewport=dict(width=1440,height=1000))
            phone=phone_context.new_page();pc=pc_context.new_page()
            for page in [phone,pc]:
                page.on('pageerror',lambda e:errors.append(str(e)))
                page.add_init_script("addEventListener('unhandledrejection',e=>{window.unhandled=(window.unhandled||[]).concat(String(e.reason))})")
                page.goto(base+'/#/read/sample/0')
                page.locator('#password').fill(WebTests.authcode);page.locator('#login-form button').click()
                page.wait_for_function('() => state.reader?.ready&&state.reader.vocab')
                page.wait_for_function('() => ReadingSync.pending()===0&&!state.reader.progressChecking')
            phone.evaluate('refreshRemoteProgress()')
            notice=phone.locator('#reader-remote-progress')
            expect(notice).not_to_be_visible()
            before=snapshot(phone)
            remote=go(pc,9)
            assert remote['paragraphIndex']>=8,remote
            # The real 15-second foreground timer notices a PC update without
            # reloading, tapping, or navigating the already-open phone article.
            expect(notice).to_be_visible(timeout=20000)
            assert snapshot(phone)==before,'A remote update must not move the phone'
            expect(notice).to_contain_text(f"第 {remote['paragraphIndex']+1} 段")
            shot(phone,f'live-progress-{engine}')
            assert saved(phone)['lastReadAt']==remote['lastReadAt']
            phone.evaluate('saveProgress()')
            assert saved(phone)==remote,'An idle old viewport must not overwrite PC progress'
            # Resuming uses the paragraph+in-paragraph position and a new timing
            # baseline, without granting the skipped passage as measured words.
            old_id=phone.evaluate('state.reader.clock.id')
            measured=reading_activity.article_timing('sample',0)['measuredWords']
            phone.locator('#reading-time-toggle').tap()
            assert phone.evaluate('state.reader.clock.manualPaused')
            phone.get_by_role('button',name='继续到新进度',exact=True).tap()
            expect(notice).not_to_be_visible()
            phone.wait_for_function('() => !state.reader.restoringProgress&&ReadingSync.pending()===0')
            position=phone.evaluate('currentPosition()')
            assert position['paragraphIndex']==remote['paragraphIndex'],(position,remote)
            assert abs(position['paragraphOffset']-remote['paragraphOffset'])<=5,(position,remote)
            assert phone.evaluate('state.reader.clock.id')!=old_id
            assert phone.evaluate('state.reader.clock.manualPaused')
            assert reading_activity.article_timing('sample',0)['measuredWords']==measured
            assert saved(phone)['paragraphIndex']==remote['paragraphIndex']
            # Offline phone reading followed by a later PC advance: replay must
            # report a conflict, even though the phone observation time is newer.
            phone_context.set_offline(True)
            phone.locator('#para-3').scroll_into_view_if_needed();snapshot(phone)
            phone.evaluate('saveProgress()')
            assert phone.evaluate('ReadingSync.pending()')>0
            remote=go(pc,15)
            phone_context.set_offline(False)
            phone.evaluate("window.dispatchEvent(new Event('online'))")
            expect(notice).to_be_visible()
            phone.wait_for_function('() => ReadingSync.pending()===0')
            assert saved(phone)['paragraphIndex']==remote['paragraphIndex']
            # Choosing to keep the local location is explicit and is allowed to
            # save it as the new reading position, including deliberate rereads.
            local_position=phone.evaluate('currentPosition()')
            phone.get_by_role('button',name='保持当前位置',exact=True).tap()
            expect(notice).not_to_be_visible()
            phone.wait_for_function('() => ReadingSync.pending()===0')
            actual=saved(phone)
            assert actual['paragraphIndex']==local_position['paragraphIndex'],dict(saved=actual,local=local_position,
                current=phone.evaluate('currentPosition()'),pending=phone.evaluate('ReadingSync.entries()'))
            # Returning to the phone triggers an immediate fresh check. Repeated
            # focus/visibility events share one request; no article/vocab GETs.
            pc.evaluate('refreshRemoteProgress()')
            remote=go(pc,19)
            pulled=[];article_gets=[]
            phone.on('request',lambda r:article_gets.append(r.url) if r.method=='GET' and r.url.endswith(('/articles/0','/vocab')) else None)
            phone.route('**/articles/0/progress',lambda route:pulled.append(route) if route.request.method=='GET' else route.continue_())
            phone.evaluate("() => {window.dispatchEvent(new Event('focus'));document.dispatchEvent(new Event('visibilitychange'));window.dispatchEvent(new Event('pageshow'));}")
            phone.wait_for_function('() => !!state.reader.progressChecking')
            assert len(pulled)==1
            # A vocabulary dialog can remain open while a newer position arrives.
            phone.locator('#badge-3').tap();expect(phone.locator('#sheet')).to_be_visible()
            pulled.pop().continue_();phone.unroute('**/articles/0/progress')
            phone.wait_for_function('() => !!state.reader.remoteProgress')
            expect(notice).not_to_be_visible()
            before=snapshot(phone)
            phone.locator('#sheet-close').tap();expect(notice).to_be_visible()
            assert snapshot(phone)==before
            assert not article_gets,'Live progress must only fetch the small progress endpoint'
            # Hidden/offline pages do not poll; late results are ignored on route
            # changes, and updates for changed prose never jump the old article.
            phone.evaluate("Object.defineProperty(document,'hidden',{value:true,configurable:true})")
            pulls=[]
            phone.on('request',lambda r:pulls.append(r.url) if r.method=='GET' and r.url.endswith('/progress') else None)
            phone.evaluate('refreshRemoteProgress()')
            assert not pulls
            phone.evaluate('delete document.hidden')
            phone.route('**/articles/0/progress',lambda route:pulled.append(route) if route.request.method=='GET' else route.continue_())
            phone.evaluate('() => {refreshRemoteProgress();}')
            phone.wait_for_function('() => !!state.reader.progressChecking')
            before=snapshot(phone)
            pulled.pop().fulfill(json=dict(record=remote,state='unread',contentVersion='new-prose'))
            expect(phone.locator('#reading-cache-notice')).to_contain_text('正文已更新')
            assert snapshot(phone)==before
            phone.evaluate('() => {refreshRemoteProgress();}')
            phone.wait_for_function('() => !!state.reader.progressChecking')
            phone.evaluate("location.hash='#/library'")
            expect(phone.locator('.book-card')).to_have_count(1)
            pulled.pop().fulfill(json=dict(record=remote,state='unread',contentVersion='late'))
            expect(phone.locator('#reader-remote-progress')).to_have_count(0)
            for page in [phone,pc]:
                assert not page.evaluate('window.unhandled||[]')
            phone_context.close();pc_context.close();browser.close()
        assert not errors,errors
        print('LIVE PROGRESS PASS: independent phone/PC, real timer, foreground catchup, small GETs, no forced scroll, resume/timing baseline, offline conflict protection, explicit stay, modal/hidden/version/route guards; no real models.')
    finally:
        server.shutdown();server.server_close();thread.join(timeout=5);WebTests.tearDownClass()


if __name__=='__main__':main()
