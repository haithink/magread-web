"""Scrollbar jumps, offline replay and explicit completion; synthetic books only."""
import os
import threading
import types
from unittest.mock import patch

from playwright.sync_api import sync_playwright, expect
from werkzeug.serving import make_server

from browser_helpers import reader_button
from run_web_tests import WebTests, config, store, llm
from webapp import reading_activity as activity, reading_stats
import webapp.server as server_module


def main():
    WebTests.setUpClass()
    article = dict(index=0, title='Navigation is not reading', paragraphs=['word '*100 for _ in range(30)])
    store._atomic_json_write(store._cache_dir('sample')/'content.json', dict(
        bookSize=WebTests.meta['size'], format='epub', articles=[article]))
    now = [server_module.time.time_ns()//1_000_000]
    fake_time = types.SimpleNamespace(time_ns=lambda: now[0]*1_000_000, time=lambda: now[0]/1000,
        monotonic=server_module.time.monotonic)
    errors = []
    with patch.object(activity, 'time', fake_time), patch.object(server_module, 'time', fake_time), \
            patch.object(llm, 'conversation', side_effect=AssertionError('No paid models')):
        server = make_server('127.0.0.1', 0, WebTests.app, threaded=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True);thread.start()
        try:
            with sync_playwright() as p:
                engine = os.environ.get('KANDU_TEST_ENGINE', 'chromium')
                browser = getattr(p, engine).launch(headless=True, **(
                    {'executable_path': os.environ['KANDU_TEST_CHROME']} if engine == 'chromium' else {}))
                expect.set_options(timeout=15000)
                for mobile in [False, True]:
                    for name in ['reading-stats.json',
                                 'reading_history/sample.json', 'article_states/sample.json']:
                        (config.app_dir()/name).unlink(missing_ok=True)
                    before = activity.article_timing('sample', 0)['measuredWords']
                    context = browser.new_context(base_url=f'http://127.0.0.1:{server.server_port}',
                        viewport=dict(width=393 if mobile else 1440,
                        height=852 if mobile else 1000), is_mobile=mobile, has_touch=mobile,
                        service_workers='block')
                    page = context.new_page();page.on('pageerror', lambda e: errors.append(str(e)))
                    page.goto(f'http://127.0.0.1:{server.server_port}/#/read/sample/0')
                    page.locator('#password').fill(WebTests.authcode);page.locator('#login-form button').click()
                    page.wait_for_function('() => state.reader?.ready && ReadingSync.pending()===0')

                    def pulse(delta, position=None):
                        now[0] += delta
                        page.evaluate('''async ({stamp,delta,position}) => {
                            ReadingSync.now=()=>stamp;
                            state.reader.clock.elapsedMs+=delta;
                            await flushReadingClock(state.reader,false,position);
                        }''', dict(stamp=now[0], delta=delta, position=position))
                        page.wait_for_function('() => ReadingSync.pending()===0')

                    def jump(bottom):
                        now[0] += 1000
                        page.evaluate('''({stamp,bottom}) => {
                            ReadingSync.now=()=>stamp;state.reader.clock.elapsedMs+=1000;
                            scrollTo({top:bottom?document.documentElement.scrollHeight:0,behavior:'instant'});
                        }''', dict(stamp=now[0], bottom=bottom))
                        page.wait_for_timeout(400)
                        page.wait_for_function('() => !state.reader.clock.navigation && ReadingSync.pending()===0')
                        page.evaluate('saveProgress()')
                        page.wait_for_function('() => ReadingSync.pending()===0')

                    # Waiting cannot build up a word budget for dragging to end.
                    pulse(120_000);pulse(120_000)
                    jump(True)
                    report = page.request.get('/api/reading-stats').json()
                    assert report['articleCount'] == 0, report
                    assert report['analytics']['readingVolume']['totalWords'] == before
                    assert activity.article_timing('sample', 0)['measuredWords'] == before
                    assert store.load_reading_history('sample')['0']['atEnd']
                    assert store.load_reading_history('sample')['0']['completionMode'] == 'explicit'
                    assert page.evaluate('state.reader.state') == 'unread'
                    page.evaluate("location.hash='#/book/sample'")
                    expect(page.locator('.article-card select')).to_have_value('unread')
                    page.wait_for_function('() => ReadingSync.pending()===0')
                    page.evaluate("location.hash='#/read/sample/0'")
                    page.wait_for_function('() => state.reader?.ready && ReadingSync.pending()===0')

                    # Returning to the reading position allows normal progress.
                    jump(False)
                    pulse(30_000, dict(paragraphIndex=1, paragraphOffset=0))
                    assert activity.article_timing('sample', 0)['measuredWords'] == before+100
                    # Saving in the same JS turn as scrollTo precedes the scroll
                    # event (as pagehide/navigation can). It must also be safe.
                    now[0] += 1000
                    page.evaluate('''async stamp => {
                        ReadingSync.now=()=>stamp;state.reader.clock.elapsedMs+=1000;
                        scrollTo({top:document.documentElement.scrollHeight,behavior:'instant'});
                        await flushReadingClock();
                    }''', now[0])
                    page.wait_for_timeout(400)
                    page.wait_for_function('() => ReadingSync.pending()===0')
                    assert activity.article_timing('sample', 0)['measuredWords'] == before+100
                    jump(False)
                    # The same jump must remain navigation in the offline journal.
                    context.set_offline(True)
                    now[0] += 1000
                    page.evaluate('''stamp => {
                        ReadingSync.now=()=>stamp;state.reader.clock.elapsedMs+=1000;
                        scrollTo({top:document.documentElement.scrollHeight,behavior:'instant'});
                    }''', now[0])
                    page.wait_for_timeout(400)
                    queued = page.evaluate('ReadingSync.entries()')
                    assert any(i['data'].get('navigation') for i in queued if i['path'].endswith('/reading-time'))
                    context.set_offline(False)
                    page.evaluate('ReadingSync.drain()')
                    page.wait_for_function('() => ReadingSync.pending()===0')
                    assert activity.article_timing('sample', 0)['measuredWords'] == before+100
                    jump(False)
                    # Explicit completion always credits all 3,000 words at top.
                    reader_button(page, '标记已读').click()
                    expect(page.locator('#reader-reading-time')).to_contain_text('已读停计时')
                    jump(True);jump(False)
                    report = page.request.get('/api/reading-stats').json()
                    assert report['articleCount'] == 1
                    assert report['analytics']['readingVolume']['totalWords'] == 3000
                    assert activity.article_timing('sample', 0)['measuredWords'] == before+100
                    assert report['analytics']['totals']['measuredWords'] == before+100
                    page.evaluate("location.hash='#/reading-stats'")
                    expect(page.locator('[data-stat="words"]')).to_have_text('3,000')
                    page.wait_for_function('() => ReadingSync.pending()===0')
                    context.close()
                browser.close()
            assert not errors, errors
            print(f'READING NAVIGATION PASS ({engine}): desktop + phone, long wait, end/back jumps, '
                  'normal reading recovery, offline navigation replay, explicit full-word completion')
        finally:
            server.shutdown();server.server_close();thread.join(timeout=5)
            WebTests.tearDownClass()


if __name__ == '__main__':
    main()
