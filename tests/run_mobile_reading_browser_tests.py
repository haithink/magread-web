"""Phone reading: tap words without a native selection; keep native selection optional."""
import os
from pathlib import Path
import threading
from socketserver import ThreadingMixIn
from unittest.mock import patch
from wsgiref.simple_server import make_server, WSGIServer

from playwright.sync_api import sync_playwright, expect
from run_web_tests import WebTests, config, store, core_dict
from run_vocab_hover_browser_tests import word_point


TEXT="🌙 A candid reader follows the argument. It certainly won’t paper over the uncertainties. "
PARAGRAPHS=[f'Note {i}. '+TEXT*3 for i in range(16)]


class BrowserServer(ThreadingMixIn,WSGIServer):
    daemon_threads=True


def snapshot(page):
    # WebKit can deliver scroll events after the tap promise resolves. Observe
    # settled frames on both sides of a dialog, instead of comparing stale UI.
    return page.evaluate('''() => new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(()=>
        resolve({y:scrollY,progress:document.querySelector('#reader-progress-bar').value}))))''')


def tap_word(page,word='candid',paragraph=4):
    point=word_point(page,word,paragraph)
    page.evaluate("y=>scrollBy({top:y-innerHeight/2,behavior:'instant'})",point['y'])
    snapshot(page)
    point=word_point(page,word,paragraph,scroll=False)
    assert 0<=point['y']<page.viewport_size['height'],point
    page.touchscreen.tap(point['x'],point['y'])
    expect(page.locator('#selection-tools')).to_be_visible()
    return point


def shot(page,name):
    if folder:=os.environ.get('KANDU_TEST_ARTIFACTS'):
        target=Path(folder);target.mkdir(parents=True,exist_ok=True)
        page.screenshot(path=str(target/(name+'.png')))


def main():
    WebTests.setUpClass()
    store._atomic_json_write(config.app_dir()/'cache/sample/content.json',dict(
        bookSize=WebTests.meta['size'],format='epub',articles=[
            dict(index=0,title='A calmer reading experience on your phone',paragraphs=PARAGRAPHS),
            dict(index=1,title='Another article',paragraphs=[TEXT])]))
    server=make_server('127.0.0.1',0,WebTests.app,server_class=BrowserServer)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    errors=[]
    try:
        with (sync_playwright() as playwright,patch.object(core_dict,'ensure_db'),
              patch.object(core_dict,'lookup_many',return_value={})):
            chrome=os.environ.get('KANDU_TEST_CHROME')
            engine=os.environ.get('KANDU_TEST_ENGINE','chromium')
            browser=getattr(playwright,engine).launch(headless=True,**({'executable_path':chrome} if chrome and engine=='chromium' else {}))
            expect.set_options(timeout=15000 if engine=='webkit' else 5000)
            for width,height in [(393,659),(393,852),(320,740),(600,852)]:
                context=browser.new_context(viewport=dict(width=width,height=height),screen=dict(width=width,height=852),
                    device_scale_factor=3,is_mobile=True,has_touch=True,service_workers='block')
                page=context.new_page();page.on('pageerror',lambda error:errors.append(str(error)))
                print(f'Checking {engine} {width}x{height}',flush=True)
                llm_calls=[];jobs={}
                def analyze(route):
                    llm_calls.append(route.request.post_data_json)
                    jobs['test']=dict(state='running',status='模型正在思考')
                    route.fulfill(status=202,json=dict(jobId='test'))
                page.route('**/api/books/sample/articles/0/analyze',analyze)
                page.route('**/api/jobs/test',lambda route:route.fulfill(json=jobs['test']))
                base=f'http://127.0.0.1:{server.server_port}'
                page.goto(base+'/#/read/sample/0')
                page.locator('#password').fill(WebTests.authcode);page.locator('#login-form button').click()
                expect(page.locator('#badge-4')).to_be_visible(timeout=20000)
                page.wait_for_function('() => state.reader?.ready&&state.reader.vocab')
                page.wait_for_timeout(150)
                expect(page.locator('#touch-reading-help')).to_contain_text('轻点单词')
                assert page.locator('#reader-body').evaluate("n=>getComputedStyle(n.querySelector('p')).userSelect")!='none'
                assert page.locator('.reader-toolbar').bounding_box()['height']<=145
                timer=page.locator('#reading-time-toggle')
                expect(timer).to_be_visible()
                assert timer.bounding_box()['height']>=44
                before=snapshot(page)
                timer_bounds=timer.bounding_box()
                page.touchscreen.tap(timer_bounds['x']+timer_bounds['width']/2,timer_bounds['y']+timer_bounds['height']/2)
                expect(page.get_by_role('button',name='继续计时',exact=True)).to_have_attribute('aria-pressed','true')
                expect(page.locator('#reader-reading-time')).to_contain_text('已暂停')
                assert snapshot(page)==before,(width,height,'pause',before,snapshot(page))
                page.get_by_role('button',name='继续计时',exact=True).tap()
                expect(timer).to_have_attribute('aria-pressed','false')
                # Extra controls do not permanently consume reading space or change its position.
                rect=page.locator('#reader-more-toggle').bounding_box()
                page.touchscreen.tap(rect['x']+rect['width']/2,rect['y']+rect['height']/2)
                expect(page.locator('#reader-options')).to_be_visible()
                before=snapshot(page);page.touchscreen.tap(rect['x']+rect['width']/2,rect['y']+rect['height']/2)
                expect(page.locator('#reader-options')).not_to_be_visible()
                assert snapshot(page)==before,(width,height,'more',before,snapshot(page))
                # Tapping creates only a local range/highlight, without querying/saving anything.
                point=tap_word(page);before=snapshot(page)
                assert page.evaluate('getSelection().isCollapsed')
                assert page.locator('#para-4 p').evaluate("n=>getComputedStyle(n).userSelect")!='none'
                assert page.evaluate('state.selection.quote')=='candid'
                expect(page.locator('#selection-lookup')).to_be_visible()
                assert page.evaluate("[...CSS.highlights.get('kandu-touch-selection')][0].toString()")=='candid'
                assert page.evaluate('state.selection.start')==len(PARAGRAPHS[4][:PARAGRAPHS[4].index('candid')])
                assert not llm_calls
                expect(page.locator('#sheet')).not_to_be_visible()
                bounds=page.locator('#selection-tools').bounding_box()
                assert bounds['x']>=9 and bounds['x']+bounds['width']<=width-9,bounds
                assert bounds['y']>=0 and bounds['y']+bounds['height']<=height,bounds
                # Select the sentence without invoking OS text selection or an LLM.
                page.locator('#selection-expand').tap()
                assert page.evaluate('state.selection.quote')=='🌙 A candid reader follows the argument.'
                expect(page.locator('#selection-lookup')).not_to_be_visible()
                # Copy replaces the browser callout without saving a phrase as a word.
                page.evaluate("() => Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async text=>{window.copiedText=text}}})")
                page.get_by_role('button',name='复制',exact=True).tap()
                page.wait_for_function("() => copiedText==='🌙 A candid reader follows the argument.'")
                assert page.evaluate('getSelection().isCollapsed')
                assert snapshot(page)==before
                shot(page,f'mobile-selection-{width}-{height}-{engine}')
                page.get_by_role('button',name='句子讲解',exact=True).tap()
                expect(page.locator('#sheet-wait')).to_be_visible()
                page.wait_for_function('() => state.activeJobs.size>0')
                assert llm_calls[-1]['sentence']=='🌙 A candid reader follows the argument.'
                assert page.evaluate("!CSS.highlights.has('kandu-touch-selection')")
                jobs['test']=dict(state='done',status='完成',result=dict(translation='一位坦率的读者。'))
                expect(page.locator('#sheet')).to_be_visible()
                assert snapshot(page)==before
                page.locator('#sheet-close').tap()
                # Prose callouts are suppressed; other page controls keep their menus.
                assert page.locator('#para-4 p').evaluate("n=>!n.dispatchEvent(new MouseEvent('contextmenu',{bubbles:true,cancelable:true}))")
                assert page.locator('#badge-4').evaluate("n=>n.dispatchEvent(new MouseEvent('contextmenu',{bubbles:true,cancelable:true}))")
                # Cancelled/moved/multitouch/long gestures must not select or start a request.
                for gesture in ['pan','cancel','multitouch','hold']:
                    page.evaluate('''({x,y,gesture})=>{
                        const p=document.querySelector('#para-4 p');
                        const send=(type,extra={})=>p.dispatchEvent(new PointerEvent(type,{bubbles:true,pointerType:'touch',
                            pointerId:7,isPrimary:true,button:0,clientX:x,clientY:y,...extra}));
                        send('pointerdown');
                        if(getComputedStyle(p).userSelect!=='none')throw Error('Press must suppress native selection');
                        if(gesture==='pan')send('pointermove',{clientX:x+40});
                        if(gesture==='cancel')send('pointercancel');
                        if(gesture==='multitouch')send('pointerdown',{pointerId:8,isPrimary:false});
                        if(gesture==='hold')return;
                        send('pointerup');
                    }''',dict(**point,gesture=gesture))
                    if gesture=='hold':
                        page.wait_for_timeout(600)
                        page.locator('#para-4 p').dispatch_event('pointerup',dict(pointerType='touch',pointerId=7,isPrimary=True,
                            button=0,clientX=point['x'],clientY=point['y']))
                    expect(page.locator('#selection-tools')).not_to_be_visible()
                    assert page.locator('#para-4 p').evaluate("n=>getComputedStyle(n).userSelect")!='none'
                assert len(llm_calls)==1
                # Real touch scrolling is still handled by the browser, without a lookup.
                if engine=='chromium':
                    cdp=context.new_cdp_session(page);scroll_before=page.evaluate('scrollY')
                    cdp.send('Input.dispatchTouchEvent',dict(type='touchStart',touchPoints=[dict(x=width/2,y=height*.75)]))
                    for fraction in [.65,.55,.45,.35]:
                        cdp.send('Input.dispatchTouchEvent',dict(type='touchMove',touchPoints=[dict(x=width/2,y=height*fraction)]))
                        page.wait_for_timeout(40)
                    cdp.send('Input.dispatchTouchEvent',dict(type='touchEnd',touchPoints=[]))
                    page.wait_for_timeout(250)
                    assert page.evaluate('scrollY')>scroll_before+100
                    expect(page.locator('#selection-tools')).not_to_be_visible()
                    assert len(llm_calls)==1
                    cdp.detach()
                # Tap-mode context/offset must reach lookup just as native selection does.
                lookup_calls=[]
                def lookup(route):
                    lookup_calls.append(route.request.post_data_json)
                    route.fulfill(json=dict(saved=dict(word='candid',sense='Frank'),
                        contextMeaning=dict(sense='本文中指坦率的',explanation='模拟本文解释'),canExplain=True))
                page.route('**/api/books/sample/articles/0/lookup',lookup)
                tap_word(page);before=snapshot(page)
                page.get_by_role('button',name='查词并收藏',exact=True).tap()
                expect(page.locator('.context-sense')).to_have_text('本文中指坦率的')
                assert lookup_calls==[dict(word='candid',paragraph=4,start=len(PARAGRAPHS[4][:PARAGRAPHS[4].index('candid')]))]
                page.locator('#sheet-close').tap();assert snapshot(page)==before,(engine,'lookup-close',before,snapshot(page))
                # Keep free text selection/copy available and remember the user's mode.
                page.locator('#reader-more-toggle').tap();page.locator('#touch-mode-toggle').tap()
                assert page.locator('#para-4 p').evaluate("n=>getComputedStyle(n).userSelect")!='none'
                assert page.locator('#para-4 p').evaluate("n=>!n.dispatchEvent(new MouseEvent('contextmenu',{bubbles:true,cancelable:true}))")
                native_point=word_point(page,'paper over',4)
                page.evaluate("y=>scrollBy({top:y-innerHeight/2,behavior:'instant'})",native_point['y'])
                page.evaluate('''()=>{
                    const p=document.querySelector('#para-4 p'),start=p.firstChild.textContent.indexOf('paper over'),range=document.createRange();
                    range.setStart(p.firstChild,start);range.setEnd(p.firstChild,start+10);
                    getSelection().removeAllRanges();getSelection().addRange(range);
                }''')
                expect(page.locator('#selection-tools')).to_be_visible()
                assert page.evaluate('state.selection.quote')=='paper over'
                expect(page.locator('#selection-lookup')).not_to_be_visible()
                expect(page.get_by_role('button',name='句子讲解',exact=True)).to_be_visible()
                assert page.locator('#selection-tools').bounding_box()['y']>height-200
                shot(page,f'mobile-native-selection-{width}-{height}-{engine}')
                page.reload();expect(page.locator('#touch-reading-help')).to_contain_text('长按文字')
                page.locator('#reader-more-toggle').tap();page.locator('#touch-mode-toggle').tap()
                # Search markup stays readable and tappable, with codepoint-correct offsets.
                page.locator('#reader-more-toggle').tap();page.get_by_role('button',name='查找',exact=True).tap()
                page.get_by_label('查找原文').fill('candid')
                # Dismiss the keyboard before navigating the prose. Otherwise
                # WebKit may scroll a still-focused search field back into view
                # during our programmatic scroll, leaving tap coordinates offscreen.
                page.get_by_label('查找原文').blur()
                tap_word(page)
                assert page.evaluate('state.selection.quote')=='candid'
                page.get_by_role('button',name='关闭选词操作').tap()
                assert page.evaluate("!CSS.highlights.has('kandu-touch-selection')")
                # Repeated taps on an ordinary word keep its lookup available;
                # neither touch taps nor a synthesized dblclick invoke the model.
                calls_before=len(llm_calls)
                point=tap_word(page,'uncertainties')
                expect(page.locator('#selection-lookup')).to_be_visible()
                assert len(llm_calls)==calls_before
                page.touchscreen.tap(point['x'],point['y'])
                expect(page.locator('#selection-lookup')).to_be_visible()
                page.locator('#para-4 p').dispatch_event('dblclick')
                page.wait_for_timeout(100)
                assert len(llm_calls)==calls_before
                expect(page.locator('#sheet')).not_to_be_visible()
                page.get_by_role('button',name='关闭选词操作').tap()
                # Dialogs and inputs remain usable when a mobile keyboard shrinks the visual viewport.
                page.get_by_role('button',name='与 AI 讨论',exact=True).tap()
                input=page.get_by_role('textbox',name='向 AI 提问');input.fill('Why?')
                assert input.evaluate('n=>parseFloat(getComputedStyle(n).fontSize)')>=16
                page.evaluate("Object.defineProperty(visualViewport,'height',{value:360,configurable:true});visualViewport.dispatchEvent(new Event('resize'))")
                bounds=page.locator('#sheet').bounding_box()
                assert bounds['y']>=0 and bounds['y']+bounds['height']<=360,bounds
                shot(page,f'mobile-keyboard-{width}-{height}-{engine}')
                page.evaluate("delete visualViewport.height;visualViewport.dispatchEvent(new Event('resize'))")
                page.locator('#sheet-close').tap()
                # Rotation preserves the preference, range positioning and reading location.
                page.set_viewport_size(dict(width=852,height=393))
                page.evaluate("document.documentElement.style.setProperty('--safe-left','50px');document.documentElement.style.setProperty('--safe-right','50px')")
                tap_word(page)
                bounds=page.locator('#selection-tools').bounding_box()
                assert bounds['x']>=60 and bounds['x']+bounds['width']<=792,bounds
                assert bounds['y']>=0 and bounds['y']+bounds['height']<=393,bounds
                shot(page,f'mobile-landscape-{width}-{height}-{engine}')
                page.evaluate("document.documentElement.style.removeProperty('--safe-left');document.documentElement.style.removeProperty('--safe-right')")
                page.set_viewport_size(dict(width=width,height=height))
                page.goto(base+'/#/read/sample/1');expect(page.locator('.reader-head h1')).to_have_text('Another article')
                assert page.evaluate("!CSS.highlights.has('kandu-touch-selection')")
                page.goto(base+'/#/library');expect(page.locator('.book-card')).to_have_count(1)
                shot(page,f'mobile-library-{width}-{height}-{engine}')
                page.get_by_role('link',name='设置',exact=True).tap()
                expect(page.get_by_role('heading',name='阅读设置')).to_be_visible()
                assert page.locator('input').first.evaluate('n=>parseFloat(getComputedStyle(n).fontSize)')>=16
                for target in [base+'/#/book/sample',base+'/#/wordbook',base+'/#/reading-stats']:
                    page.goto(target);expect(page.locator('.loading')).to_have_count(0)
                    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth'),target
                context.close()
            browser.close()
        assert not errors,errors
        print('MOBILE READING PASS: iPhone 15 Pro 393x852 at 3x + 320/600px, tap/whole sentence/native selection, gestures, search, mode persistence, rotation, keyboard, nonblocking results and core page layouts; no real models.')
    except BaseException:
        print('PAGE ERRORS:',errors,flush=True)
        raise
    finally:
        server.shutdown();server.server_close();thread.join(timeout=5);WebTests.tearDownClass()


if __name__=='__main__':main()
