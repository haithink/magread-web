"""Publication-local recent reading, completed history and resume in an isolated library."""
import os
from pathlib import Path
import threading
import time
from unittest.mock import patch
from wsgiref.simple_server import make_server

from playwright.sync_api import sync_playwright, expect
from run_article_duplicate_tests import publication, WebTests, config, store, duplicates
from run_web_tests import core_dict


def main():
    WebTests.setUpClass()
    titles=[f'Article {i}: A thoughtful conversation about science and the future' for i in range(10)]
    current,articles=publication('issue','Wired_2026.11.02.epub',titles)
    older,_=publication('older','Wired_2026.08.02.epub',[titles[0]])
    unrelated,_=publication('unrelated','Other_2026.11.02.epub',['Other publication reading'])
    completed_titles=['A finished article worth revisiting','Another completed article']
    completed,_=publication('completed','Wired_2026.09.02.epub',completed_titles)
    episode,_=publication('podcast-local-42','podcast42.epub',['#42: An ongoing conversation'])
    episode.update(sourceType='podcast',sourceId='lex-fridman',episodeNumber=42,title='#42: An ongoing conversation')
    other_episode,_=publication('podcast-local-43','podcast43.epub',['#43: An unopened conversation'])
    other_episode.update(sourceType='podcast',sourceId='lex-fridman',episodeNumber=43,title='#43: An unopened conversation')
    for article in articles:
        article['paragraphs']=[('An extended paragraph for checking a saved reading position. '*7)+f'Paragraph {i}.' for i in range(16)]
    store._atomic_json_write(config.app_dir()/'cache/issue/content.json',dict(
        format='epub',bookSize=current['size'],articles=articles))
    store.save_library([current,older,unrelated,completed,episode,other_episode]);duplicates._cached_titles.cache_clear()
    now=int(time.time()*1000)
    histories={str(i):dict(title=titles[i],paragraphIndex=5,paragraphOffset=0,paragraphCount=16,
        wordCount=1200,lastReadAt=now-(10-i)*1000,progress=.32,atStart=False,atEnd=False) for i in range(9)}
    histories['0']['lastReadAt']=now
    histories['5'].update(progress=1,atEnd=True)
    histories['8'].update(progress=.997)  # Legacy completion without an atEnd flag.
    store.save_reading_progress('unrelated',0,dict(title='Other publication reading',paragraphIndex=0,
        paragraphOffset=0,paragraphCount=1,wordCount=12,progress=.1))
    server=make_server('127.0.0.1',0,WebTests.app)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    base=f'http://127.0.0.1:{server.server_port}'
    errors=[]
    try:
        with sync_playwright() as playwright,patch.object(core_dict,'ensure_db'),patch.object(core_dict,'lookup_many',return_value={}):
            chrome=os.environ.get('KANDU_TEST_CHROME')
            browser=playwright.chromium.launch(headless=True,**({'executable_path':chrome} if chrome else {}))
            for mobile in [False,True]:
                store._atomic_json_write(config.app_dir()/'reading_history/issue.json',histories)
                store._atomic_json_write(config.app_dir()/'article_states/issue.json',{'6':'read','7':'uninterested'})
                store._atomic_json_write(config.app_dir()/'article_states/completed.json',{'0':'read','1':'read'})
                store._atomic_json_write(config.app_dir()/'reading_history/completed.json',{
                    str(i):dict(histories['0'],title=title,paragraphIndex=0,progress=.07+i*.8,lastReadAt=now-i*1000)
                    for i,title in enumerate(completed_titles)})
                store._atomic_json_write(config.app_dir()/'article_states/podcast-local-42.json',{})
                store.save_reading_progress('podcast-local-42',0,dict(title=episode['title'],paragraphIndex=0,
                    paragraphOffset=0,paragraphCount=1,wordCount=12,progress=.2))
                context=browser.new_context(viewport=dict(width=390 if mobile else 1440,height=1000),is_mobile=mobile,has_touch=mobile)
                page=context.new_page();page.on('pageerror',lambda error:errors.append(str(error)))
                requests=[];page.on('request',lambda req:requests.append((req.method,req.url)))
                page.goto(base+'/#/book/issue')
                page.locator('#password').fill(WebTests.authcode);page.locator('#login-form button').click()
                expect(page.get_by_role('button',name='未读 7',exact=True)).to_have_class('active')
                expect(page.locator('.article-card')).to_have_count(7)
                page.get_by_role('button',name='全部 9',exact=True).click()
                expect(page.locator('.article-card')).to_have_count(9)
                page.get_by_role('button',name='未读 7',exact=True).click()
                recent=page.get_by_role('region',name='本刊最近阅读',exact=True)
                links=recent.locator('.issue-recent-link')
                expect(links).to_have_count(3)
                expect(recent.locator('.count')).to_have_text('本刊 · 8 篇')
                expect(recent.locator('.issue-recent-title')).to_have_text([titles[i] for i in [0,8,6]])
                expect(links.first).to_have_attribute('href','#/read/issue/0')
                expect(links.first.locator('.issue-recent-position')).to_contain_text('第 6 段 · 32%')
                expect(links.locator('.issue-recent-state')).to_have_text(['阅读中','已到文末','已读'])
                assert page.locator('.article-card').filter(has=page.get_by_role('heading',name=titles[0],exact=True)).count()==0
                assert recent.bounding_box()['y']<page.get_by_role('searchbox',name='搜索文章').bounding_box()['y']
                before=len(requests)
                more=recent.get_by_role('button',name='更多',exact=True)
                more.tap() if mobile else more.click()
                expect(links).to_have_count(8)
                expect(recent.locator('.issue-recent-title')).to_have_text([titles[i] for i in [0,8,6,5,4,3,2,1]])
                expect(recent.get_by_role('button',name='收起',exact=True)).to_have_attribute('aria-expanded','true')
                search=page.get_by_role('searchbox',name='搜索文章')
                search.fill('no such article')
                expect(page.locator('.article-card')).to_have_count(0)
                expect(links).to_have_count(8)
                search.fill('')
                page.get_by_role('button',name='未读 7',exact=True).click()
                expect(links).to_have_count(8)
                page.locator('#show-duplicate-articles').check()
                expect(links).to_have_count(8)
                assert len(requests)==before,requests[before:]
                recent.get_by_role('button',name='收起',exact=True).click()
                expect(links).to_have_count(3)
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                if folder:=os.environ.get('KANDU_TEST_ARTIFACTS'):
                    target=Path(folder);target.mkdir(parents=True,exist_ok=True)
                    page.screenshot(path=str(target/('issue-recent-phone.png' if mobile else 'issue-recent-desktop.png')))
                links.first.tap() if mobile else links.first.click()
                expect(page.locator('.reader-head h1')).to_have_text(titles[0])
                expect(page.locator('#reader-paragraph')).to_have_text('第 6 / 16 段')
                assert page.evaluate('state.reader.bookId')=='issue'
                page.get_by_role('link',name='← 目录',exact=True).click()
                expect(page.get_by_role('button',name='未读 7',exact=True)).to_have_class('active')
                expect(links.first).to_have_attribute('href','#/read/issue/0')
                # Completed items stay accessible; uninteresting items are removed.
                card=page.locator('.article-card').filter(has=page.get_by_role('heading',name=titles[4],exact=True))
                card.get_by_role('combobox',name='文章标记').select_option('read')
                expect(card).to_have_count(0)
                expect(recent.locator('.count')).to_have_text('本刊 · 8 篇')
                recent.get_by_role('button',name='更多',exact=True).click()
                expect(recent.locator('.issue-recent-link[href="#/read/issue/4"] .issue-recent-state')).to_have_text('已读')
                card=page.locator('.article-card').filter(has=page.get_by_role('heading',name=titles[3],exact=True))
                card.get_by_role('combobox',name='文章标记').select_option('uninterested')
                expect(card).to_have_count(0)
                expect(recent.locator('.count')).to_have_text('本刊 · 7 篇')
                expect(links).to_have_count(7)
                expect(recent.locator('.issue-recent-link[href="#/read/issue/3"]')).to_have_count(0)
                # Reproduce an issue whose only two histories are manually read
                # at partial progress: its recent panel must remain visible.
                page.goto(base+'/#/book/completed')
                expect(page.get_by_role('button',name='未读 0',exact=True)).to_have_class('active')
                expect(page.locator('.article-card')).to_have_count(0)
                page.get_by_role('button',name='全部 2',exact=True).click()
                expect(page.locator('.article-card')).to_have_count(2)
                expect(recent).to_be_visible()
                expect(recent.locator('.count')).to_have_text('本刊 · 2 篇')
                expect(recent.locator('.issue-recent-title')).to_have_text(completed_titles)
                expect(recent.locator('.issue-recent-state')).to_have_text(['已读','已读'])
                expect(recent.get_by_role('button',name='更多',exact=True)).not_to_be_visible()
                # No reading history leaves no empty panel or literal null on the page.
                page.goto(base+'/#/book/older')
                expect(page.locator('.digest-head h1')).to_have_text('Wired_2026.08.02')
                expect(page.locator('#issue-recent')).not_to_be_visible()
                assert page.locator('#app').evaluate('''node=>[...node.childNodes].filter(n=>n.nodeType===Node.TEXT_NODE)
                    .map(n=>n.textContent.trim()).filter(Boolean)''')==[]
                # Series display indices differ from the real transcript's article index.
                page.goto(base+'/#/book/podcast-series-lex-fridman')
                series=page.get_by_role('region',name='本系列最近阅读',exact=True)
                expect(series.locator('.issue-recent-title')).to_have_text(episode['title'])
                expect(series.locator('.issue-recent-link')).to_have_attribute('href','#/read/podcast-local-42/0')
                series.locator('.issue-recent-link').tap() if mobile else series.locator('.issue-recent-link').click()
                expect(page.locator('.reader-head h1')).to_have_text(episode['title'])
                expect(page.get_by_role('link',name='← 目录',exact=True)).to_have_attribute('href','#/book/podcast-series-lex-fridman')
                context.close()
            browser.close()
        assert not errors,errors
        print('ISSUE RECENT PASS: desktop/touch phone, current issue only, recent read/completed/ongoing histories, completed-only issue, more/collapse, independent filters and duplicates, saved paragraph resume, immediate state update, empty state, no extra fetch/model calls')
    finally:
        server.shutdown();server.server_close();thread.join(timeout=5)
        WebTests.tearDownClass()


if __name__=='__main__':
    main()
