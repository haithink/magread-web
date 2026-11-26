"""Default volume chart/calendar with real plugin uploads; isolated library only."""
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import threading

from playwright.sync_api import sync_playwright, expect
from werkzeug.serving import make_server

from run_web_tests import WebTests
from run_daily_volume_tests import DailyVolumeTests
from webapp import reading_activity as activity, reading_stats


def main():
    DailyVolumeTests.setUpClass()
    fixture = DailyVolumeTests()
    fixture.setUp()
    now = int(datetime.now(timezone.utc).timestamp()*1000)
    fixture.interval(now-60_000,100)
    server = make_server('127.0.0.1',0,WebTests.app,threaded=True)
    thread = threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    base = f'http://127.0.0.1:{server.server_port}'
    errors = []
    try:
        with sync_playwright() as p:
            engine = os.environ.get('KANDU_TEST_ENGINE','chromium')
            expect.set_options(timeout=30_000 if engine=='webkit' else 5000)
            browser = getattr(p,engine).launch(headless=True,**(
                {'executable_path':os.environ['KANDU_TEST_CHROME']} if engine=='chromium' else {}))
            for index, mobile in enumerate([False,True]):
                context = browser.new_context(viewport=dict(width=393 if mobile else 1440,height=852 if mobile else 1000),
                    is_mobile=mobile,has_touch=mobile,timezone_id='Asia/Shanghai',service_workers='block')
                page = context.new_page()
                page.on('pageerror',lambda error:errors.append(str(error)))
                page.goto(base+'/#/reading-stats')
                page.locator('#password').fill(WebTests.authcode)
                page.locator('#login-form button').click()
                expect(page.locator('.chart-line.series-readWords')).to_have_count(1)
                before = page.request.get(base+'/api/reading-stats?days=365&timezoneOffset=-480').json()['analytics']
                # Upload twice as an extension would; no timer payload is supplied.
                page.evaluate('''async ({index,now}) => {
                    const {token}=await api('/reading-sync',{method:'POST',data:{name:'Browser fixture'}});
                    const articles=[{title:'An untimed plugin article '+index,url:'https://example.invalid/'+index,
                        wordCount:564,readAt:now}];
                    for(let i=0;i<2;i++) {
                        const response=await fetch('/api/reading-sync/import',{method:'POST',
                            headers:{'Content-Type':'application/json',Authorization:'Bearer '+token},
                            body:JSON.stringify({articles})});
                        if(!response.ok)throw new Error('Fixture upload failed');
                    }
                    await route({refresh:true});
                }''',dict(index=index,now=now))
                after = page.request.get(base+'/api/reading-stats?days=365&timezoneOffset=-480').json()['analytics']
                today = after['daily'][-1]
                self_total = 100+564*(index+1)
                assert sum(d['readWords'] for d in after['daily'])==self_total
                assert after['totals']==before['totals']
                chart = page.locator('.reading-chart-panel').filter(has=page.get_by_role('heading',name='每日阅读词数',exact=True))
                expect(chart.locator('.chart-line.series-readWords')).to_have_count(1)
                expect(chart.locator('.reading-chart-detail')).to_contain_text(f"阅读词数 {today['readWords']:,} 词")
                expect(page.get_by_role('checkbox',name='对比每日读完文章词数')).not_to_be_checked()
                expect(page.locator('#reading-period-summary')).to_contain_text(f'阅读词数 {self_total:,} 词')
                cell = page.locator(f'.reading-heatmap [data-date="{today["date"]}"]')
                expect(cell).to_have_attribute('class',__import__('re').compile(r'heat-level-[1-4]'))
                cell.click()
                expect(page.locator('.reading-calendar .reading-chart-detail')).to_contain_text(f"阅读词数 {today['readWords']:,} 词")
                calendar=page.locator('.reading-calendar')
                expect(calendar.get_by_text('最近一年',exact=True)).to_be_visible()
                expect(calendar.locator('.reading-heatmap button')).to_have_count(365)
                expect(calendar.locator('.calendar-summary')).to_contain_text(f'{self_total:,} 词')
                expect(calendar.locator('.heatmap-months')).to_contain_text('月')
                # Sunday-first alignment and trailing placeholders keep actual
                # dates in the right weekday; future squares are not clickable.
                first_date=after['daily'][0]['date']
                leading=(datetime.fromisoformat(first_date).weekday()+1)%7
                assert calendar.locator('.reading-heatmap button').first.evaluate('(cell)=>[...cell.parentElement.children].indexOf(cell)')==leading
                expect(calendar.locator('.heatmap-weekdays')).to_have_text('周一周三周五')
                cell.focus()
                page.keyboard.press('ArrowLeft')
                selected=(datetime.fromisoformat(today['date'])-timedelta(days=7)).date().isoformat()
                expect(calendar.locator(f'[data-date="{selected}"]')).to_be_focused()
                expect(calendar.locator('.reading-chart-detail')).to_contain_text(selected)
                page.keyboard.press('End')
                expect(cell).to_be_focused()
                if mobile:
                    assert calendar.locator('.heatmap-scroll').evaluate('(node)=>node.scrollWidth>node.clientWidth&&node.scrollLeft>0')
                page.get_by_role('combobox',name='图表日期范围').select_option('7')
                expect(chart.get_by_role('slider')).to_have_attribute('max','6')
                expect(calendar.locator('.reading-heatmap button')).to_have_count(365)
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                # Old offline snapshots have no daily readWords field. They
                # explicitly fall back to completed words until online refresh.
                page.evaluate('''async () => {
                    const value=(await api('/reading-stats?days=365&timezoneOffset=-480',{fresh:true})).analytics;
                    value.daily.forEach(day=>delete day.readWords);
                    window.legacyPanel=readingAnalyticsPanel(value);
                    document.querySelector('main').append(legacyPanel);
                }''')
                legacy = page.locator('.reading-analytics').last
                expect(legacy).to_contain_text('当前为旧版缓存')
                expect(legacy.locator('.chart-line.series-completedWords')).to_have_count(1)
                assert 'NaN' not in legacy.inner_text()
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                page.evaluate('legacyPanel.cleanup();legacyPanel.remove()')
                # Dense, entirely synthetic year for visual inspection and a
                # leap-day regression, independent of the real library.
                page.evaluate('''async () => {
                    const value=structuredClone((await api('/reading-stats?days=365&timezoneOffset=-480')).analytics);
                    const start=Date.UTC(2024,0,2,12);
                    value.daily=Array.from({length:365},(_,i)=>{
                        const readWords=i%7===0?0:[450,2400,5600,11000][i%4];
                        return {date:new Date(start+i*86400000).toISOString().slice(0,10),readWords,
                            activeMs:readWords*300,measuredWords:readWords,completedWords:0,completedArticles:0,wordsPerMinute:readWords?200:null};
                    });
                    window.previewPanel=readingAnalyticsPanel(value);
                    document.querySelector('main').replaceChildren(previewPanel);
                }''')
                preview=page.locator('.reading-calendar')
                leap=preview.locator('[data-date="2024-02-29"]')
                expect(leap).to_have_count(1)
                assert leap.evaluate('(cell)=>[...cell.parentElement.children].indexOf(cell)%7')==4
                expect(preview.locator('.calendar-date-range')).to_have_text('2024-01-02 — 2024-12-31')
                labels=preview.locator('.heatmap-months span').evaluate_all('(nodes)=>nodes.map(node=>({x:node.offsetLeft,y:node.offsetTop}))')
                assert len(labels)==12 and len({label['y'] for label in labels})==1,labels
                assert all(a['x']<b['x'] for a,b in zip(labels,labels[1:])),labels
                size=preview.locator('.reading-heatmap button').first.bounding_box()
                assert size['width']==size['height'],size
                if folder:=os.environ.get('KANDU_TEST_ARTIFACTS'):
                    target=Path(folder);target.mkdir(parents=True,exist_ok=True)
                    preview.screenshot(path=str(target/f'calendar-{engine}-{"phone" if mobile else "desktop"}.png'))
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                page.evaluate('previewPanel.cleanup()')
                context.close()
            browser.close()
        assert not errors, errors
        print(f'DAILY VOLUME BROWSER PASS ({engine}): desktop/phone default unified curve, plugin/retry, calendar color, period/range, unchanged speed, old cache, no overflow/models')
    finally:
        server.shutdown();server.server_close();thread.join(timeout=5)
        fixture.tearDown()
        fixture.doCleanups()
        DailyVolumeTests.tearDownClass()


if __name__ == '__main__':
    main()
