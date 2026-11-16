"""Desktop and mobile browser tests against an isolated local server."""
import json
import os
from pathlib import Path
import threading
import time
from unittest.mock import patch
from wsgiref.simple_server import make_server

from run_web_tests import WebTests, SANDBOX, config, store, gen, llm, core_dict, wordbook
from playwright.sync_api import sync_playwright, expect


def check_vocab_scroll_isolation(page, paragraph, long_list=False):
    page.locator(f'#badge-{paragraph}').scroll_into_view_if_needed()
    page.wait_for_timeout(1200)
    def snapshot():
        return page.evaluate('''() => ({y:scrollY,
            progress:document.querySelector('#reader-progress-bar').value,
            paragraph:document.querySelector('#reader-paragraph').textContent})''')
    before = snapshot()
    record = store.load_reading_history('sample')['0']
    saved_position = (record['paragraphIndex'], record['paragraphOffset'])
    page.locator(f'#badge-{paragraph}').click()
    sheet = page.locator('#sheet')
    expect(sheet).to_be_visible()
    assert snapshot() == before, (before, snapshot())
    assert sheet.evaluate('(node) => node.scrollHeight > node.clientHeight') == long_list
    bounds = sheet.bounding_box()
    page.mouse.move(bounds['x'] + bounds['width'] / 2, bounds['y'] + bounds['height'] / 2)
    page.mouse.wheel(0, -1200)
    page.wait_for_timeout(250)
    page.mouse.wheel(0, 350)
    page.wait_for_timeout(250)
    if long_list:
        assert sheet.evaluate('(node) => node.scrollTop') > 0
    sheet.evaluate('(node) => node.scrollTop = node.scrollHeight')
    page.mouse.wheel(0, 1200)
    page.wait_for_timeout(250)
    page.mouse.wheel(0, 1200)
    page.wait_for_timeout(250)
    page.mouse.move(2, 2)  # The modal backdrop must also leave the article stationary.
    page.mouse.wheel(0, 1200)
    page.wait_for_timeout(1100)
    assert snapshot() == before, (before, snapshot())
    record = store.load_reading_history('sample')['0']
    assert (record['paragraphIndex'], record['paragraphOffset']) == saved_position, record
    if long_list:
        page.keyboard.press('Escape')
    else:
        page.locator('#sheet-close').click()
    expect(sheet).not_to_be_visible()
    assert snapshot() == before, (before, snapshot())
    page.mouse.move(page.viewport_size['width'] / 2, page.viewport_size['height'] / 2)
    page.mouse.wheel(0, 200)
    page.wait_for_timeout(350)
    assert snapshot()['y'] > before['y'], 'Article scrolling must resume after closing the dialog'


def main():
    WebTests.setUpClass()
    app = WebTests.app
    article = dict(index=0, title='Reading across devices', paragraphs=[
        f'Paragraph {i}. The writer gave a candid reply. ' + 'We read the world one sentence at a time. ' * 10
        for i in range(32)])
    first_sentence = 'Cruz brought a little surprise to Isaacman’s confirmation hearing last April.'
    article['paragraphs'][0] = (first_sentence + ' It was a poster of the moon. A candid reply followed. '
        '“My team used ChatGPT,” explained the senator, who chairs the committee that oversees NASA.')
    store._atomic_json_write(config.app_dir() / 'cache/sample/content.json', dict(
        bookSize=WebTests.meta['size'], format='epub', articles=[article]))
    server = make_server('127.0.0.1', 0, app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f'http://127.0.0.1:{server.server_port}'
    errors = []
    requests = []
    analysis_calls = []
    def fake_analysis(meta, art, sentence, before, after, cfg, **kwargs):
        analysis_calls.append(sentence)
        return dict(grammarComponents=[], grammarPoints=[], vocab=[], translation='模拟句子讲解。')
    def fake_vocab(meta, art, level, cfg, **kwargs):
        kwargs['on_progress'](0,1,'test')
        kwargs['on_chars'](150)
        time.sleep(.2)
        result=dict(articleIndex=0,level=level,titleEntries=[],processedParagraphs=[-1,*range(32)],
            paragraphs=[dict(index=i,entries=[dict(word='candid',sense='坦率的',inText='a candid reply')]
                + ([dict(word=word,sense='模拟词义',inText=word) for word in
                    ['stubborn','chart','determination','pursue','novel','insight','subtle','vivid','resolve','strive','persist']]
                    if i == 19 else [])) for i in range(32)])
        store.save_vocab(meta['id'],result)
        return result
    def fake_discussion(cfg,system,messages,on_text,control):
        on_text('这是')
        time.sleep(.2)
        on_text('这是一个模拟回答。')
        return '这是一个模拟回答。'
    try:
        with (sync_playwright() as playwright, patch.object(gen,'generate_vocab',side_effect=fake_vocab) as vocab,
              patch.object(gen,'analyze',side_effect=fake_analysis),
              patch.object(llm,'conversation',side_effect=fake_discussion), patch.object(core_dict,'lookup_many',return_value={})):
            chrome=os.environ.get('KANDU_TEST_CHROME')
            browser=playwright.chromium.launch(**({'executable_path':chrome} if chrome else {}),headless=True)
            desktop=browser.new_context(viewport={'width':1440,'height':1000})
            mobile=browser.new_context(viewport={'width':390,'height':844},is_mobile=True,has_touch=True,device_scale_factor=1)
            pages=[]
            for context in (desktop,mobile):
                page=context.new_page()
                page.on('pageerror',lambda error:errors.append(str(error)))
                page.on('response',lambda response:requests.append((response.status,response.url)) if response.status>=400 else None)
                page.goto(url)
                page.locator('#password').fill(WebTests.authcode)
                page.locator('#login-form button').click()
                expect(page.get_by_role('heading',name='我的阅读室')).to_be_visible()
                expect(page.locator('.book-card')).to_have_count(1)
                pages.append(page)
            page=pages[0]
            page.locator('.book-card .cover-link').click()
            expect(page.locator('.article-card')).to_have_count(1)
            page.locator('.card-bottom select').select_option('read')
            expect(page.locator('.state-pill')).to_have_text('已读')
            page.locator('.card-bottom a').click()
            expect(page.locator('.paragraph')).to_have_count(32)
            expect(page.locator('#reader-percentage')).to_have_text('阅读进度 0%')
            expect(page.get_by_role('progressbar',name='文章阅读进度')).to_be_visible()
            # Model an older installed extension: it listens on document in the bubble phase.
            # Opening a dialog must not allow the same mouse event to start a second analysis.
            page.evaluate('''() => {
                window.externalReaderEvents = {dblclick:0, mouseup:0};
                for (const type of ['dblclick', 'mouseup']) document.addEventListener(type, event => {
                    if (event.target.closest('.paragraph, #sheet')) window.externalReaderEvents[type]++;
                });
            }''')
            page.locator('#para-0 p').dblclick(position={'x':18, 'y':14})
            expect(page.locator('#sheet-content .analysis-sentence')).to_have_text(first_sentence)
            page.locator('#sheet-content .analysis-sentence').dblclick()
            page.locator('#sheet-close').click()
            expect(page.locator('#sheet')).not_to_be_visible()
            assert analysis_calls == [first_sentence], analysis_calls
            assert page.evaluate('() => window.externalReaderEvents') == {'dblclick':0, 'mouseup':0}
            expect(page.locator('#badge-0')).to_contain_text('待生成')
            page.locator('#badge-0').click()
            expect(page.locator('#sheet')).to_be_visible()
            expect(page.locator('.notice')).to_contain_text('尚未生成')
            page.get_by_role('button',name='生成 / 补缺全文生词',exact=True).click()
            expect(page.locator('#sheet-content .vocab-word').first).to_have_text('candid',timeout=10000)
            expect(page.locator('.notice')).to_have_count(0)
            page.locator('.entry-top button').first.click()
            expect(page.locator('.entry-top button').first).to_have_text('编辑收藏')
            source=store.get_book('sample')
            saved_word=wordbook.get('candid')
            assert saved_word['source']['bookTitle']==source['fileName']
            assert saved_word['source']['paragraph']==0
            page.locator('#sheet-close').click()
            expect(page.locator('#badge-0')).to_have_text('词 1 · 本段生词')
            assert vocab.call_count==1
            check_vocab_scroll_isolation(page, 18)
            check_vocab_scroll_isolation(page, 19, long_list=True)
            page.locator('#para-19').scroll_into_view_if_needed()
            time.sleep(1.3)
            record=store.load_reading_history('sample')['0']
            assert record['paragraphIndex']>10,record
            expect(page.locator('#reader-percentage')).not_to_have_text('阅读进度 0%')
            assert abs(page.locator('#reader-progress-bar').evaluate('(node) => node.value')-record['progress'])<.01
            assert page.locator('#reader-paragraph').inner_text()==f"第 {record['paragraphIndex']+1} / 32 段"
            stored_paragraph=record['paragraphIndex']
            phone=pages[1]
            phone.locator('.book-card .cover-link').click()
            phone.locator('.card-bottom a').click()
            expect(phone.locator('.paragraph')).to_have_count(32)
            expect(phone.locator('#badge-0')).to_have_text('词 1 · 本段生词')
            time.sleep(.5)
            assert abs(store.load_reading_history('sample')['0']['paragraphIndex']-stored_paragraph)<=1
            width=phone.evaluate('() => ({scroll:document.documentElement.scrollWidth, width:innerWidth})')
            assert width['scroll']<=width['width'],width
            check_vocab_scroll_isolation(phone, 18)
            check_vocab_scroll_isolation(phone, 19, long_list=True)
            phone.locator('.reader-toolbar').get_by_role('button',name='与 AI 讨论').click()
            phone.locator('.discussion-input textarea').fill('作者想表达什么？')
            phone.locator('.discussion-input button[type=submit]').click()
            expect(phone.locator('.answer-text')).to_have_text('这是一个模拟回答。',timeout=10000)
            expect(phone.locator('.discussion-input button[type=submit]')).to_be_enabled()
            phone.locator('#sheet-close').click()
            phone.get_by_role('link',name='生词本',exact=True).click()
            expect(phone.locator('.vocab-word')).to_have_text('candid')
            expect(phone.locator('.word-source-heading')).to_contain_text('最近来源')
            expect(phone.locator('.word-source-meta')).to_contain_text('第 1 段')
            expect(phone.locator('.word-source-context')).to_contain_text(first_sentence)
            expect(phone.locator('.word-source-link')).to_have_attribute('href','#/read/sample/0?paragraph=0')
            phone.locator('.entry-top button').first.click()
            phone.get_by_label('释义',exact=True).fill('手机上编辑的释义')
            phone.get_by_role('button',name='保存词条',exact=True).click()
            expect(phone.locator('.entry-sense')).to_have_text('手机上编辑的释义')
            expect(phone.locator('.wordbook-list .word-source-link')).to_contain_text('Reading across devices')
            wordbook.save(dict(word='legacy',sense='早期手动词条'))
            phone.reload()
            phone.get_by_role('searchbox',name='搜索个人生词本').fill('legacy')
            expect(phone.locator('.word-source-empty')).to_contain_text('未记录文章来源')
            phone.get_by_role('searchbox',name='搜索个人生词本').fill('Reading across devices')
            expect(phone.locator('.vocab-word')).to_have_text('candid')
            phone.locator('.wordbook-list .word-source-link').click()
            expect(phone.locator('#para-0')).to_have_class('paragraph source-target')
            expect(phone.locator('#reader-paragraph')).to_have_text('第 1 / 32 段')
            gap=phone.evaluate("() => document.querySelector('#para-0').getBoundingClientRect().top-document.querySelector('.reader-toolbar').getBoundingClientRect().bottom")
            assert abs(gap-8)<=2,gap
            phone.evaluate('() => window.scrollTo(0, document.documentElement.scrollHeight)')
            expect(phone.locator('#reader-percentage')).to_have_text('阅读进度 100%')
            expect(phone.locator('#reader-paragraph')).to_have_text('第 32 / 32 段')
            phone.get_by_role('link',name='设置',exact=True).click()
            expect(phone.get_by_role('heading',name='阅读设置')).to_be_visible()
            width=phone.evaluate('() => ({scroll:document.documentElement.scrollWidth, width:innerWidth})')
            assert width['scroll']<=width['width'],width
            assert not errors,errors
            assert not requests,requests
            print('BROWSER PASS: desktop + 390px phone; modal wheel isolation, unchanged saved position, restored article scrolling, live progress, recent word source/search/jump, legacy source fallback, isolated double-click, login, mark, vocab, discussion, sync, edit, no overflow/errors')
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        WebTests.tearDownClass()
        SANDBOX.cleanup()


if __name__=='__main__':
    main()
