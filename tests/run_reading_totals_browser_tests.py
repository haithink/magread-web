"""Partial reading and cached statistics with delayed outbox replies; no real data/LLM."""
import os
from pathlib import Path
import threading
import types
from unittest.mock import patch

from playwright.sync_api import sync_playwright, expect
from werkzeug.serving import make_server

from run_web_tests import WebTests, store, llm
from webapp import reading_activity as activity, reading_stats
import webapp.server as server_module


def main():
    WebTests.setUpClass()
    WebTests.meta['publication']='Lex Fridman Podcast'
    store.save_library([WebTests.meta])
    article=dict(index=0,title='An unfinished reading experiment',paragraphs=['word '*100 for _ in range(20)])
    store._atomic_json_write(store._cache_dir('sample')/'content.json',dict(
        bookSize=WebTests.meta['size'],format='epub',articles=[article]))
    now=[server_module.time.time_ns()//1_000_000]
    fake_time=types.SimpleNamespace(time_ns=lambda:now[0]*1_000_000,time=lambda:now[0]/1000,
        monotonic=server_module.time.monotonic)
    errors=[]
    with patch.object(activity,'time',fake_time),patch.object(server_module,'time',fake_time),\
            patch.object(llm,'conversation',side_effect=AssertionError('No paid models')):
        server=make_server('127.0.0.1',0,WebTests.app,threaded=True)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        base=f'http://127.0.0.1:{server.server_port}'
        try:
            with sync_playwright() as p:
                engine=os.environ.get('KANDU_TEST_ENGINE','chromium')
                expect.set_options(timeout=30_000 if engine=='webkit' else 5000)
                browser=getattr(p,engine).launch(headless=True,**(
                    {'executable_path':os.environ['KANDU_TEST_CHROME']} if engine=='chromium' else {}))
                for mobile in [False,True]:
                    store.set_article_state('sample',0,'unread')
                    reading_stats.mark_web(WebTests.meta,article,False)
                    context=browser.new_context(viewport={'width':393 if mobile else 1440,'height':852 if mobile else 1000},
                        is_mobile=mobile,has_touch=mobile,timezone_id='Asia/Shanghai',service_workers='block')
                    page=context.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
                    def legacy_stats(route):
                        response=route.fetch()
                        data=response.json()
                        data['analytics'].pop('readingSources',None)
                        route.fulfill(response=response,json=data)
                    page.route('**/api/reading-stats?*',legacy_stats)
                    page.goto(base+'/#/reading-stats')
                    page.locator('#password').fill(WebTests.authcode);page.locator('#login-form button').click()
                    measured=page.locator('[data-stat="measured-words"]')
                    before=activity.report([])['totals']['measuredWords']
                    expect(measured).to_have_text(f'{before:,}')
                    expect(page.locator('[data-stat="words"]')).to_have_text('0')
                    expect(measured.locator('..')).to_contain_text('累计实际阅读词数（估算）')
                    expect(page.locator('[data-stat="words"]').locator('..')).to_contain_text('已完成文章总词数')
                    source_mode=page.get_by_role('combobox',name='阅读来源统计范围')
                    source_panel=page.locator('.reading-source-distribution')
                    expect(source_mode).to_have_value('completed')
                    page.unroute('**/api/reading-stats?*',legacy_stats)
                    page.evaluate("dispatchEvent(new CustomEvent('reading-synced',{detail:{path:'/reading-stats',data:{},result:{}}}))")
                    expect(source_mode).to_have_value('all')
                    # Seed a real cache before reading, then hold the next clock
                    # upload while opening statistics. A stale snapshot must
                    # update after synchronization without a manual refresh.
                    page.evaluate("location.hash='#/read/sample/0'")
                    page.wait_for_function('() => state.reader?.clock?.lastQueued && ReadingSync.pending()===0')
                    held=[];released=[False]
                    def hold_clock(route):
                        if released[0]:route.continue_()
                        else:held.append(route)
                    page.route('**/reading-time',hold_clock)
                    now[0]+=30_000
                    page.evaluate('''stamp => {
                        ReadingSync.now=()=>stamp;
                        state.reader.clock.elapsedMs=30000;
                        flushReadingClock(state.reader,false,{paragraphIndex:1,paragraphOffset:0});
                        location.hash='#/reading-stats';
                    }''',now[0])
                    expect(measured).to_have_text(f'{before:,}')
                    expect(page.get_by_role('heading',name='已读统计',exact=True)).to_be_visible()
                    page.get_by_role('searchbox',name='搜索已读文章').fill('keep my search')
                    page.get_by_role('combobox',name='图表日期范围').select_option('7')
                    source_mode.select_option('completed')
                    expect(page.locator('[data-stat="articles"]')).to_have_text('0')
                    page.wait_for_function('() => ReadingSync.pending()>0')
                    assert held
                    released[0]=True
                    for route in held[:]:route.continue_()
                    expect(measured).to_have_text(f'{before+100:,}')
                    page.unroute('**/reading-time',hold_clock)
                    expect(page.locator('[data-stat="words"]')).to_have_text('0')
                    expect(page.get_by_role('searchbox',name='搜索已读文章')).to_have_value('keep my search')
                    expect(page.get_by_role('combobox',name='图表日期范围')).to_have_value('7')
                    expect(source_mode).to_have_value('completed')
                    expect(source_panel.locator('.reading-source-row')).to_have_count(0)
                    source_mode.select_option('all')
                    expect(source_panel.locator('.reading-source-row')).to_have_count(1)
                    expect(source_panel).to_contain_text('Lex Fridman Podcast')
                    expect(source_panel).to_contain_text(f'已完成 0 篇 · 未完成 1 篇 · {before+100:,} 词')
                    expect(page.locator('#reading-cache-notice')).to_have_count(0)
                    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                    # Completion replaces partial credit with the full article;
                    # the original measured-word and speed ledger stays intact.
                    measured_before=activity.report([])['totals']
                    page.evaluate('''async () => {
                        await api('/books/sample/state',{method:'POST',data:{articleIndex:0,state:'read'}});
                    }''')
                    page.reload()
                    expect(page.locator('[data-stat="words"]')).to_have_text('2,000')
                    expect(measured).to_have_text('2,000')
                    expect(page.locator('[data-stat="articles"]')).to_have_text('1')
                    expect(source_mode).to_have_value('all')
                    expect(source_panel).to_contain_text('已完成 1 篇 · 未完成 0 篇 · 2,000 词')
                    source_mode.select_option('completed')
                    expect(source_panel).to_contain_text('1 篇 · 2,000 词')
                    source_mode.select_option('all')
                    assert activity.report([])['totals']==measured_before
                    if folder:=os.environ.get('KANDU_TEST_ARTIFACTS'):
                        target=Path(folder);target.mkdir(parents=True,exist_ok=True)
                        page.screenshot(path=str(target/('reading-totals-phone.png' if mobile else 'reading-totals-desktop.png')),full_page=True)
                    # Small sources remain individually discoverable beyond five;
                    # old offline snapshots must not pretend to include partial words.
                    page.evaluate('''async () => {
                        const analytics=(await api('/reading-stats?days=365&timezoneOffset=-480',{fresh:true})).analytics;
                        const sample={...analytics,readingSources:Array.from({length:7},(_,i)=>({
                            name:i===6?'Small Podcast Source':'Source '+i,words:7-i,
                            articleCount:1,completedArticleCount:0,unfinishedArticleCount:1}))};
                        window.testSourcePanel=readingAnalyticsPanel(sample);
                        document.querySelector('main').append(testSourcePanel);
                    }''')
                    expect(page.locator('.reading-source-row')).to_have_count(8)
                    expect(page.get_by_text('Small Podcast Source',exact=True)).to_be_visible()
                    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                    page.evaluate('''async () => {
                        testSourcePanel.cleanup();testSourcePanel.remove();
                        const analytics={...(await api('/reading-stats?days=365&timezoneOffset=-480')).analytics};
                        delete analytics.readingSources;
                        window.testSourcePanel=readingAnalyticsPanel(analytics);
                        document.querySelector('main').append(testSourcePanel);
                    }''')
                    legacy=page.locator('.reading-source-distribution').last
                    expect(legacy.get_by_role('combobox')).to_have_value('completed')
                    expect(legacy).to_contain_text('按已完成文章全文词数统计')
                    page.evaluate('testSourcePanel.cleanup();testSourcePanel.remove()')
                    # A late stats response after navigation must not replace the reader.
                    delayed=[]
                    page.route('**/api/reading-stats?*',lambda route:delayed.append(route))
                    page.reload()
                    expect(measured).to_have_text('2,000')
                    page.evaluate("location.hash='#/read/sample/0'")
                    expect(page.locator('#reader-reading-time')).to_be_visible()
                    for route in delayed:route.continue_()
                    expect(page.locator('[data-stat="measured-words"]')).to_have_count(0)
                    context.close()
                browser.close()
            assert not errors,errors
            print(f'READING TOTALS BROWSER PASS ({engine}): desktop + phone, partial reading, cached stats, delayed sync, preserved controls, early completion, route cleanup, no overflow/models')
        finally:
            server.shutdown();server.server_close();thread.join(timeout=5)
            WebTests.tearDownClass()


if __name__=='__main__':
    main()
