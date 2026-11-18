"""Persistent cache, slow/offline navigation, retry journal and bounded storage."""
import os
import threading
from unittest.mock import patch
from wsgiref.simple_server import make_server

from playwright.sync_api import sync_playwright, expect
from run_web_tests import WebTests, config, store, core_dict
from run_mobile_reading_browser_tests import BrowserServer


def main():
    WebTests.setUpClass()
    paragraphs=['A candid reader follows the argument. '+('We read and think about science. '*20) for _ in range(18)]
    article=dict(index=0,title='Reading with a local cache',paragraphs=paragraphs)
    store._atomic_json_write(config.app_dir()/'cache/sample/content.json',dict(
        bookSize=WebTests.meta['size'],format='epub',articles=[article]))
    store.save_vocab('sample',dict(articleIndex=0,level='cet4',titleEntries=[],idioms=[],
        processedParagraphs=list(range(18)),paragraphs=[dict(index=i,entries=[dict(word='candid',sense='坦率的')]) for i in range(18)]))
    from PIL import Image
    Image.new('RGB',(32,48),'navy').save(store.cover_file('sample'))
    server=make_server('127.0.0.1',0,WebTests.app,server_class=BrowserServer)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    errors=[]
    try:
        with sync_playwright() as p,patch.object(core_dict,'ensure_db'),patch.object(core_dict,'lookup_many',return_value={}):
            engine=os.environ.get('KANDU_TEST_ENGINE','chromium')
            expect.set_options(timeout=20000 if engine=='webkit' else 5000)
            browser=getattr(p,engine).launch(headless=True,**({'executable_path':os.environ['KANDU_TEST_CHROME']} if engine=='chromium' else {}))
            for mobile in [False,True] if engine=='chromium' else [True]:
                print(f'Checking {engine} '+('phone' if mobile else 'desktop'),flush=True)
                context=browser.new_context(viewport=dict(width=393 if mobile else 1440,height=852 if mobile else 1000),
                                            is_mobile=mobile,has_touch=mobile)
                page=context.new_page();page.on('pageerror',lambda e:errors.append(dict(message=str(e),stack=e.stack)))
                page.add_init_script("addEventListener('unhandledrejection',e=>{window.unhandled=(window.unhandled||[]).concat(String(e.reason))})")
                base=f'http://127.0.0.1:{server.server_port}'
                page.goto(base+'/#/library');page.locator('#password').fill(WebTests.authcode);page.locator('#login-form button').click()
                expect(page.locator('.book-card')).to_have_count(1)
                page.wait_for_function('async() => !!await ReadingCache.get("cover:sample")')
                page.wait_for_function('() => document.querySelector(".cover-link img").naturalWidth===32')
                covers=[]
                page.on('request',lambda request:covers.append(request.url) if request.url.endswith('/cover') else None)
                page.evaluate('staleReadingCache()')
                page.reload();expect(page.locator('.book-card')).to_have_count(1)
                page.wait_for_function('() => document.querySelector(".cover-link img").naturalWidth===32')
                assert not covers,'Persisted covers must render without another request'
                # An expired cover remains visible and validates via ETag; a
                # 304 must preserve its saved image rather than save an empty body.
                page.evaluate('''async()=>{
                    const row=await ReadingCache.get('cover:sample');
                    await ReadingCache.put('cover:sample',row.value,{...row,checked:Date.now()-3600_001});
                }''')
                validators=[]
                def unchanged_cover(route):
                    validators.append(route.request.headers.get('if-none-match'))
                    route.continue_()
                page.route('**/api/books/sample/cover',unchanged_cover)
                page.reload();expect(page.locator('.book-card')).to_have_count(1)
                page.wait_for_function('() => document.querySelector(".cover-link img").naturalWidth===32')
                page.wait_for_function('async() => (await ReadingCache.get("cover:sample")).checked>Date.now()-30_000')
                assert len(validators)==1 and validators[0],validators
                page.unroute('**/api/books/sample/cover')
                page.goto(base+'/#/book/sample');expect(page.locator('.article-card')).to_have_count(1)
                page.goto(base+'/#/read/sample/0');expect(page.locator('#vocab-button')).to_have_text('生词表 1')
                page.wait_for_function('() => ReadingSync.pending()===0')
                vocab_requests=[]
                page.on('request',lambda request:vocab_requests.append(request.url) if request.url.endswith('/vocab') else None)
                for _ in range(3):
                    page.locator('#badge-4').click();expect(page.locator('#sheet')).to_be_visible();page.locator('#sheet-close').click()
                assert not vocab_requests,'Opening paragraphs must reuse the loaded vocabulary'
                # A slow save must not hold navigation or cached article rendering.
                held=[]
                page.route('**/progress',lambda route:held.append(route))
                with page.expect_request('**/progress'):
                    page.evaluate("() => { state.reader.lastPosition=null; saveProgress(); }")
                page.evaluate("location.hash='#/book/sample'")
                expect(page.locator('.article-card')).to_have_count(1,timeout=3000)
                assert held,'Progress must really be in flight'
                page.evaluate("location.hash='#/read/sample/0'")
                expect(page.locator('#vocab-button')).to_have_text('生词表 1',timeout=12000 if engine=='webkit' else 3000)
                for route in held:route.continue_()
                page.unroute('**/progress')
                page.wait_for_function('() => ReadingSync.pending()===0')
                # Stale content is served immediately; validation never moves prose.
                await_read="() => ReadingCache.stale()"
                page.evaluate(await_read)
                pending=[]
                page.route('**/api/books/sample/articles/0',lambda route:pending.append(route))
                page.evaluate("location.hash='#/book/sample'");expect(page.locator('.article-card')).to_have_count(1)
                page.evaluate("location.hash='#/read/sample/0'")
                expect(page.locator('.reader-head h1')).to_have_text(article['title'],timeout=3000)
                page.wait_for_function('() => state.reader?.ready')
                before=page.evaluate('scrollY')
                # Return a changed article to the background request, not the DOM.
                page.wait_for_timeout(100)
                assert pending
                response=page.request.get(base+'/api/books/sample/articles/0').json()
                response['article']['paragraphs'][0]='Changed remotely. '+paragraphs[0]
                for route in pending:route.fulfill(json=response)
                expect(page.locator('#reading-cache-notice')).to_contain_text('有更新')
                assert page.locator('#para-0 p').text_content()==paragraphs[0]
                assert page.evaluate('scrollY')==before
                page.unroute('**/api/books/sample/articles/0')
                other=context.new_page();other.goto(base+'/#/read/sample/0')
                expect(other.locator('#vocab-button')).to_have_text('生词表 1')
                other.wait_for_function('() => ReadingSync.pending()===0')
                page.wait_for_function('() => cacheRequests.size===0')
                other.wait_for_function('() => cacheRequests.size===0')
                # Offline navigation works within the loaded application, including
                # paragraphs and progress; no general writes are silently queued.
                context.set_offline(True)
                page.evaluate("location.hash='#/library'");expect(page.locator('.book-card')).to_have_count(1)
                page.wait_for_function('() => document.querySelector(".cover-link img").naturalWidth===32')
                page.evaluate("location.hash='#/book/sample'");expect(page.locator('.article-card')).to_have_count(1)
                page.evaluate("location.hash='#/read/sample/0'");expect(page.locator('#vocab-button')).to_have_text('生词表 1')
                page.locator('#para-7').scroll_into_view_if_needed();page.wait_for_timeout(1000)
                page.evaluate('saveProgress()')
                assert page.evaluate('ReadingSync.pending()')>0
                assert page.evaluate("Object.keys(localStorage).some(k=>k.startsWith('kandu-reading-outbox:'))")
                queued=page.evaluate('ReadingSync.entries()')
                assert all(item['path'].endswith(('/progress','/reading-time')) for item in queued)
                other.locator('#para-9').scroll_into_view_if_needed();other.wait_for_timeout(1000)
                other.evaluate('saveProgress()')
                assert other.evaluate('ReadingSync.pending()')>0
                assert page.evaluate("Object.keys(localStorage).filter(k=>k.startsWith('kandu-reading-outbox:')).length")>=2
                with page.expect_response('**/vocab'):
                    context.set_offline(False)
                page.wait_for_function('() => cacheRequests.size===0')
                other.close()
                # A new document recovers its predecessor's durable queue.
                page.reload();expect(page.locator('#vocab-button')).to_have_text('生词表 1')
                page.evaluate('ReadingSync.drain().catch(()=>{})');page.wait_for_function('() => ReadingSync.pending()===0')
                history=store.load_reading_history('sample')['0']
                assert history['paragraphIndex']>=6,history
                # A repeated sentence explanation uses a browser result, no job POST.
                calls=[]
                page.route('**/analyze',lambda route:(calls.append(route.request.post_data_json),route.fulfill(status=202,json=dict(jobId='cache-analysis')))[-1])
                page.route('**/api/jobs/cache-analysis',lambda route:route.fulfill(json=dict(state='done',result=dict(translation='模拟讲解'))))
                for _ in range(2):
                    page.evaluate("() => {state.selection={quote:'A candid reader follows the argument.',paragraph:0};analyzeSelection(state.reader);}")
                    expect(page.locator('#sheet')).to_be_visible();expect(page.locator('#sheet-content')).to_contain_text('模拟讲解')
                    page.locator('#sheet-close').click()
                assert len(calls)==1
                # A model result arriving after logout must not recreate a cache.
                page.evaluate('''async()=>{
                    const original=runJob;
                    try {
                        runJob=async(path,data,onDone)=>{window.lateCacheResult=onDone;};
                        state.selection={quote:'A result that arrives after logout.',paragraph:0};
                        await analyzeSelection(state.reader);
                    } finally {runJob=original;closeSheet();}
                }''')
                # Settings never enter IndexedDB. Clearing snapshots retains the journal.
                page.goto(base+'/#/settings');expect(page.get_by_role('heading',name='本机阅读缓存')).to_be_visible()
                assert page.evaluate("async()=>{const row=await ReadingCache.get('/settings');return row===null}")
                # LRU group count and byte budget, using synthetic generated strings.
                page.evaluate('''async()=>{
                    await ReadingCache.clear();
                    for(let i=0;i<20;i++)await ReadingCache.put('fixture:'+i,{text:'small'}, {group:'article:'+i});
                    await ReadingCache.get('fixture:0');
                    await ReadingCache.put('fixture:20',{text:'small'},{group:'article:20'});
                    if(!await ReadingCache.get('fixture:0')||await ReadingCache.get('fixture:1'))throw Error('LRU group retention');
                    const text='x'.repeat(18*1024*1024);
                    for(let i=0;i<3;i++)await ReadingCache.put('large:'+i,{text},{group:'large:'+i});
                    const info=await ReadingCache.info();
                    if(info.bytes>50*1024*1024||info.articles>20)throw Error('Cache budget exceeded');
                    await ReadingCache.clear();
                }''')
                page.get_by_role('button',name='退出',exact=True).click();expect(page.locator('#login-form')).to_be_visible()
                page.evaluate("async()=>{await window.lateCacheResult({state:'done',result:{translation:'late result'}});}")
                assert page.evaluate('(async()=> (await ReadingCache.info()).bytes)()')==0
                assert not page.evaluate('window.unhandled||[]')
                context.close()
            if engine=='chromium':
                fallback=browser.new_context()
                fallback.add_init_script("Object.defineProperty(window,'indexedDB',{value:{open(){throw Error('storage disabled')}}})")
                fallback_page=fallback.new_page();fallback_page.goto(base+'/#/read/sample/0')
                fallback_page.locator('#password').fill(WebTests.authcode);fallback_page.locator('#login-form button').click()
                expect(fallback_page.locator('#vocab-button')).to_have_text('生词表 1')
                assert not fallback_page.evaluate('async()=> (await ReadingCache.info()).persistent')
                fallback.close()
            browser.close()
        assert not errors,errors
        print('READING CACHE PASS: persistent/offline covers, cover ETag validation, warm reads, zero repeated vocabulary GETs, nonblocking saves, background change notice, offline navigation, durable replay, analysis reuse, LRU/50MB, private settings and logout; no real models.')
    finally:
        server.shutdown();server.server_close();thread.join(timeout=5);WebTests.tearDownClass()


if __name__=='__main__':main()
