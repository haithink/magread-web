"""Touch definitions and immediate paragraph vocabulary, with isolated fixtures."""
import os
import threading
from unittest.mock import patch
from wsgiref.simple_server import make_server

from playwright.sync_api import sync_playwright, expect
from run_mobile_reading_browser_tests import BrowserServer, snapshot, shot
from run_vocab_hover_browser_tests import word_point
from run_web_tests import WebTests, config, store, core_dict, gen, wordbook


TEXT = "A candid reader follows the argument. It certainly won’t paper over the uncertainties. "
PARAGRAPHS = [f'Note {i}. ' + TEXT * 3 for i in range(80)]


def tap(page, word, paragraph=4):
    point = word_point(page, word, paragraph)
    page.evaluate("y=>scrollBy({top:y-innerHeight/2,behavior:'instant'})", point['y'])
    snapshot(page)
    point = word_point(page, word, paragraph, scroll=False)
    before = snapshot(page)
    page.touchscreen.tap(point['x'], point['y'])
    return before


def main():
    WebTests.setUpClass()
    store._atomic_json_write(config.app_dir()/'cache/sample/content.json', dict(
        bookSize=WebTests.meta['size'], format='epub', articles=[dict(
            index=0, title='Mobile vocabulary performance', paragraphs=PARAGRAPHS)]))
    store.save_vocab('sample', dict(articleIndex=0, level='cet4', titleEntries=[], idioms=[],
        paragraphs=[dict(index=i, entries=[dict(word='candid', phonetic='/kand-id/', pos='adj.',
            sense=f'第 {i+1} 段：坦率的', root='Latin candidus', inText='A candid reader'),
            dict(word='paper over', sense='掩盖问题', inText='paper over the uncertainties')]) for i in range(80)],
        processedParagraphs=[-1, *range(80)], phraseProcessedParagraphs=[-1, *range(80)],
        coverageVersion=gen.VOCAB_COVERAGE_VERSION))
    server=make_server('127.0.0.1',0,WebTests.app,server_class=BrowserServer)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    errors=[]
    try:
        with sync_playwright() as playwright, patch.object(core_dict,'ensure_db'), patch.object(core_dict,'lookup_many',return_value={}):
            engine=os.environ.get('KANDU_TEST_ENGINE','chromium')
            browser=getattr(playwright,engine).launch(headless=True,**(
                {'executable_path':os.environ['KANDU_TEST_CHROME']} if engine=='chromium' else {}))
            expect.set_options(timeout=15000 if engine=='webkit' else 5000)
            base=f'http://127.0.0.1:{server.server_port}'
            for width,height in [(393,659),(393,852),(320,740),(600,852)]:
                for record in wordbook.list_words():
                    wordbook.delete(record['word'])
                context=browser.new_context(viewport=dict(width=width,height=height),device_scale_factor=3,
                                            is_mobile=True,has_touch=True,service_workers='block')
                page=context.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
                for endpoint in ['analyze','lookup','lookup-context','generate-vocab']:
                    page.route('**/'+endpoint,lambda route:route.fulfill(status=503,json=dict(error='Unexpected model request in passive tap test')))
                requests=[]
                page.on('request',lambda r:requests.append((r.method,r.url)))
                page.add_init_script("addEventListener('unhandledrejection',e=>{window.unhandled=(window.unhandled||[]).concat(String(e.reason))})")
                print(f'Checking {engine} {width}x{height}',flush=True)
                # A cold open acknowledges the tap and shares the initial request.
                held=[]
                page.route('**/vocab',lambda route:held.append(route))
                page.goto(base+'/#/read/sample/0')
                page.locator('#password').fill(WebTests.authcode);page.locator('#login-form button').click()
                expect(page.locator('#badge-4')).to_be_visible(timeout=20000)
                page.wait_for_function('() => state.reader?.vocabLoading!=null')
                page.locator('#badge-4').tap()
                expect(page.locator('#sheet-content')).to_contain_text('正在读取已有词表')
                page.locator('#sheet-close').tap()
                assert len(held)==1,'Initial load and a cold tap must share one request'
                held.pop().continue_();page.unroute('**/vocab')
                page.wait_for_function('() => state.reader?.vocab&&!state.reader.vocabLoading')
                expect(page.locator('#sheet')).not_to_be_visible()
                page.wait_for_function('() => cacheRequests.size===0')
                # Real warm taps must not touch IDB or rebuild the whole article.
                page.evaluate('''() => {
                    window.vocabWork={reads:0,index:0,highlight:0};
                    const get=ReadingCache.get,index=indexVocabHover,highlight=updateVocabHighlights;
                    ReadingCache.get=async function(path){if(path.endsWith('/vocab'))vocabWork.reads++;return get(path);};
                    indexVocabHover=function(...args){vocabWork.index++;return index(...args);};
                    updateVocabHighlights=function(...args){vocabWork.highlight++;return highlight(...args);};
                }''')
                for _ in range(3):
                    page.locator('#badge-4').tap();expect(page.locator('#sheet-content')).to_contain_text('第 5 段：坦率的')
                    page.locator('#sheet-close').tap()
                assert page.evaluate('vocabWork')==dict(reads=0,index=0,highlight=0)
                # Even a stalled cache revalidation must not hold the sheet open.
                page.evaluate('''() => {
                    const get=ReadingCache.get;window.restoreCacheGet=()=>{ReadingCache.get=get;};
                    ReadingCache.get=path=>path.endsWith('/vocab')?new Promise(resolve=>{
                        window.releaseVocabRead=()=>get(path).then(resolve);
                    }):get(path);
                    state.reader.vocabLoadedAt=0;
                }''')
                page.locator('#badge-4').tap()
                expect(page.locator('#sheet-content')).to_contain_text('第 5 段：坦率的')
                page.wait_for_function("() => typeof releaseVocabRead==='function'")
                assert page.evaluate('!!state.reader.vocabLoading')
                page.locator('#sheet-close').tap()
                page.evaluate('() => {releaseVocabRead();restoreCacheGet();}')
                page.wait_for_function('() => !state.reader.vocabLoading')
                # Identical fresh responses must preserve the existing index.
                page.evaluate('loadVocab(state.reader,{fresh:true})')
                assert page.evaluate('vocabWork.index')==0
                assert page.evaluate('vocabWork.highlight')==0
                writes_before=[r for r in requests if r[0]=='POST' and not r[1].endswith(('/progress','/reading-time','/login'))]
                before=tap(page,'candid')
                tip=page.locator('#vocab-hover')
                expect(tip).to_be_visible()
                expect(tip.locator('.hover-sense')).to_have_text('第 5 段：坦率的')
                expect(tip.locator('.hover-heading')).to_contain_text('kand-id')
                assert 'null' not in tip.inner_text(),'Absent optional fields must not render as text'
                expect(page.locator('#selection-tools')).not_to_be_visible()
                page.wait_for_timeout(400)
                expect(tip).to_be_visible()
                assert snapshot(page)==before
                assert page.evaluate('state.reader.hoverTouch&&state.reader.hoverVisible')
                bounds=tip.bounding_box()
                assert bounds['x']>=9 and bounds['x']+bounds['width']<=width-9,bounds
                assert bounds['y']>=0 and bounds['y']+bounds['height']<=height,bounds
                assert [r for r in requests if r[0]=='POST' and not r[1].endswith(('/progress','/reading-time','/login'))]==writes_before
                shot(page,f'mobile-vocab-{width}-{height}-{engine}')
                # A real touch on the collection button keeps the popup alive.
                tip.get_by_role('button',name='＋ 加入生词本',exact=True).tap()
                expect(tip.get_by_role('button',name='已加入生词本 ✓',exact=True)).to_be_disabled()
                record=wordbook.get('candid')
                assert record['source']['paragraph']==4 and record['sense']=='第 5 段：坦率的'
                assert snapshot(page)==before
                tip.get_by_role('button',name='关闭生词释义').tap();expect(tip).not_to_be_visible()
                # Tapping either half of a phrase uses its complete definition.
                for part in ['paper','over']:
                    tap(page,part)
                    expect(tip.locator('.hover-heading strong')).to_have_text('paper over')
                    expect(tip.locator('.hover-sense')).to_have_text('掩盖问题')
                    tip.get_by_role('button',name='选词 / 讲解',exact=True).tap()
                    expect(page.locator('#selection-tools')).to_be_visible()
                    assert page.evaluate('state.selection.quote')=='paper over'
                    page.get_by_role('button',name='关闭选词操作').tap()
                # Ordinary words retain the existing actions; scrolling dismisses hints.
                tap(page,'reader')
                expect(page.locator('#selection-tools')).to_be_visible()
                assert page.evaluate('state.selection.quote')=='reader'
                expect(tip).not_to_be_visible()
                page.get_by_role('button',name='关闭选词操作').tap()
                tap(page,'candid');expect(tip).to_be_visible()
                page.evaluate('scrollBy(0,60)');expect(tip).not_to_be_visible()
                # Rapid repeated taps on marked prose must never start analysis.
                point=word_point(page,'candid',4);snapshot(page)
                point=word_point(page,'candid',4,scroll=False)
                page.touchscreen.tap(point['x'],point['y']);page.touchscreen.tap(point['x'],point['y'])
                expect(tip).to_be_visible()
                tip.get_by_role('button',name='关闭生词释义').tap()
                # A pan/cancel/multitouch on an existing word must not show a hint.
                for gesture in ['pan','cancel','multitouch']:
                    page.evaluate('''({x,y,gesture})=>{
                        const p=document.querySelector('#para-4 p');
                        const send=(type,extra={})=>p.dispatchEvent(new PointerEvent(type,{bubbles:true,pointerType:'touch',
                            pointerId:7,isPrimary:true,button:0,clientX:x,clientY:y,...extra}));
                        send('pointerdown');
                        if(gesture==='pan')send('pointermove',{clientX:x+40});
                        if(gesture==='cancel')send('pointercancel');
                        if(gesture==='multitouch')send('pointerdown',{pointerId:8,isPrimary:false});
                        send('pointerup');
                    }''',dict(**point,gesture=gesture))
                    expect(tip).not_to_be_visible()
                # Search mark text nodes still resolve the correct word and sense.
                page.evaluate("findArticle(state.reader,'candid')")
                tap(page,'candid');expect(tip.locator('.hover-sense')).to_have_text('第 5 段：坦率的')
                page.keyboard.press('Escape');expect(tip).not_to_be_visible()
                # Real background changes are picked up without delaying the open
                # sheet, reopening it, or moving the article underneath it.
                response=page.evaluate('structuredClone(state.reader.vocab)')
                for section in response['vocab']['paragraphs']:
                    if section['index']==4:
                        section['entries'][0]['sense']='服务器更新后的本文含义'
                page.evaluate('''async()=>{await ReadingCache.stale();state.reader.vocabLoadedAt=0;}''')
                page.route('**/vocab',lambda route:held.append(route))
                page.locator('#badge-4').tap()
                expect(page.locator('#sheet-content')).to_contain_text('第 5 段：坦率的')
                page.wait_for_function('() => cacheRequests.size>0')
                before=snapshot(page)
                assert len(held)==1
                held.pop().fulfill(json=response)
                page.wait_for_function("() => state.reader.vocab.vocab.paragraphs.some(p=>p.index===4&&p.entries[0].sense==='服务器更新后的本文含义')")
                expect(page.locator('#sheet')).to_be_visible()
                assert snapshot(page)==before
                page.locator('#sheet-close').tap()
                tap(page,'candid');expect(tip.locator('.hover-sense')).to_have_text('服务器更新后的本文含义')
                assert not page.evaluate('window.unhandled||[]')
                assert not [r for r in requests if r[1].endswith(('/analyze','/lookup','/lookup-context','/generate-vocab'))]
                context.close()
            browser.close()
        assert not errors,errors
        print('MOBILE VOCAB PASS: cold feedback/cancel, zero warm IDB/index work, blocked cache, unchanged refresh, paragraph/phrase definitions, collection, ordinary words, scroll/search/viewport; no real models.')
    finally:
        server.shutdown();server.server_close();thread.join(timeout=5);WebTests.tearDownClass()


if __name__=='__main__':main()
