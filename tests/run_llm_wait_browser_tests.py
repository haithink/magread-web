"""Nonblocking reader LLM waits, completion placement and stale result isolation."""
import os
from pathlib import Path
import threading
from unittest.mock import patch
from wsgiref.simple_server import make_server

from playwright.sync_api import sync_playwright, expect
from run_web_tests import WebTests, config, store, gen, core_dict
from run_vocab_hover_browser_tests import word_point


TEXT='A candid explorer charted a new path through the world. The future requires patient reading and curiosity. '
ARTICLE=dict(index=0,title='Reading while AI works',paragraphs=[f'Paragraph {i}. '+TEXT*7 for i in range(24)])
ANALYSIS=dict(translation='一位坦率的探索者开辟了一条新路。',context='探索与阅读。',
              grammarComponents=[dict(text='A candid explorer',label='主语')])


def cached_vocab():
    return dict(articleIndex=0,level='cet4',titleEntries=[],idioms=[],
        paragraphs=[dict(index=i,startWords=p[:30],entries=[dict(word='candid',sense='Frank here')])
                    for i,p in enumerate(ARTICLE['paragraphs'])],
        processedParagraphs=[-1,*range(len(ARTICLE['paragraphs']))],
        phraseProcessedParagraphs=[-1,*range(len(ARTICLE['paragraphs']))],
        coverageVersion=gen.VOCAB_COVERAGE_VERSION,phraseCoverageVersion=gen.PHRASE_COVERAGE_VERSION)


def position(page):
    return page.evaluate('''() => ({y:scrollY,progress:document.querySelector('#reader-progress-bar').value})''')


def reading_point(page,paragraph=8,at_bottom=False):
    point=word_point(page,'candid',paragraph)
    page.evaluate('''({y,bottom}) => scrollBy({top:y-(bottom?innerHeight-50:innerHeight/2),behavior:'instant'})''',
                  dict(y=point['y'],bottom=at_bottom))
    return word_point(page,'candid',paragraph,scroll=False)


def analyze(page,paragraph=8,at_bottom=False):
    point=reading_point(page,paragraph,at_bottom)
    page.mouse.dblclick(point['x'],point['y'])
    expect(page.locator('#sheet-wait')).to_be_visible()
    expect(page.locator('#sheet')).not_to_be_visible()
    expect(page.locator('.sheet-wait-status')).to_contain_text('模型正在思考')


def screenshot(page,name):
    if folder:=os.environ.get('KANDU_TEST_ARTIFACTS'):
        target=Path(folder);target.mkdir(parents=True,exist_ok=True)
        page.screenshot(path=str(target/(name+'.png')))


def main():
    WebTests.setUpClass()
    store._atomic_json_write(store._cache_dir('sample')/'content.json',dict(bookSize=WebTests.meta['size'],
        format='epub',articles=[ARTICLE,dict(index=1,title='Another article',paragraphs=[TEXT])]))
    server=make_server('127.0.0.1',0,WebTests.app)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    errors=[]
    try:
        with (sync_playwright() as playwright,patch.object(core_dict,'ensure_db'),
              patch.object(core_dict,'lookup_many',return_value={}),
              patch.object(core_dict,'lookup',return_value=dict(word='candid',lemma='candid',translation='坦率的'))):
            chrome=os.environ.get('KANDU_TEST_CHROME')
            browser=playwright.chromium.launch(headless=True,**({'executable_path':chrome} if chrome else {}))
            for mobile in [False,True]:
                store.save_vocab('sample',cached_vocab())
                context=browser.new_context(viewport=dict(width=390 if mobile else 1440,height=900),
                                            is_mobile=mobile,has_touch=mobile)
                page=context.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
                jobs={};submissions=[]
                def start(route):
                    if route.request.method!='POST':route.continue_();return
                    job_id='reader-wait-'+str(len(jobs))
                    jobs[job_id]=dict(state='running',status='模型正在思考',result=ANALYSIS,error='模拟失败')
                    submissions.append((job_id,route.request.url,route.request.post_data_json))
                    route.fulfill(status=202,json=dict(jobId=job_id))
                def poll(route):
                    job=jobs[route.request.url.rsplit('/',1)[-1]]
                    if job['state']=='disconnect':route.abort('failed')
                    else:route.fulfill(json=job)
                for endpoint in ['analyze','lookup-context','discussion','generate-vocab']:
                    page.route('**/api/books/sample/articles/0/'+endpoint,start)
                page.route('**/api/jobs/reader-wait-*',poll)
                base=f'http://127.0.0.1:{server.server_port}'
                page.goto(base+'/#/read/sample/0')
                page.locator('#password').fill(WebTests.authcode);page.locator('#login-form button').click()
                expect(page.locator('#badge-8')).to_contain_text('1')
                analyze(page,at_bottom=mobile)
                page.wait_for_function("() => document.querySelector('.sheet-wait-status').textContent.includes('模型正在思考')")
                wait=page.locator('#sheet-wait')
                boxes=page.evaluate('''() => {
                    const wait=document.querySelector('#sheet-wait').getBoundingClientRect();
                    const range=state.sheetWait.range.getBoundingClientRect();
                    const body=document.querySelector('#reader-body').getBoundingClientRect();
                    return {wait:wait.toJSON(),range:range.toJSON(),body:body.toJSON(),width:innerWidth,height:innerHeight};
                }''')
                w,r=boxes['wait'],boxes['range']
                assert w['x']>=0 and w['right']<=boxes['width'] and w['top']>=0 and w['bottom']<=boxes['height'],boxes
                assert w['right']<=r['left'] or w['left']>=r['right'] or w['bottom']<=r['top'] or w['top']>=r['bottom'],boxes
                if not mobile:assert w['left']>=boxes['body']['right'],boxes
                else:assert w['top']<50,boxes  # The selected line is at the bottom.
                assert page.locator('#reader-body').evaluate('(n)=>!n.closest("[inert]")')
                expect(page.locator('#reader-reading-time')).to_contain_text('计时中')
                before=position(page)
                before_time=page.evaluate('state.reader.clock.elapsedMs')
                page.mouse.move(120,450);page.mouse.wheel(0,450)
                page.wait_for_timeout(1200)
                assert position(page)['y']>before['y']+200
                assert page.evaluate('state.reader.clock.elapsedMs')>before_time
                screenshot(page,'llm-wait-phone' if mobile else 'llm-wait-desktop')
                # Viewing partial content and docking it again preserves scroll.
                before=position(page)
                wait.get_by_role('button',name='查看',exact=True).click()
                expect(page.locator('#sheet')).to_be_visible()
                expect(page.locator('#reader-reading-time')).to_contain_text('弹窗暂停')
                page.locator('#sheet-background').click()
                expect(wait).to_be_visible();expect(page.locator('#sheet')).not_to_be_visible()
                assert position(page)==before
                # Completion must center the result at the CURRENT reading location.
                before=position(page);job_id=submissions[-1][0];jobs[job_id]['state']='done'
                expect(page.locator('.analysis-sentence')).to_have_text('A candid explorer charted a new path through the world.')
                expect(wait).to_have_count(0)
                bounds=page.locator('#sheet').bounding_box()
                # Native dialog centering excludes the reserved scrollbar gutter.
                assert abs(bounds['x']+bounds['width']/2-page.evaluate('innerWidth')/2)<9,bounds
                assert abs(bounds['y']+bounds['height']/2-450)<3,bounds
                assert position(page)==before
                expect(page.locator('#reader-reading-time')).to_contain_text('弹窗暂停')
                screenshot(page,'llm-result-phone' if mobile else 'llm-result-desktop')
                page.locator('#sheet-close').click()
                # Explicit dismissal, navigation and another sheet all suppress stale popups.
                analyze(page);job_id=submissions[-1][0]
                page.get_by_role('button',name='关闭等待提示').click();jobs[job_id]['state']='done'
                page.wait_for_function('() => !state.activeJobs.size')
                expect(page.locator('#sheet')).not_to_be_visible()
                analyze(page);job_id=submissions[-1][0]
                page.locator('#badge-8').click();expect(page.locator('#sheet-title')).to_contain_text('生词与短语')
                jobs[job_id]['state']='done';page.wait_for_function('() => !state.activeJobs.size')
                expect(page.locator('#sheet-title')).to_contain_text('生词与短语');page.locator('#sheet-close').click()
                analyze(page);job_id=submissions[-1][0]
                page.goto(base+'/#/read/sample/1');expect(page.locator('.reader-head h1')).to_have_text('Another article')
                jobs[job_id]['state']='done';page.wait_for_function('() => !state.activeJobs.size')
                expect(page.locator('#sheet')).not_to_be_visible();expect(wait).to_have_count(0)
                page.goto(base+'/#/read/sample/0');expect(page.locator('#badge-8')).to_contain_text('1')
                # Generation in an existing vocabulary sheet docks and retains its timing policy.
                page.locator('#badge-8').click()
                page.get_by_role('button',name='补充短语解释',exact=True).click()
                expect(wait).to_be_visible();expect(page.locator('#sheet')).not_to_be_visible()
                expect(page.locator('#reader-reading-time')).to_contain_text('生词生成暂停')
                expect(page.locator('.sheet-wait-status')).to_contain_text('模型正在思考')
                jobs[submissions[-1][0]]['state']='done'
                expect(page.locator('#sheet')).to_be_visible();expect(wait).to_have_count(0)
                expect(page.locator('#sheet-title')).to_contain_text('第 9 段');page.locator('#sheet-close').click()
                # Word meaning and discussion use the same nonblocking lifecycle.
                point=reading_point(page,8);page.mouse.dblclick(point['x'],point['y'])
                # This double-click launches sentence analysis; dismiss it and select via the toolbar.
                page.get_by_role('button',name='关闭等待提示').click()
                jobs[submissions[-1][0]]['state']='done';page.wait_for_function('() => !state.activeJobs.size')
                page.evaluate('''() => {
                    const p=document.querySelector('#para-8 p'),r=document.createRange();
                    const at=p.firstChild.textContent.indexOf('candid');r.setStart(p.firstChild,at);r.setEnd(p.firstChild,at+6);
                    getSelection().removeAllRanges();getSelection().addRange(r);
                }''')
                page.get_by_role('button',name='查词并收藏',exact=True).click()
                expect(wait).to_be_visible();expect(page.locator('#sheet')).not_to_be_visible()
                page.wait_for_function("() => document.querySelector('.sheet-wait-status').textContent.includes('模型正在思考')")
                jobs[submissions[-1][0]].update(state='done',result=dict(meaning=dict(sense='本文中指坦率的',explanation='根据语境解释。')))
                expect(page.locator('.context-sense')).to_have_text('本文中指坦率的');expect(wait).to_have_count(0)
                page.locator('#sheet-close').click()
                page.get_by_role('button',name='与 AI 讨论',exact=True).click()
                page.get_by_role('textbox',name='向 AI 提问').fill('Explain the point')
                page.get_by_role('button',name='发送问题 →').click()
                expect(wait).to_be_visible();expect(page.locator('#sheet')).not_to_be_visible()
                expect(page.locator('.sheet-wait-status')).to_contain_text('模型正在思考')
                jobs[submissions[-1][0]].update(state='done',text='A saved discussion answer.')
                expect(page.locator('#sheet')).to_be_visible()
                expect(page.locator('.answer-text')).to_have_text('A saved discussion answer.')
                page.locator('#sheet-close').click()
                # Error, stopped and disconnected jobs must leave no stuck wait card.
                for terminal in ['error','stopped','disconnect']:
                    analyze(page);jobs[submissions[-1][0]]['state']=terminal
                    expect(wait).to_have_count(0);expect(page.locator('#sheet')).to_be_visible()
                    expect(page.locator('#sheet-content .error')).to_be_visible();page.locator('#sheet-close').click()
                # A generated result may outlive a failed vocabulary reload.
                page.locator('#badge-8').click()
                page.get_by_role('button',name='补充短语解释',exact=True).click()
                expect(page.locator('.sheet-wait-status')).to_contain_text('模型正在思考')
                vocab_url='**/api/books/sample/articles/0/vocab'
                page.route(vocab_url,lambda route:route.fulfill(status=503,json=dict(error='模拟加载失败')))
                jobs[submissions[-1][0]]['state']='done'
                expect(wait).to_have_count(0);expect(page.locator('#sheet')).to_be_visible()
                expect(page.locator('#sheet-content .error')).to_contain_text('加载词表失败')
                page.unroute(vocab_url);page.locator('#sheet-close').click()
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                context.close()
            browser.close()
        assert not errors,errors
        print('LLM WAIT PASS: desktop/phone, avoidance, live reading, completion, scroll, timing, dismissal, stale tasks, word meaning, vocabulary, discussion and errors.')
    finally:
        server.shutdown();server.server_close();thread.join(timeout=5);WebTests.tearDownClass()


if __name__=='__main__':main()
