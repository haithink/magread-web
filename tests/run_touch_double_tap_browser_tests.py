"""Real phone taps: passive word lookup, sentence shortcuts and gesture guards."""
import os
import threading
from unittest.mock import patch
from wsgiref.simple_server import make_server

from playwright.sync_api import sync_playwright, expect
from run_web_tests import WebTests, store, gen, core_dict
from run_mobile_reading_browser_tests import BrowserServer, snapshot
from run_vocab_hover_browser_tests import word_point


PARAGRAPHS = [
    '🌙 A candid reader learns. It certainly won’t paper over the uncertainties.',
    'Another candid explorer waits. A patient reader studies the argument.',
    'The reader pauses. A reader then explores the second sentence.',
    *['An ordinary paragraph allows the reader to scroll safely. ' * 3 for _ in range(12)],
]


def place(page, word, paragraph=0, last=False):
    point = word_point(page, word, paragraph)
    page.evaluate("y=>scrollBy({top:y-innerHeight/2,behavior:'instant'})", point['y'])
    snapshot(page)
    if last:
        return page.evaluate('''({word,paragraph}) => {
            const p=document.querySelector(`#para-${paragraph} p`),range=document.createRange();
            const at=p.textContent.lastIndexOf(word);range.setStart(p.firstChild,at);range.setEnd(p.firstChild,at+word.length);
            const r=range.getClientRects()[0];return {x:r.left+r.width/2,y:r.top+r.height/2};
        }''', dict(word=word, paragraph=paragraph))
    return word_point(page, word, paragraph, scroll=False)


def main():
    WebTests.setUpClass()
    store._atomic_json_write(store._cache_dir('sample') / 'content.json', dict(bookSize=WebTests.meta['size'],
        format='epub', articles=[dict(index=0, title='Touch gestures', paragraphs=PARAGRAPHS)]))
    store.save_vocab('sample', dict(articleIndex=0, level='cet4', titleEntries=[], idioms=[],
        paragraphs=[dict(index=i, entries=[dict(word='candid', sense='坦率的'),
            dict(word='paper over', sense='掩盖问题')]) for i in range(len(PARAGRAPHS))],
        processedParagraphs=[-1, *range(len(PARAGRAPHS))], phraseProcessedParagraphs=[-1, *range(len(PARAGRAPHS))],
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
            for standalone in (False, True):
                context = browser.new_context(viewport=dict(width=393, height=852), device_scale_factor=3,
                    is_mobile=True, has_touch=True, service_workers='block')
                page = context.new_page()
                page.set_default_timeout(15000)
                if standalone:
                    page.add_init_script("Object.defineProperty(navigator,'standalone',{value:true})")
                errors, calls, jobs, lookups = [], [], {}, []
                page.on('pageerror', lambda error: errors.append(str(error)))

                def analyze(route):
                    calls.append(route.request.post_data_json)
                    job = f'double-{len(calls)}'
                    jobs[job] = dict(state='running', status='模型正在思考')
                    route.fulfill(status=202, json=dict(jobId=job))

                page.route('**/api/books/sample/articles/0/analyze', analyze)
                page.route('**/api/jobs/double-*', lambda route: route.fulfill(json=jobs[route.request.url.rsplit('/', 1)[-1]]))
                page.on('request', lambda request: lookups.append(request.url) if '/lookup' in request.url else None)
                page.goto(f'http://127.0.0.1:{server.server_port}/#/read/sample/0')
                page.locator('#password').fill(WebTests.authcode)
                page.locator('#login-form button').click()
                page.wait_for_function('() => state.reader?.ready&&state.reader.vocab')
                print(f'DOUBLE TAP {engine}, standalone={standalone}', flush=True)
                # Single tap remains immediate and passive, on either word kind.
                point = place(page, 'reader')
                page.touchscreen.tap(**point)
                expect(page.locator('#selection-lookup')).to_be_visible()
                assert not calls and not lookups
                page.get_by_role('button', name='关闭选词操作').tap()
                point = place(page, 'candid')
                page.touchscreen.tap(**point)
                expect(page.locator('#vocab-hover .hover-sense')).to_have_text('坦率的')
                assert not calls and not lookups
                page.get_by_role('button', name='关闭生词释义').tap()

                for word, paragraph, last, sentence in [
                    ('reader', 0, False, '🌙 A candid reader learns.'),
                    ('candid', 1, False, 'Another candid explorer waits.'),
                    ('paper', 0, False, 'It certainly won’t paper over the uncertainties.'),
                    ('reader', 2, True, 'A reader then explores the second sentence.'),
                ]:
                    point = place(page, word, paragraph, last)
                    before, count = snapshot(page), len(calls)
                    page.touchscreen.tap(**point)
                    page.touchscreen.tap(**point)
                    expect(page.locator('#sheet-wait')).to_be_visible()
                    page.wait_for_function('() => state.activeJobs.size>0')
                    assert len(calls) == count + 1, calls
                    assert calls[-1] == dict(sentence=sentence, paragraph=paragraph), calls[-1]
                    # Native dblclick and a third tap cannot duplicate the job.
                    page.locator(f'#para-{paragraph} p').dispatch_event('dblclick')
                    page.touchscreen.tap(**point)
                    page.wait_for_timeout(100)
                    assert len(calls) == count + 1
                    assert not lookups
                    jobs[f'double-{len(calls)}'] = dict(state='done', status='完成', result=dict(translation='模拟句子讲解'))
                    expect(page.locator('#sheet')).to_be_visible()
                    expect(page.locator('#sheet-content')).to_contain_text(sentence)
                    expect(page.locator('#vocab-hover')).not_to_be_visible()
                    page.locator('#sheet-close').tap()
                    assert snapshot(page) == before

                # Quickly moving to another word is still two single lookups.
                first = place(page, 'reader', 1)
                second = word_point(page, 'argument', 1, scroll=False)
                page.touchscreen.tap(**first)
                page.touchscreen.tap(**second)
                expect(page.locator('#selection-lookup')).to_be_visible()
                assert page.evaluate('state.selection.quote') == 'argument'
                assert len(calls) == 4
                page.get_by_role('button', name='关闭选词操作').tap()

                # A pan, canceled pointer, second finger or long press between
                # taps must break the double-tap candidate.
                for gesture in ('pan', 'cancel', 'multitouch', 'hold'):
                    point = place(page, 'reader')
                    page.touchscreen.tap(**point)
                    page.evaluate('''({x,y,gesture}) => {
                        const p=document.querySelector('#para-0 p');
                        const send=(type,extra={})=>p.dispatchEvent(new PointerEvent(type,{bubbles:true,
                            pointerType:'touch',pointerId:7,isPrimary:true,button:0,clientX:x,clientY:y,...extra}));
                        send('pointerdown');
                        if(gesture==='pan')send('pointermove',{clientX:x+40});
                        if(gesture==='cancel')send('pointercancel');
                        if(gesture==='multitouch')send('pointerdown',{pointerId:8,isPrimary:false});
                        if(gesture==='hold')return;
                        send('pointerup');
                    }''', dict(**point, gesture=gesture))
                    if gesture == 'hold':
                        page.wait_for_timeout(600)
                        page.locator('#para-0 p').dispatch_event('pointerup', dict(pointerType='touch', pointerId=7,
                            isPrimary=True, button=0, clientX=point['x'], clientY=point['y']))
                    page.touchscreen.tap(**point)
                    expect(page.locator('#selection-lookup')).to_be_visible()
                    assert len(calls) == 4
                    page.get_by_role('button', name='关闭选词操作').tap()
                assert not errors, errors
                context.close()
            browser.close()
            print(f'TOUCH DOUBLE TAP PASS: {engine}, browser/standalone, ordinary/known/phrase words, repeated-word sentence boundary, single taps, one request, gesture guards and scroll position.', flush=True)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        WebTests.tearDownClass()


if __name__ == '__main__':
    main()
