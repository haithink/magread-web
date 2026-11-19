"""Cached vocabulary hover: real pointer geometry, lifecycle and reading clock."""
import os
from pathlib import Path
import threading
from unittest.mock import patch
from wsgiref.simple_server import make_server

from playwright.sync_api import sync_playwright, expect
from browser_helpers import reader_button
from run_web_tests import WebTests, config, store, gen, core_dict, wordbook


def word_point(page, word, paragraph=0, scroll=True):
    return page.evaluate('''({word,paragraph,scroll}) => {
        const p=document.querySelector(paragraph===-1?'.reader-head h1':`#para-${paragraph} p`);
        if(scroll)p.scrollIntoView({block:'center',behavior:'instant'});
        const start=p.textContent.indexOf(word),end=start+word.length;
        if(start<0)throw Error('Missing fixture word');
        const walker=document.createTreeWalker(p,NodeFilter.SHOW_TEXT),range=document.createRange();
        let offset=0,node,started=false;
        while(node=walker.nextNode()) {
            const next=offset+node.length;
            if(!started&&start<next){range.setStart(node,start-offset);started=true;}
            if(started&&end<=next){range.setEnd(node,end-offset);break;}offset=next;
        }
        const r=range.getClientRects()[0];return {x:r.left+r.width/2,y:r.top+r.height/2};
    }''', dict(word=word, paragraph=paragraph, scroll=scroll))


def hover_word(page, word, paragraph=0, wait=True):
    page.mouse.move(1, 1)
    expect(page.locator('#vocab-hover')).not_to_be_visible()
    point = word_point(page, word, paragraph)
    page.wait_for_timeout(80)  # Finish scroll events before starting the dwell.
    point = word_point(page, word, paragraph, scroll=False)
    page.mouse.move(point['x'], point['y'])
    if wait:
        expect(page.locator('#vocab-hover')).to_be_visible(timeout=2500)
    return point


def snapshot(page):
    return page.evaluate('''() => ({y:scrollY,progress:document.querySelector('#reader-progress-bar').value,
        paragraph:document.querySelector('#reader-paragraph').textContent})''')


def check_hover_collection(page, article, entry):
    """Click the real card, including a failed save, retry and in-flight double click."""
    page.evaluate("location.hash='#/read/sample/0'")
    expect(page.locator('#badge-0')).to_contain_text('6')
    page.evaluate('loadVocab(state.reader,{fresh:true})')
    hover_word(page,'candid')
    tip=page.locator('#vocab-hover')
    collect=tip.get_by_role('button',name='＋ 加入生词本',exact=True)
    expect(collect).to_be_enabled()
    before=snapshot(page)
    # Move through the gap into the card and keep the pointer there long enough
    # to catch accidental close timers before clicking.
    collect.hover()
    page.wait_for_timeout(400)
    expect(tip).to_be_visible()
    held=[]
    def hold(route):
        held.append(route)
    page.route('**/collect',hold)
    collect.click()
    pending=tip.get_by_role('button',name='正在加入…',exact=True)
    expect(pending).to_be_disabled()
    expect(tip).to_be_visible()
    assert len(held)==1
    held.pop().fulfill(status=503,json={'error':'模拟收藏失败'})
    retry=tip.get_by_role('button',name='重试加入生词本',exact=True)
    expect(retry).to_be_enabled()
    assert not wordbook.get('candid')
    retry.click()
    expect(pending).to_be_disabled()
    box=pending.bounding_box()
    page.mouse.click(box['x']+box['width']/2,box['y']+box['height']/2)
    assert len(held)==1,'A repeated click must not send another collection request'
    held.pop().continue_()
    expect(tip.get_by_role('button',name='已加入生词本 ✓',exact=True)).to_be_disabled()
    page.unroute('**/collect',hold)
    record=wordbook.get('candid')
    for field in ['sense','phonetic','root','note']:
        assert record[field]==entry[field],(field,record)
    assert record['source']['bookId']=='sample' and record['source']['articleIndex']==0
    assert record['source']['paragraph']==0 and record['source']['context']==article['paragraphs'][0]
    assert snapshot(page)==before,'Collecting must not move the article or reading position'
    assert page.locator('#para-0 p').text_content()==article['paragraphs'][0]
    hover_word(page,'candid',1)
    expect(tip.get_by_role('button',name='已在生词本 ✓',exact=True)).to_be_disabled()
    hover_word(page,'candid',-1)
    expect(tip.get_by_role('button',name='已在生词本 ✓',exact=True)).to_be_disabled()
    # Reloading preserves the collection state in the cached and server vocab.
    page.reload()
    expect(page.locator('#badge-0')).to_contain_text('6')
    hover_word(page,'candid')
    expect(tip.get_by_role('button',name='已在生词本 ✓',exact=True)).to_be_disabled()
    # This chart occurrence uses a fallback from paragraph 0; the source must be
    # the actually hovered paragraph 2. A concurrent personal edit must survive.
    hover_word(page,'chart',2)
    wordbook.save(dict(word='chart',sense='My edited definition',note='Keep my note'))
    collect=tip.get_by_role('button',name='＋ 加入生词本',exact=True)
    collect.focus()
    page.keyboard.press('Enter')
    expect(tip.get_by_role('button',name='已加入生词本 ✓',exact=True)).to_be_disabled()
    record=wordbook.get('chart')
    assert record['source']['paragraph']==2 and record['source']['context']==article['paragraphs'][2]
    assert record['sense']=='My edited definition' and record['note']=='Keep my note'
    hover_word(page,'over')
    tip.get_by_role('button',name='＋ 加入生词本',exact=True).click()
    expect(tip.get_by_role('button',name='已加入生词本 ✓',exact=True)).to_be_disabled()
    record=wordbook.get('paper over')
    assert record['sense']=='To conceal a problem' and record['source']['paragraph']==0
    page.keyboard.press('Escape')
    expect(tip).not_to_be_visible()
    # The saved words must also appear in the standalone notebook.
    page.get_by_role('link',name='生词本',exact=True).click()
    expect(page.get_by_role('heading',name='个人生词本',exact=True)).to_be_visible()
    expect(page.locator('.vocab-word')).to_have_count(3)
    expect(page.locator('.wordbook-list')).to_contain_text('paper over')


def main():
    WebTests.setUpClass()
    paragraphs = [
        "A candid speaker charted paths. It won't paper over uncertainties. A runner stopped and studied candidly.",
        'A candid assessment charts the future.',
        'A chart describes the future. Familiar words remain ordinary.',
        *['An ordinary reading paragraph with sufficient content for scrolling. ' * 5 for _ in range(10)]]
    article = dict(index=0, title='A candid view', paragraphs=paragraphs)
    store._atomic_json_write(config.app_dir()/'cache/sample/content.json', dict(
        bookSize=WebTests.meta['size'], format='epub', articles=[article,
            dict(index=1, title='Another article', paragraphs=['A candid speaker.'])]))
    entry = dict(word='candid', phonetic='/kand-id/', pos='adj.', sense='Frank in this paragraph <em>literal</em>',
        inText='A candid speaker', root='From Latin candidus.', note='My saved note')
    vocab = dict(articleIndex=0, level='cet4', titleEntries=[dict(word='candid', sense='Title meaning')],
        paragraphs=[dict(index=0, entries=[entry,
            dict(word='chart', sense='To plan a path'), dict(word='paper over', sense='To conceal a problem'),
            dict(word='uncertainty', sense='A lack of certainty'), dict(word='stop', sense='To cease'),
            dict(word='study', sense='To learn')]),
            dict(index=1, entries=[dict(word='candid', sense='Frank assessment')])],
        processedParagraphs=[-1, *range(len(paragraphs))],
        phraseProcessedParagraphs=[-1, *range(len(paragraphs))], coverageVersion=gen.VOCAB_COVERAGE_VERSION)
    store.save_vocab('sample', vocab)
    server = make_server('127.0.0.1', 0, WebTests.app)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    errors, requests = [], []
    try:
        with (sync_playwright() as playwright, patch.object(core_dict, 'ensure_db'),
              patch.object(core_dict, 'lookup_many', return_value={})):
            chrome = os.environ.get('KANDU_TEST_CHROME')
            browser = playwright.chromium.launch(headless=True, **({'executable_path':chrome} if chrome else {}))
            for width in [1440, 390]:
                for record in wordbook.list_words():
                    wordbook.delete(record['word'])
                context = browser.new_context(viewport=dict(width=width, height=900), service_workers='block')
                page = context.new_page(); page.on('pageerror', lambda error:errors.append(str(error)))
                page.on('request', lambda request: requests.append((request.method, request.url)))
                page.goto(f'http://127.0.0.1:{server.server_port}/#/read/sample/0')
                page.locator('#password').fill(WebTests.authcode); page.locator('#login-form button').click()
                expect(page.locator('#badge-0')).to_contain_text('6')
                page.evaluate("document.documentElement.style.scrollBehavior='auto'")
                toggle=page.locator('#vocab-highlight-toggle')
                expect(toggle).to_have_attribute('aria-pressed','true')
                highlights=lambda:page.evaluate("[...(CSS.highlights.get('kandu-vocabulary')||[])].map(r=>r.toString())")
                assert {'candid','charted','paper over','uncertainties','stopped','studied','charts','chart'} <= set(highlights()), highlights()
                assert 'speaker' not in highlights() and 'candidly' not in highlights()
                assert page.locator('#para-0 p').text_content()==paragraphs[0]
                # Painting ranges adds no inline elements and never changes wrapping or selection.
                assert page.locator('#para-0 p > *').count()==0
                hover_word(page,'candid',wait=False);page.mouse.move(1,1)
                geometry=page.locator('#para-0 p').bounding_box()
                position=snapshot(page)
                page.evaluate('''() => {
                    const p=document.querySelector('#para-0 p'),r=document.createRange();
                    r.setStart(p.firstChild,2);r.setEnd(p.firstChild,8);
                    getSelection().removeAllRanges();getSelection().addRange(r);
                    document.querySelector('#vocab-highlight-toggle').click();
                }''')
                assert page.evaluate('getSelection().toString()')=='candid'
                assert highlights()==[]
                assert page.locator('#para-0 p').bounding_box()==geometry
                assert snapshot(page)==position
                page.evaluate('getSelection().removeAllRanges()')
                page.reload()
                expect(page.locator('#badge-0')).to_contain_text('6')
                page.evaluate("document.documentElement.style.scrollBehavior='auto'")
                expect(toggle).to_have_attribute('aria-pressed','false')
                assert highlights()==[]
                reader_button(page,'生词标记').click()
                expect(toggle).to_have_attribute('aria-pressed','true')
                assert 'paper over' in highlights()
                tip = page.locator('#vocab-hover')
                request_start = len(requests)
                point = hover_word(page, 'candid', wait=False)
                before = snapshot(page)
                page.wait_for_timeout(450)
                expect(tip).not_to_be_visible()
                page.mouse.move(point['x']+1, point['y'])  # Movement inside the same word keeps its dwell.
                page.wait_for_timeout(650)
                expect(tip).to_be_visible()
                expect(tip.locator('.hover-sense')).to_have_text(entry['sense'])
                expect(tip.locator('em')).to_have_count(0)
                expect(tip).to_contain_text('kand-id')
                expect(tip).to_contain_text('Latin candidus')
                expect(tip).to_contain_text('My saved note')
                assert snapshot(page) == before
                bounds = tip.bounding_box()
                assert bounds['x'] >= 9 and bounds['x']+bounds['width'] <= width-9, bounds
                assert bounds['y'] >= 9 and bounds['y']+bounds['height'] <= 891, bounds
                word_bottom = point['y'] + 20
                assert min(abs(bounds['y']-word_bottom), abs(point['y']-(bounds['y']+bounds['height']))) < 60, bounds
                expect(page.locator('#reader-reading-time')).to_contain_text('词义提示暂停')
                elapsed = page.evaluate('state.reader.clock.elapsedMs')
                page.wait_for_timeout(1200)
                assert page.evaluate('state.reader.clock.elapsedMs') == elapsed
                page.mouse.move(1, 1)
                expect(tip).not_to_be_visible()
                expect(page.locator('#reader-reading-time')).to_contain_text('计时中')
                if folder:=os.environ.get('KANDU_TEST_ARTIFACTS'):
                    target=Path(folder);target.mkdir(parents=True,exist_ok=True)
                    page.screenshot(path=str(target/f'vocabulary-highlights-{width}.png'))

                for word, meaning in [('charted', 'To plan a path'), ('uncertainties', 'A lack of certainty'),
                                      ('stopped', 'To cease'), ('studied', 'To learn'), ('over', 'To conceal a problem')]:
                    hover_word(page, word)
                    expect(tip.locator('.hover-sense')).to_have_text(meaning)
                hover_word(page, 'candid', 1)
                expect(tip.locator('.hover-sense')).to_have_text('Frank assessment')
                hover_word(page, 'chart', 2)
                expect(tip.locator('.hover-sense')).to_have_text('To plan a path')
                hover_word(page, 'candid', -1)
                expect(tip.locator('.hover-sense')).to_have_text('Title meaning')
                for word in ['speaker', 'candidly']:
                    hover_word(page, word, wait=False); page.wait_for_timeout(1150)
                    expect(tip).not_to_be_visible()

                hover_word(page, 'candid')
                page.keyboard.press('Escape'); expect(tip).not_to_be_visible()
                hover_word(page, 'candid')
                page.mouse.wheel(0, 100); expect(tip).not_to_be_visible()
                hover_word(page, 'candid', wait=False)
                page.mouse.move(1, 1); page.wait_for_timeout(1150)
                expect(tip).not_to_be_visible()

                # Highlighting splits a word into several text nodes; still resolve the full word.
                reader_button(page,'查找').click()
                page.get_by_label('查找原文').fill('and')
                assert page.locator('#para-0 mark').count() > 0
                assert page.evaluate("[...CSS.highlights.get('kandu-vocabulary')].every(r=>!r.startContainer.parentElement.closest('mark'))")
                hover_word(page, 'candid')
                expect(tip.locator('.hover-sense')).to_have_text(entry['sense'])
                page.evaluate('''() => {
                    const p=document.querySelector('#para-0 p'),range=document.createRange();
                    range.selectNodeContents(p);getSelection().removeAllRanges();getSelection().addRange(range);
                }''')
                expect(tip).not_to_be_visible()
                expect(page.locator('#selection-tools')).to_be_visible()
                expect(page.locator('#selection-lookup')).not_to_be_visible()
                page.evaluate('getSelection().removeAllRanges()')
                expect(page.locator('#selection-tools')).not_to_be_visible()

                hover_word(page, 'candid')
                reader_button(page,'暂停计时').click()
                hover_word(page, 'candid')
                expect(page.locator('#reader-reading-time')).to_contain_text('已暂停')
                page.mouse.move(1, 1)
                expect(page.locator('#reader-reading-time')).to_contain_text('已暂停')
                reader_button(page,'继续计时').click()

                page.get_by_label('查找原文').fill('')
                assert 'candid' in highlights() and 'paper over' in highlights()
                assert page.locator('#para-0 p > *').count()==0

                # Existing vocabulary is the only source; hovering does not fetch or collect words.
                assert not [url for method, url in requests[request_start:] if
                    any(s in url for s in ['/vocab', '/lookup', '/collect', '/generate-', '/wordbook'])], requests[request_start:]
                assert not wordbook.list_words()

                # A normal vocabulary refresh must replace cached hover meanings.
                vocab['paragraphs'][0]['entries'][0]['sense'] = 'Updated saved meaning'
                store.save_vocab('sample', vocab)
                hover_word(page, 'candid')
                page.locator('#badge-0').click()
                expect(page.locator('#sheet')).to_be_visible()
                expect(tip).not_to_be_visible()
                # The normal reader reuses vocabulary for 30 seconds; request
                # an explicit refresh so this assertion is independent of test speed.
                page.evaluate('loadVocab(state.reader,{fresh:true})')
                page.locator('#sheet-close').click()
                hover_word(page, 'candid')
                expect(tip.locator('.hover-sense')).to_have_text('Updated saved meaning')
                assert 'candid' in highlights()
                vocab['paragraphs'][0]['entries'][0]['sense'] = entry['sense'] = 'Frank in this paragraph <em>literal</em>'
                store.save_vocab('sample', vocab)

                # Touch events must neither schedule nor display mouse hover hints.
                page.mouse.move(1, 1)
                page.locator('#para-0 p').dispatch_event('pointermove', dict(pointerType='touch', clientX=point['x'], clientY=point['y']))
                page.wait_for_timeout(1150); expect(tip).not_to_be_visible()
                hover_word(page, 'candid', wait=False)
                page.evaluate("location.hash='#/read/sample/1'")
                expect(page.locator('.reader-head h1')).to_have_text('Another article')
                assert highlights()==[]
                page.wait_for_timeout(1150)
                expect(page.locator('#vocab-hover')).not_to_be_visible()
                hover_word(page, 'candid', wait=False); page.wait_for_timeout(1150)
                expect(page.locator('#vocab-hover')).not_to_be_visible()
                check_hover_collection(page,article,entry)
                context.close()
            assert not errors, errors
            browser.close()
        print('VOCAB HOVER/HIGHLIGHT PASS: desktop/narrow viewport, hover card collection/retry/double-click guard, saved status, keyboard save, actual paragraph source, personal edits, phrase/notebook integration, highlights, unchanged position, lifecycle/timing; passive hover makes no model/lookup/collection requests')
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=5)
        WebTests.tearDownClass()


if __name__ == '__main__':
    main()
