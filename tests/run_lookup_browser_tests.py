"""Selection geometry and contextual lookup on desktop and touch layouts."""
import json
import os
from pathlib import Path
import threading
import time
from unittest.mock import patch
from wsgiref.simple_server import make_server

from playwright.sync_api import sync_playwright, expect
from run_web_tests import WebTests, config, store, llm, core_dict
from run_mobile_reading_browser_tests import BrowserServer


def select_word(page, paragraph, at_bottom=False, quote='bound'):
    page.locator(f'#para-{paragraph} p').scroll_into_view_if_needed()
    page.evaluate('''({paragraph,bottom,quote}) => {
        const p=document.querySelector(`#para-${paragraph} p`);
        p.scrollIntoView({block:'center',behavior:'instant'});
        const walker=document.createTreeWalker(p,NodeFilter.SHOW_TEXT);
        let node;while(node=walker.nextNode()) {
            const start=node.textContent.indexOf(quote);if(start<0)continue;
            const range=document.createRange();range.setStart(node,start);range.setEnd(node,start+quote.length);
            if(bottom)scrollBy({top:range.getBoundingClientRect().bottom-innerHeight+30,behavior:'instant'});
            const selection=getSelection();selection.removeAllRanges();selection.addRange(range);break;
        }
    }''',dict(paragraph=paragraph,bottom=at_bottom,quote=quote))
    expect(page.locator('#selection-tools')).to_be_visible()
    page.wait_for_timeout(1100)
    metrics=page.evaluate('''() => {
        const word=getSelection().getRangeAt(0).getBoundingClientRect();
        const bar=document.querySelector('#selection-tools').getBoundingClientRect();
        return {left:bar.left,right:bar.right,top:bar.top,bottom:bar.bottom,width:innerWidth,height:innerHeight,
            gap:Math.min(Math.abs(bar.top-word.bottom),Math.abs(word.top-bar.bottom)),wordTop:word.top};
    }''')
    assert metrics['left']>=9 and metrics['right']<=metrics['width']-9,metrics
    assert metrics['top']>=9 and metrics['bottom']<=metrics['height']-9,metrics
    if page.evaluate("matchMedia('(pointer:coarse)').matches"):
        assert metrics['bottom']>=metrics['height']-11,metrics
    else:
        assert metrics['gap']<=11,metrics
        if at_bottom:assert metrics['bottom']<metrics['wordTop'],metrics
    return page.evaluate('''() => ({y:scrollY,progress:document.querySelector('#reader-progress-bar').value,
        paragraph:document.querySelector('#reader-paragraph').textContent})''')


def screenshot(page,name):
    if folder:=os.environ.get('KANDU_TEST_ARTIFACTS'):
        target=Path(folder);target.mkdir(parents=True,exist_ok=True)
        page.screenshot(path=str(target/(name+'.png')))


def main():
    WebTests.setUpClass()
    text=('A fully loaded, moon-bound Starship expends almost all of its fuel to get to low Earth orbit. '
          'So that Starship needs to refill its tank before it heads to the moon, requiring the launch of more and more refueling Starships. '
          'They paper over problems and fall short of expectations. ')
    article=dict(index=0,title='A trip to the moon',paragraphs=[text+f'Reading note {i}.' for i in range(16)])
    store._atomic_json_write(config.app_dir()/'cache/sample/content.json',dict(
        bookSize=WebTests.meta['size'],format='epub',articles=[article]))
    calls=[]
    def chat(cfg,system,user,**kwargs):
        context=json.loads(user);calls.append(context)
        assert context['word'] in ['bound', 'paper over', 'fall short of']
        assert ('moon-bound' if context['word']=='bound' else context['word']) in context['sentence']
        kwargs['on_delta'](50)
        time.sleep(.3)
        return json.dumps(dict(sense='驶向月球的；以月球为目的地的' if context['word']=='bound' else '短语的本文义',pos='形容词后缀',
            explanation='bound 与 moon 构成 moon-bound，表示目的地是月球，并非“界限”。',
            translation='一艘满载、驶向月球的星舰为了进入近地轨道，几乎耗尽了全部燃料。'),ensure_ascii=False)
    server=make_server('127.0.0.1',0,WebTests.app,server_class=BrowserServer)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    base=f'http://127.0.0.1:{server.server_port}'
    errors=[]
    try:
        with (sync_playwright() as playwright,patch.object(llm,'chat',side_effect=chat),
              patch.object(core_dict,'ensure_db'),patch.object(core_dict,'lookup_many',return_value={}),
              patch.object(core_dict,'lookup',side_effect=lambda word:dict(word='bound',lemma='bound',translation='界限；受约束的') if word=='bound' else {})):
            chrome=os.environ.get('KANDU_TEST_CHROME')
            engine=os.environ.get('KANDU_TEST_ENGINE','chromium')
            expect.set_options(timeout=15000 if engine=='webkit' else 5000)
            browser=getattr(playwright,engine).launch(headless=True,
                **({'executable_path':chrome} if chrome and engine=='chromium' else {}))
            for mobile in [False,True]:
                context=browser.new_context(viewport={'width':390 if mobile else 1440,'height':844 if mobile else 1000},
                    is_mobile=mobile,has_touch=mobile,service_workers='block')
                context.add_init_script("localStorage.setItem('kandu-touch-mode','select')")
                page=context.new_page();page.on('pageerror',lambda error:errors.append(str(error)))
                page.goto(base+'/#/read/sample/0')
                page.locator('#password').fill(WebTests.authcode);page.locator('#login-form button').click()
                expect(page.locator('.paragraph')).to_have_count(16)
                before=select_word(page,8,at_bottom=mobile)
                saved_before=store.load_reading_history('sample')['0']
                screenshot(page,'lookup-selection-phone' if mobile else 'lookup-selection-desktop')
                toolbar=page.locator('#selection-tools')
                lookup=toolbar.get_by_role('button',name='查词并收藏',exact=True)
                lookup.tap() if mobile else lookup.click()
                expect(page.locator('.lookup-context .context-sense')).to_have_text('驶向月球的；以月球为目的地的')
                expect(page.locator('.lookup-context .context-explanation')).to_contain_text('moon-bound')
                expect(page.locator('.lookup-context .quote')).to_contain_text('A fully loaded, moon-bound Starship')
                expect(page.locator('.lookup-context .context-translation')).to_contain_text('近地轨道')
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                screenshot(page,'lookup-meaning-phone' if mobile else 'lookup-meaning-desktop')
                page.locator('#sheet-close').click()
                after=page.evaluate('''() => ({y:scrollY,progress:document.querySelector('#reader-progress-bar').value,
                    paragraph:document.querySelector('#reader-paragraph').textContent})''')
                assert before==after,(before,after)
                saved_after=store.load_reading_history('sample')['0']
                assert (saved_before['paragraphIndex'],saved_before['paragraphOffset'])==(saved_after['paragraphIndex'],saved_after['paragraphOffset'])
                count=len(calls)
                select_word(page,8,at_bottom=mobile)
                lookup=page.locator('#selection-tools').get_by_role('button',name='查词并收藏',exact=True)
                lookup.tap() if mobile else lookup.click()
                expect(page.locator('.lookup-context .context-sense')).to_be_visible()
                assert len(calls)==count,'Same occurrence should use cached meaning'
                page.locator('#sheet-close').click()
                for phrase in ['paper over', 'fall short of']:
                    before_phrase=select_word(page,8,at_bottom=mobile,quote=phrase)
                    expect(page.locator('#selection-lookup')).to_be_visible()
                    lookup=page.locator('#selection-lookup')
                    lookup.tap() if mobile else lookup.click()
                    expect(page.locator('.lookup-context .context-sense')).to_have_text('短语的本文义')
                    assert store.load_reading_history('sample')['0']['title']==article['title']
                    page.locator('#sheet-close').click()
                    assert page.evaluate('scrollY')==before_phrase['y']
                select_word(page,8,at_bottom=mobile,quote='They paper over problems')
                expect(page.locator('#selection-lookup')).not_to_be_visible()
                # Even invoking the entry directly must not collect a sentence.
                count=len(calls)
                page.evaluate('lookupSelection(state.reader)')
                expect(page.locator('#sheet')).not_to_be_visible()
                assert len(calls)==count
                page.get_by_role('button',name='关闭选词操作').click()
                page.get_by_role('link',name='生词本',exact=True).click()
                expect(page.locator('.word-source .context-sense')).to_contain_text(['短语的本文义','短语的本文义','驶向月球的；以月球为目的地的'])
                context.close()
            assert not errors,errors
            browser.close()
            print(f'LOOKUP BROWSER PASS: {engine}, desktop + touch phone, single word and 2/3-word phrases, longer selections blocked, contextual meaning/translation, saved source, cached reuse, unchanged scroll and reading progress')
    finally:
        server.shutdown();server.server_close();thread.join(timeout=5)
        WebTests.tearDownClass()


if __name__=='__main__':
    main()
