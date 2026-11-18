"""Clock lifecycle and interactive charts on desktop/touch, entirely synthetic data."""
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import threading
import types
from unittest.mock import patch
from wsgiref.simple_server import make_server

from playwright.sync_api import sync_playwright, expect
from browser_helpers import reader_button
from run_web_tests import WebTests, config, store, llm
from webapp import reading_activity as activity, reading_stats
from webapp import server as server_module


def check_vocabulary_waits(page, now):
    """Exercise real generation buttons, with held submissions and fake job states."""
    submissions=[]
    job={'state':'running','status':'模拟生成中'}
    def poll(route):
        if job['state']=='disconnect':
            route.abort('failed')
        else:
            route.fulfill(json=dict(job, result={}, error='模拟生成失败'))
    page.route('**/generate-vocab',lambda route:submissions.append(route))
    page.route('**/api/jobs/vocab-clock-*',poll)
    try:
        for terminal in ['done','error','stopped','submission-error','disconnect']:
            job.update(state='running',status='模拟生成中')
            page.locator('#vocab-button').click()
            page.wait_for_function('() => state.vocabWaits.size===1')
            expect(page.locator('#reader-reading-time')).to_contain_text('生词生成暂停')
            elapsed=page.evaluate('() => state.reader.clock.elapsedMs')
            now[0]+=15_000;page.clock.run_for(15_000)
            assert page.evaluate('() => state.reader.clock.elapsedMs')==elapsed,'Submitting vocabulary must pause'
            assert len(submissions)==1
            request=submissions.pop()
            if terminal=='submission-error':
                request.fulfill(status=503,json={'error':'模拟提交失败'})
            else:
                request.fulfill(status=202,json={'jobId':'vocab-clock-'+terminal})
                page.wait_for_function('() => state.activeJobs.size===1')
                now[0]+=15_000;page.clock.run_for(15_000)
                assert page.evaluate('() => state.reader.clock.elapsedMs')==elapsed,'Generating vocabulary must pause'
                if terminal=='stopped':
                    reader_button(page,'暂停计时').click()
                job.update(state=terminal,status='模拟生成结束')
                now[0]+=1000;page.clock.run_for(1000)
            page.wait_for_function('() => state.vocabWaits.size===0')
            if terminal=='done':
                expect(page.locator('#sheet')).to_be_visible()
                now[0]+=15_000;page.clock.run_for(15_000)
                assert page.evaluate('() => state.reader.clock.elapsedMs')==elapsed,'Viewing generated words must pause'
                page.locator('#sheet-close').click()
            else:
                # Completed requests now return their error/stop notice to the center.
                expect(page.locator('#sheet')).to_be_visible()
                page.locator('#sheet-close').click()
            if terminal=='stopped':
                expect(page.locator('#reader-reading-time')).to_contain_text('已暂停')
                reader_button(page,'继续计时').click()
            # Failed/stopped generation must not leave the reader permanently paused.
            page.locator('#reader-reading-time').click()
            now[0]+=1000;page.clock.run_for(1000)
            expect(page.locator('#reader-reading-time')).to_contain_text('计时中')
            assert page.evaluate('() => state.reader.clock.elapsedMs')>elapsed
        # Repeated requests sharing a job must all release; an unrelated article
        # keeps counting, and returning to the original one keeps its pause.
        job.update(state='running',status='模拟生成中')
        page.locator('#vocab-button').click()
        page.wait_for_function('() => state.vocabWaits.size===1')
        now[0]+=1000;page.clock.run_for(1000)
        submissions.pop().fulfill(status=202,json={'jobId':'vocab-clock-shared'})
        page.wait_for_function('() => state.activeJobs.size===1')
        page.evaluate("() => { runJob(baseFor(state.reader)+'/generate-vocab',{phrasesOnly:true}); }")
        page.wait_for_function('() => state.vocabWaits.size===2')
        now[0]+=1000;page.clock.run_for(1000)
        submissions.pop().fulfill(status=202,json={'jobId':'vocab-clock-shared'})
        page.wait_for_function("() => state.jobObservers.get('vocab-clock-shared')?.length===2")
        page.goto(page.url.split('#')[0]+'#/read/sample/1')
        expect(page.locator('#reader-reading-time')).to_contain_text('计时中')
        page.goto(page.url.split('#')[0]+'#/read/sample/0')
        expect(page.locator('#reader-reading-time')).to_contain_text('生词生成暂停')
        elapsed=page.evaluate('() => state.reader.clock.elapsedMs')
        now[0]+=15_000;page.clock.run_for(15_000)
        assert page.evaluate('() => state.reader.clock.elapsedMs')==elapsed
        job.update(state='stopped',status='模拟停止')
        now[0]+=1000;page.clock.run_for(1000)
        page.wait_for_function('() => state.vocabWaits.size===0')
        expect(page.locator('#reader-reading-time')).to_contain_text('计时中')
    finally:
        page.unroute('**/generate-vocab')
        page.unroute('**/api/jobs/vocab-clock-*')


def main():
    WebTests.setUpClass()
    paragraph=' '.join(['Reading science requires patience and attention to each carefully written paragraph.']*20)
    article=dict(index=0,title='Reading with curiosity',paragraphs=[paragraph for _ in range(32)])
    old=dict(index=2,title='An untimed historical article',paragraphs=[paragraph])
    navigation=dict(old,index=1,title='A briefly opened navigation target')
    store._atomic_json_write(store._cache_dir('sample')/'content.json',dict(
        bookSize=WebTests.meta['size'],format='epub',articles=[article,navigation,old]))
    now=[int(datetime.now(timezone.utc).timestamp()*1000)]
    fake_time=types.SimpleNamespace(time_ns=lambda:now[0]*1_000_000,time=lambda:now[0]/1000,monotonic=server_module.time.monotonic)
    errors=[]
    with patch.object(activity,'time',fake_time),patch.object(server_module,'time',fake_time),patch.object(llm,'conversation',side_effect=AssertionError('No paid model')):
        # Populate a genuine ledger with a mix of reading days and zero days.
        for i in [18,16,13,12,10,7,4,3,2,1]:
            now[0]=int((datetime.now(timezone.utc)-timedelta(days=i)).replace(hour=12,minute=0,second=0).timestamp()*1000)
            meta=dict(WebTests.meta,id='archive-'+str(i),title='Lex Fridman Podcast' if i%2 else 'Wired',
                publication='Lex Fridman Podcast' if i%2 else 'Wired')
            art=dict(article,title='Science conversation '+str(i))
            sample=dict(sessionId='seed-reading-session-'+str(i).zfill(4),sequence=0,elapsedMs=0,paragraphIndex=0,paragraphOffset=0)
            activity.record(meta,art,sample)
            now[0]+=60_000
            activity.record(meta,art,dict(sample,sequence=1,elapsedMs=60_000,paragraphIndex=1,paragraphOffset=i*8))
            data=reading_stats.load();reading_stats.merge(data,reading_stats.web_source(meta,art,now[0]));reading_stats.save(data)
        store.set_article_state('sample',2,'read')
        now[0]=int(datetime.now(timezone.utc).timestamp()*1000)
        server=make_server('127.0.0.1',0,WebTests.app)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        base=f'http://127.0.0.1:{server.server_port}'
        try:
            with sync_playwright() as p:
                browser=p.chromium.launch(headless=True,executable_path=os.environ['KANDU_TEST_CHROME'])
                for mobile in [False,True]:
                    store.set_article_state('sample',0,'unread')
                    context=browser.new_context(viewport={'width':390 if mobile else 1440,'height':844 if mobile else 1000},is_mobile=mobile,has_touch=mobile,timezone_id='Asia/Shanghai')
                    page=context.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
                    page.clock.install()
                    page.goto(base+'/#/read/sample/0')
                    page.locator('#password').fill(WebTests.authcode);page.locator('#login-form button').click()
                    expect(page.locator('#reader-reading-time')).to_contain_text('计时中')
                    timer=page.locator('#reading-time-toggle')
                    expect(timer).to_be_visible()
                    expect(timer).to_have_attribute('aria-pressed','false')
                    assert page.locator('#reader-options #reading-time-toggle').count()==0
                    page.wait_for_function('() => state.reader?.clock?.lastSaved')
                    before=activity.article_timing('sample',0)['activeMs']
                    lost=[False]
                    def lose_first_reply(route):
                        if route.request.post_data_json.get('sequence')==1 and not lost[0]:
                            lost[0]=True
                            assert route.fetch().ok
                            route.abort('failed')
                        else:
                            route.continue_()
                    page.route('**/reading-time',lose_first_reply)
                    # Each simulated heartbeat is backed by the same server clock advance.
                    for step,para in enumerate([1,2,3],1):
                        now[0]+=15_000
                        page.locator('#para-'+str(para)).scroll_into_view_if_needed()
                        page.clock.run_for(15_000)
                        if step==1:
                            page.wait_for_function('() => state.reader.clock.syncFailed')
                        else:
                            page.wait_for_function('(elapsed) => state.reader.clock.syncedElapsed>=elapsed',arg=step*15_000-1000)
                    page.unroute('**/reading-time',lose_first_reply)
                    assert page.evaluate('() => state.reader.clock.serverMs')==activity.article_timing('sample',0)['activeMs']
                    reader_button(page,'暂停计时').click()
                    expect(page.locator('#reader-reading-time')).to_contain_text('已暂停')
                    expect(timer).to_have_attribute('aria-pressed','true')
                    page.wait_for_function('() => !state.reader.clock.sending')
                    paused=activity.article_timing('sample',0)['activeMs']
                    assert paused>before,(paused,before)
                    now[0]+=60_000;page.clock.run_for(60_000)
                    page.wait_for_function('() => !state.reader.clock.sending')
                    assert activity.article_timing('sample',0)['activeMs']==paused
                    reader_button(page,'继续计时').click()
                    expect(page.locator('#reading-time-toggle')).to_have_attribute('aria-pressed','false')
                    check_vocabulary_waits(page,now)
                    # Headless Chromium keeps all pages foreground. Emulate the
                    # platform visibility/focus change, leaving clock code intact.
                    page.evaluate('''() => {
                        Object.defineProperty(document,'hidden',{configurable:true,get:()=>true});
                        Object.defineProperty(document,'hasFocus',{configurable:true,value:()=>false});
                        document.dispatchEvent(new Event('visibilitychange'));window.dispatchEvent(new Event('blur'));
                    }''')
                    page.clock.run_for(1000)
                    page.wait_for_function('() => !state.reader.clock.sending')
                    expect(page.locator('#reader-reading-time')).to_contain_text('暂停')
                    hidden=activity.article_timing('sample',0)['activeMs']
                    now[0]+=30_000;page.clock.run_for(30_000)
                    page.wait_for_function('() => !state.reader.clock.sending')
                    assert activity.article_timing('sample',0)['activeMs']==hidden,'Background/blurred pages must pause'
                    page.evaluate('''() => {
                        delete document.hidden;delete document.hasFocus;
                        document.dispatchEvent(new Event('visibilitychange'));window.dispatchEvent(new Event('focus'));
                    }''')
                    page.get_by_role('button',name='与 AI 讨论',exact=True).click()
                    expect(page.locator('#sheet')).to_be_visible()
                    now[0]+=15_000;page.clock.run_for(15_000)
                    expect(page.locator('#reader-reading-time')).to_contain_text('弹窗暂停')
                    page.wait_for_function('() => !state.reader.clock.sending')
                    modal_time=activity.article_timing('sample',0)['activeMs']
                    now[0]+=60_000;page.clock.run_for(60_000)
                    assert activity.article_timing('sample',0)['activeMs']==modal_time
                    page.locator('#sheet-close').click()
                    # Crossing the idle threshold suspends counting until a new input.
                    for _ in range(7):
                        now[0]+=15_000;page.clock.run_for(15_000)
                    expect(page.locator('#reader-reading-time')).to_contain_text('无操作暂停')
                    page.wait_for_function('() => !state.reader.clock.sending')
                    idle=activity.article_timing('sample',0)['activeMs']
                    now[0]+=30_000;page.clock.run_for(30_000)
                    assert activity.article_timing('sample',0)['activeMs']==idle
                    reader_button(page,'标记已读').click()
                    page.get_by_role('link',name='已读统计',exact=True).click()
                    expect(page.get_by_role('heading',name='已读统计',exact=True)).to_be_visible()
                    expect(page.locator('.reading-chart')).to_have_count(2)
                    expect(page.locator('.reading-heatmap button')).to_have_count(77+(datetime.now(timezone.utc)+timedelta(hours=8)).weekday()+1)
                    expect(page.locator('.reading-source-row')).to_have_count(2)
                    expect(page.locator('.completed-card').filter(has_text=old['title'])).to_contain_text('未计时')
                    expect(page.locator('.completed-card').filter(has_text=article['title'])).to_contain_text('有效阅读')
                    page.wait_for_function('() => document.querySelector(".reading-chart").viewBox.baseVal.width>0')
                    initial=page.get_by_role('slider',name='每日阅读词数，选择日期').get_attribute('max')
                    assert initial=='29',initial
                    expect(page.locator('.chart-line.series-completedWords')).to_have_count(0)
                    page.get_by_role('checkbox',name='对比每日读完文章词数').check()
                    expect(page.locator('.chart-line.series-completedWords')).to_have_count(1)
                    page.get_by_role('combobox',name='图表日期范围').select_option('7')
                    expect(page.get_by_role('slider',name='每日阅读词数，选择日期')).to_have_attribute('max','6')
                    slider=page.get_by_role('slider',name='每日阅读速度，选择日期')
                    slider.focus();page.keyboard.press('Home')
                    expect(slider).to_have_attribute('aria-valuetext',__import__('re').compile('估算速度'))
                    page.get_by_role('combobox',name='图表日期范围').select_option('30')
                    page.locator('.reading-heatmap button').last.click()
                    expect(page.locator('.reading-calendar .reading-chart-detail')).to_contain_text('计时阅读')
                    page.get_by_role('searchbox',name='搜索已读文章').fill('untimed')
                    expect(page.locator('.completed-card')).to_have_count(1)
                    page.get_by_role('searchbox',name='搜索已读文章').fill('')
                    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                    if folder:=os.environ.get('KANDU_TEST_ARTIFACTS'):
                        target=Path(folder);target.mkdir(parents=True,exist_ok=True)
                        page.screenshot(path=str(target/('reading-analytics-phone.png' if mobile else 'reading-analytics-desktop.png')),full_page=True)
                    saved=activity.article_timing('sample',0)['activeMs']
                    now[0]+=30_000;page.clock.run_for(30_000)
                    assert activity.article_timing('sample',0)['activeMs']==saved,'Clock must stop after leaving reader'
                    context.close()
                browser.close()
            assert not errors,errors
            print('READING ACTIVITY BROWSER PASS: desktop + phone; vocabulary submission/generation/modal pause and success/error/stop/disconnect cleanup, heartbeat, manual/modal/idle pause, route cleanup, charts/range/keyboard/calendar/source distribution/search, no models or overflow')
        finally:
            server.shutdown();server.server_close();thread.join(timeout=5)
            WebTests.tearDownClass()


if __name__=='__main__':
    main()
