"""Long podcast: continuous reading, bounded highlights and asynchronous analysis."""
import os
from pathlib import Path
import threading
import time
from unittest.mock import patch
from wsgiref.simple_server import make_server

from playwright.sync_api import sync_playwright, expect
from run_web_tests import WebTests, store, gen, core_dict
from run_mobile_reading_browser_tests import BrowserServer


TEXT = ('A candid reader can paper over uncertainties while the explorer charts a path. '
        'The discussion considers how learning systems explain the world and help us think. ')
PARAGRAPHS = [f'Paragraph {i}. ' + TEXT * 3 for i in range(781)]


def main():
    WebTests.setUpClass()
    store._atomic_json_write(store._cache_dir('sample') / 'content.json', dict(bookSize=WebTests.meta['size'],
        format='epub', articles=[dict(index=0, title='Long podcast reading', paragraphs=PARAGRAPHS)]))
    store.save_vocab('sample', dict(articleIndex=0, level='cet4', titleEntries=[], idioms=[],
        paragraphs=[dict(index=i, entries=[dict(word=w, sense='模拟释义')
            for w in ('candid', 'paper over', 'uncertainties', 'explorer')]) for i in range(781)],
        processedParagraphs=[-1, *range(781)], phraseProcessedParagraphs=[-1, *range(781)],
        coverageVersion=gen.VOCAB_COVERAGE_VERSION, phraseCoverageVersion=gen.PHRASE_COVERAGE_VERSION))
    server = make_server('127.0.0.1', 0, WebTests.app, server_class=BrowserServer)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with (sync_playwright() as playwright, patch.object(core_dict, 'ensure_db'),
              patch.object(core_dict, 'lookup_many', return_value={})):
            engine = os.environ.get('KANDU_TEST_ENGINE', 'webkit')
            browser = getattr(playwright, engine).launch(headless=True, **(
                {'executable_path': os.environ['KANDU_TEST_CHROME']} if engine == 'chromium' else {}))
            context = browser.new_context(viewport=dict(width=393, height=662), device_scale_factor=3,
                is_mobile=True, has_touch=True, service_workers='block')
            page = context.new_page()
            page.set_default_timeout(15000)
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            jobs = dict(state='running', status='模型正在思考')
            page.route('**/api/books/sample/articles/0/analyze', lambda route: route.fulfill(status=202, json=dict(jobId='long-reader')))
            page.route('**/api/jobs/long-reader', lambda route: route.fulfill(json=jobs))
            page.goto(f'http://127.0.0.1:{server.server_port}/#/read/sample/0')
            page.locator('#password').fill(WebTests.authcode)
            page.locator('#login-form button').click()
            page.wait_for_function('() => state.reader?.ready&&state.reader.vocab', timeout=45000)
            print('LONG READY', flush=True)
            assert page.evaluate('state.reader.hoverWords.size') == 782
            # All source words remain indexed, but only nearby paragraphs are
            # handed to the renderer. Exercise scroll jumps, continuous scroll,
            # changing browser toolbar height and landscape before any AI call.
            for width, height in [(393, 662), (393, 852), (720, 350)]:
                page.set_viewport_size(dict(width=width, height=height))
                for paragraph in [0, 89, 90, 180, 400, 780, 90]:
                    page.evaluate('(i) => {document.querySelector(`#para-${i}`).scrollIntoView();scrollBy(0,-160);}', paragraph)
                    page.wait_for_timeout(80)
                    for _ in range(3):
                        page.evaluate('() => scrollBy(0,100)')
                        page.wait_for_timeout(40)
                    snapshot = page.evaluate('''() => {
                        const reader=state.reader,highlight=CSS.highlights.get('kandu-vocabulary');
                        const active=new Set([...highlight].map(r=>Number(r.startContainer.parentElement.closest('.paragraph').dataset.index)));
                        const toolbar=document.querySelector('.reader-toolbar').getBoundingClientRect().bottom;
                        const visible=reader.paragraphNodes.filter(n=>{const r=n.querySelector('p').getBoundingClientRect();return r.bottom>toolbar&&r.top<innerHeight;});
                        return {ranges:highlight.size,window:reader.vocabHighlightWindow,
                            visible:visible.map(n=>Number(n.dataset.index)),active:[...active],
                            text:document.querySelector(`#para-${reader.vocabHighlightWindow.start} p`).textContent};
                    }''')
                    assert 0 < snapshot['ranges'] < 300, snapshot
                    assert snapshot['window']['end'] - snapshot['window']['start'] < 20, snapshot
                    assert snapshot['visible'] and set(snapshot['visible']) <= set(snapshot['active']), snapshot
                    assert snapshot['text'] == PARAGRAPHS[snapshot['window']['start']]
                    if paragraph == 90 and (directory := os.environ.get('KANDU_TEST_ARTIFACTS')):
                        folder = Path(directory)
                        folder.mkdir(parents=True, exist_ok=True)
                        page.screenshot(path=str(folder / f'long-reader-scroll-{engine}-{width}-{height}.png'))
            page.set_viewport_size(dict(width=393, height=662))
            print('CONTINUOUS SCROLL PASS: all visible paragraphs highlighted, fewer than 300 active ranges', flush=True)
            # Read well below the fold, then request a native-selected sentence.
            page.evaluate("() => {document.querySelector('#para-80').scrollIntoView();scrollBy(0,-260);}")
            page.wait_for_timeout(300)
            before = page.evaluate('scrollY')
            ranges = page.evaluate("CSS.highlights.get('kandu-vocabulary')?.size||0")
            assert 0 < ranges < 300, ranges
            page.evaluate("window.longHighlight=CSS.highlights.get('kandu-vocabulary')")
            print('HIGHLIGHTS', ranges, flush=True)
            page.evaluate('''() => {
                const p=document.querySelector('#para-80 p'),r=document.createRange();
                const text=p.firstChild.textContent,at=text.indexOf('The discussion');
                r.setStart(p.firstChild,at);r.setEnd(p.firstChild,text.indexOf('.',at)+1);
                getSelection().removeAllRanges();getSelection().addRange(r);
            }''')
            page.get_by_role('button', name='句子讲解', exact=True).tap()
            expect(page.locator('.sheet-wait-status')).to_contain_text('模型正在思考')
            print('LONG WAITING', flush=True)
            # A stalled local write must not hold the result hostage.
            page.evaluate('''() => {
                window.originalCachePut=ReadingCache.put;
                ReadingCache.put=(key,...args)=>key.startsWith('analysis:')?new Promise(()=>{}):originalCachePut(key,...args);
            }''')
            started = time.monotonic()
            jobs.update(state='done', status='已完成', result=dict(translation='长文中的句子讲解。',
                grammarComponents=[dict(text='The discussion', label='主语')]))
            expect(page.locator('#sheet')).to_be_visible(timeout=15000)
            expect(page.locator('#sheet-content')).to_contain_text('长文中的句子讲解。')
            assert not page.evaluate("CSS.highlights.has('kandu-vocabulary')")
            assert page.evaluate('state.reader.highlightsSuspended')
            if directory := os.environ.get('KANDU_TEST_ARTIFACTS'):
                folder = Path(directory)
                folder.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(folder / f'long-reader-result-{engine}.png'))
            print('LONG RESULT SECONDS', round(time.monotonic()-started, 3), flush=True)
            page.locator('#sheet-close').tap()
            expect(page.locator('#sheet')).not_to_be_visible()
            page.wait_for_function("() => CSS.highlights.get('kandu-vocabulary')===longHighlight")
            assert page.evaluate('longHighlight.size') == ranges
            assert abs(page.evaluate('scrollY') - before) < 3
            page.locator('#badge-80').tap()
            expect(page.locator('#sheet')).to_be_visible()
            page.locator('#sheet-close').tap()
            page.wait_for_function("() => CSS.highlights.get('kandu-vocabulary')===longHighlight")
            # A pending restore may run only after the NEXT modal has also closed.
            page.evaluate("() => {closeSheet();centerSheet();}")
            page.wait_for_timeout(100)
            assert not page.evaluate("CSS.highlights.has('kandu-vocabulary')")
            page.locator('#sheet-close').tap()
            page.wait_for_function("() => CSS.highlights.get('kandu-vocabulary')===longHighlight")
            # A vocabulary revalidation while modal must not register ranges or
            # restore an outdated snapshot when the user closes the sheet.
            page.locator('#badge-80').tap()
            page.evaluate('''() => {
                const value=structuredClone(state.reader.vocab);
                value.vocab.paragraphs.forEach(p=>p.entries=p.entries.filter(e=>e.word!=='candid'));
                applyVocab(state.reader,value);
            }''')
            assert not page.evaluate("CSS.highlights.has('kandu-vocabulary')")
            page.locator('#sheet-close').tap()
            page.wait_for_function("() => CSS.highlights.has('kandu-vocabulary')")
            assert page.evaluate("CSS.highlights.get('kandu-vocabulary').size") < ranges
            assert page.evaluate("CSS.highlights.get('kandu-vocabulary')!==longHighlight")
            assert abs(page.evaluate('scrollY') - before) < 3
            # A viewport change while the modal is open must rebuild the nearby
            # window, rather than restore ranges from the previous viewport.
            page.locator('#badge-80').tap()
            page.set_viewport_size(dict(width=720, height=350))
            page.evaluate('() => document.querySelector("#para-500").scrollIntoView()')
            page.locator('#sheet-close').tap()
            page.wait_for_function("() => CSS.highlights.has('kandu-vocabulary')")
            assert page.evaluate('state.reader.vocabHighlightWindow.start') > 480
            assert page.evaluate("CSS.highlights.get('kandu-vocabulary').size") < 300
            assert not errors, errors
            context.close()
            browser.close()
            print(f'LONG READING PASS: {engine}, 781 paragraphs, 9372 source matches, bounded painting, continuous scroll, resize, analysis, modal close, position and blocked cache.', flush=True)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        WebTests.tearDownClass()


if __name__ == '__main__':
    main()
