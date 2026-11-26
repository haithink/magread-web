"""Render legacy cached phonetics on desktop and iPhone, without network/LLM lookup."""
import os
import threading
from unittest.mock import patch
from wsgiref.simple_server import make_server

from playwright.sync_api import sync_playwright, expect
from run_web_tests import WebTests, config, store, core_dict, gen
from run_mobile_reading_browser_tests import BrowserServer
from run_vocab_hover_browser_tests import word_point, hover_word
from run_phonetic_tests import LEGACY, CORRECT


def main():
    WebTests.setUpClass()
    art = dict(index=0, title='A glutamate study', paragraphs=['Glutamate is an amino acid. We study glutamate and its function.']*3)
    store._atomic_json_write(store._cache_dir('sample')/'content.json',dict(
        bookSize=WebTests.meta['size'],format='epub',articles=[art]))
    legacy = dict(articleIndex=0,level='cet4',titleEntries=[],idioms=[],
        paragraphs=[dict(index=i, entries=[dict(word='glutamate',phonetic=LEGACY,pos='n.',sense='An amino acid',note='x^2')]) for i in range(3)],
        processedParagraphs=[-1,0,1,2],phraseProcessedParagraphs=[-1,0,1,2],coverageVersion=gen.VOCAB_COVERAGE_VERSION)
    store._atomic_json_write(store._cache_dir('sample')/'vocab_0_cet4.json',legacy)
    server=make_server('127.0.0.1',0,WebTests.app,server_class=BrowserServer)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    errors=[]
    try:
        with patch.object(core_dict,'ensure_db'),patch.object(core_dict,'lookup_many',return_value={}),sync_playwright() as p:
            engine=os.environ.get('KANDU_TEST_ENGINE','chromium')
            browser=getattr(p,engine).launch(headless=True,**({'executable_path':os.environ['KANDU_TEST_CHROME']} if engine=='chromium' else {}))
            expect.set_options(timeout=30000)
            for mobile in (False,True):
                context=browser.new_context(viewport=dict(width=393 if mobile else 1280,height=852),
                    is_mobile=mobile,has_touch=mobile,service_workers='block')
                page=context.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
                page.goto(f'http://127.0.0.1:{server.server_port}/#/read/sample/0')
                page.locator('#password').fill(WebTests.authcode);page.locator('#login-form button').click()
                page.wait_for_function('() => state.reader?.vocab&&!state.reader.vocabLoading')
                # Seed precisely the old value into the persistent client cache,
                # then block its network route to prove existing caches work.
                page.evaluate('''async value=>{
                    const path=baseFor(state.reader)+'/vocab';
                    const data={...state.reader.vocab,vocab:value};
                    await ReadingCache.put(path,data,{group:baseFor(state.reader)});
                    state.reader.vocab=null;await loadVocab(state.reader);
                }''', legacy)
                page.route('**/vocab',lambda route:route.abort())
                page.evaluate('''value=>{
                    const item=state.reader.vocab.vocab.paragraphs[0].entries[0];
                    if(item.phonetic!==value)throw Error('Expected raw legacy client cache');
                }''',LEGACY)
                page.locator('#badge-0').click()
                expect(page.locator('#sheet-content .phonetic').first).to_contain_text(CORRECT)
                expect(page.locator('#sheet-content .phonetic').first).not_to_contain_text('^')
                expect(page.locator('#sheet-content')).to_contain_text('x^2')
                page.locator('#sheet-close').click()
                if mobile:
                    word_point(page,'glutamate',0)
                    page.wait_for_timeout(100)
                    point=word_point(page,'glutamate',0,scroll=False)
                    page.touchscreen.tap(point['x'],point['y'])
                else:
                    hover_word(page,'glutamate',0)
                expect(page.locator('#vocab-hover')).to_be_visible()
                expect(page.locator('#vocab-hover .hover-heading')).to_contain_text(CORRECT)
                # Shared wordbook/sentence entries also render old cached values.
                page.evaluate('''entry=>{
                    const node=entryNode(entry);node.id='phonetic-fixture';document.body.append(node);
                }''',dict(word='glutamate',phonetic=LEGACY,sense='x^2'))
                expect(page.locator('#phonetic-fixture .phonetic')).to_contain_text(CORRECT)
                expect(page.locator('#phonetic-fixture .entry-sense')).to_have_text('x^2')
                context.close()
            browser.close()
        assert not errors, errors
        print('PHONETIC BROWSER PASS: desktop + iPhone; legacy client cache, paragraph list, hover/tap, shared entries; other carets unchanged')
    finally:
        server.shutdown();server.server_close();thread.join(timeout=5)
        WebTests.tearDownClass()


if __name__=='__main__':
    main()
