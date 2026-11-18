"""Render migrated PDF paragraphs and reuse sentence/vocabulary caches on two layouts."""
import os
from pathlib import Path
import threading
from unittest.mock import patch
from wsgiref.simple_server import make_server

from playwright.sync_api import sync_playwright, expect
from run_pdf_paragraph_tests import PdfParagraphTests
from run_web_tests import WebTests, config, store, core_dict
from run_vocab_hover_browser_tests import hover_word


def main():
    WebTests.setUpClass()
    fixture=PdfParagraphTests('test_page_continuation_and_real_breaks_and_article_boundary')
    fixture.setUp();fixture.fixture()
    meta=dict(fixture.meta,fileName='Synthetic PDF.pdf',lastModified=0)
    store.save_library([meta])
    sentence='What did Google have to show for it?'
    store.save_analysis(meta['id'],sentence,0,dict(translation='谷歌到底有什么成果？',
        context='团队与竞争的讨论。',grammarComponents=[dict(text=sentence,label='原句')]))
    server=make_server('127.0.0.1',0,WebTests.app)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    errors=[]
    try:
        with (sync_playwright() as playwright,patch.object(core_dict,'ensure_db'),
              patch.object(core_dict,'lookup_many',return_value={})):
            chrome=os.environ.get('KANDU_TEST_CHROME')
            browser=playwright.chromium.launch(headless=True,**({'executable_path':chrome} if chrome else {}))
            for width in [1440,390]:
                context=browser.new_context(viewport=dict(width=width,height=900))
                page=context.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
                responses=[]
                article_path=f'/api/books/{meta["id"]}/articles/0'
                page.on('response',lambda r:responses.append(r.json()) if r.url.endswith(article_path) else None)
                page.goto(f'http://127.0.0.1:{server.server_port}/#/read/{meta["id"]}/0')
                page.locator('#password').fill(WebTests.authcode);page.locator('#login-form button').click()
                expect(page.locator('.paragraph')).to_have_count(6)
                expect(page.locator('#para-1 p')).to_contain_text(sentence)
                expect(page.locator('#para-1 p')).to_contain_text('Disappointing ad sales')
                expect(page.locator('#para-2 p')).to_have_text('This is a real paragraph break.')
                expect(page.locator('#badge-1')).to_contain_text('2')
                if width==1440:
                    assert responses[0]['history']['paragraphIndex']==1,responses
                hover_word(page,'uneasy',1)
                expect(page.locator('#vocab-hover')).to_contain_text('anxious')
                page.mouse.move(1,1)
                # Select a word from the formerly split sentence and double-click.
                page.evaluate('''() => {
                    const p=document.querySelector('#para-1 p'),text=p.firstChild;
                    const at=text.textContent.indexOf('Google'),range=document.createRange();
                    range.setStart(text,at);range.setEnd(text,at+6);
                    getSelection().removeAllRanges();getSelection().addRange(range);
                    p.dispatchEvent(new MouseEvent('dblclick',{bubbles:true}));
                }''')
                expect(page.locator('.analysis-sentence')).to_have_text(sentence)
                expect(page.locator('.analysis-block').last).to_contain_text('谷歌到底有什么成果？')
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                if folder:=os.environ.get('KANDU_TEST_ARTIFACTS'):
                    target=Path(folder);target.mkdir(parents=True,exist_ok=True)
                    page.screenshot(path=str(target/f'pdf-paragraph-{width}.png'))
                context.close()
            browser.close()
        assert not errors,errors
        print('PDF paragraph browser regressions passed (desktop and narrow layout).')
    finally:
        server.shutdown();server.server_close();thread.join(timeout=5)
        WebTests.tearDownClass()


if __name__=='__main__':main()
