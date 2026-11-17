"""Isolated clock/ledger tests: fake time, temporary publications, no network/LLM."""
from datetime import datetime, timedelta, timezone
import types
import unittest
from unittest.mock import patch

from run_web_tests import WebTests, config, store
from webapp import reading_activity as activity, reading_stats


class ActivityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        WebTests.setUpClass()
        cls.app, cls.authcode = WebTests.app, WebTests.authcode
        cls.config_password = WebTests.config_password

    @classmethod
    def tearDownClass(cls):
        WebTests.tearDownClass()

    post = WebTests.post

    def setUp(self):
        WebTests.setUp(self)
        self.files = {}
        for name in ['reading-activity.sqlite3','reading-stats.json','article_states/sample.json',
                'reading_history/sample.json','cache/sample/content.json']:
            path=config.app_dir()/name
            self.files[path]=path.read_bytes() if path.exists() else None
            path.unlink(missing_ok=True)
        self.art=dict(index=0,title='A candid view',paragraphs=['word '*100 for _ in range(20)])
        self.other=dict(self.art,index=1,title='A second article')
        store._atomic_json_write(store._cache_dir('sample')/'content.json',dict(
            bookSize=WebTests.meta['size'],format='epub',articles=[self.art,self.other]))
        self.now=int(datetime.now(timezone.utc).timestamp()*1000)-3_600_000
        self.clock=patch.object(activity,'time',types.SimpleNamespace(time_ns=lambda:self.now*1_000_000))
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.session='clock-session-00000001'

    def tearDown(self):
        for path,content in self.files.items():
            if content is None:
                path.unlink(missing_ok=True)
            else:
                path.parent.mkdir(parents=True,exist_ok=True)
                path.write_bytes(content)

    def pulse(self,sequence=0,elapsed=0,words=0,session=None,index=0):
        para,offset=divmod(words,100)
        if para>=20:para,offset=19,100
        return self.post(f'/books/sample/articles/{index}/reading-time',dict(sessionId=session or self.session,
            sequence=sequence,elapsedMs=elapsed,paragraphIndex=para,paragraphOffset=offset*5))

    def report(self,**params):
        return self.client.get('/api/reading-stats',query_string=params)

    def test_retries_and_reordered_counters_are_idempotent_and_do_not_change_progress(self):
        self.assertEqual(self.pulse().status_code,200)
        self.now+=30_000
        response=self.pulse(1,30_000,100)
        self.assertEqual(response.json,dict(acceptedMs=30_000,acceptedWords=100,totalActiveMs=30_000))
        self.assertEqual(self.pulse(1,30_000,100).json['acceptedMs'],0)
        self.assertEqual(self.pulse(0,0,0).json['acceptedMs'],0)
        # The server total also reconciles a committed request whose reply was lost.
        retried=self.pulse(2,30_000,100).json
        self.assertEqual(retried['totalActiveMs'],30_000)
        self.assertEqual(retried['acceptedMs'],0)
        timing=activity.article_timing('sample',0)
        self.assertEqual(timing,dict(activeMs=30_000,measuredWords=100,wordsPerMinute=200))
        self.assertEqual(store.load_reading_history('sample'),{})
        self.assertEqual(store.load_article_states('sample'),{})
        self.assertEqual(self.report().json['articleCount'],0)
        self.post('/books/sample/state',dict(articleIndex=0,state='read'))
        stats=self.report().json
        self.assertEqual(stats['articles'][0]['timing'],timing)
        self.assertEqual(sum(d['measuredWords'] for d in stats['analytics']['daily']),100)
        reader=self.client.get('/api/books/sample/articles/0').json
        self.assertEqual(reader['readingTiming'],timing)

    def test_server_wall_clock_caps_elapsed_and_overlap_across_tabs(self):
        second='clock-session-00000002'
        self.pulse();self.pulse(session=second)
        self.now+=30_000
        first=self.pulse(1,600_000,100)
        overlapping=self.pulse(1,30_000,100,session=second)
        self.assertEqual(first.json['acceptedMs'],30_000)
        self.assertEqual(overlapping.json,dict(acceptedMs=0,acceptedWords=0,totalActiveMs=30_000))
        self.now+=600_000
        capped=self.pulse(2,1_200_000,200)
        self.assertEqual(capped.json['acceptedMs'],activity.MAX_INTERVAL_MS)
        self.assertEqual(activity.article_timing('sample',0)['activeMs'],150_000)

    def test_returning_to_previous_paragraph_does_not_count_same_words_again(self):
        self.pulse()
        for sequence,words in [(1,100),(2,50),(3,120)]:
            self.now+=30_000
            self.pulse(sequence,sequence*30_000,words)
        self.assertEqual(activity.article_timing('sample',0)['measuredWords'],120)
        # A deliberately reopened reading from the start is a new reading pass.
        second='clock-session-00000002'
        self.pulse(session=second)
        self.now+=30_000
        self.pulse(1,30_000,100,session=second)
        self.assertEqual(activity.article_timing('sample',0)['measuredWords'],220)

    def test_restored_position_and_instant_end_jump_do_not_invent_read_words(self):
        self.pulse(words=1000)
        self.now+=1000
        result=self.pulse(1,1000,2000)
        self.assertEqual(result.json,dict(acceptedMs=1000,acceptedWords=0,totalActiveMs=1000))
        timing=activity.article_timing('sample',0)
        self.assertIsNone(timing['wordsPerMinute'])
        self.assertEqual(timing['measuredWords'],0)

    def test_stationary_reading_time_is_available_when_reader_finally_scrolls(self):
        self.pulse()
        self.now+=15_000;self.pulse(1,15_000,0)
        self.now+=15_000;self.pulse(2,30_000,0)
        self.now+=15_000
        result=self.pulse(3,45_000,450)
        self.assertEqual(result.json['acceptedWords'],450)
        self.assertEqual(activity.article_timing('sample',0)['wordsPerMinute'],600)

    def test_midnight_is_split_and_timezone_offset_changes_daily_buckets(self):
        yesterday=datetime.now(timezone.utc).date()-timedelta(days=1)
        self.now=int(datetime.combine(yesterday,datetime.min.time(),timezone.utc).timestamp()*1000)+86_390_000
        self.pulse()
        self.now+=30_000
        self.pulse(1,30_000,90)
        daily={d['date']:d for d in self.report(timezoneOffset=0).json['analytics']['daily']}
        self.assertEqual(daily[yesterday.isoformat()]['activeMs'],10_000)
        self.assertEqual(daily[yesterday.isoformat()]['measuredWords'],30)
        self.assertEqual(daily[(yesterday+timedelta(days=1)).isoformat()]['activeMs'],20_000)
        self.assertEqual(daily[(yesterday+timedelta(days=1)).isoformat()]['measuredWords'],60)
        shifted=self.report(timezoneOffset=-480).json['analytics']
        self.assertEqual(sum(d['activeMs']>0 for d in shifted['daily']),1)
        self.assertEqual(sum(d['measuredWords'] for d in shifted['daily']),90)

    def test_old_completion_and_plugin_metadata_have_no_fabricated_timing(self):
        now=int(datetime.now(timezone.utc).timestamp()*1000)
        data=reading_stats.load()
        old=reading_stats.web_source(WebTests.meta,self.art,now-2*86_400_000)
        reading_stats.merge(data,old)
        reading_stats.merge(data,reading_stats.web_source(WebTests.meta,self.art,now-86_400_000))
        chrome=dict(id='chrome:fake',kind='chrome',title='A CANDID VIEW!',wordCount=2100,
            source='example.invalid',url='https://example.invalid/a',readAt=now)
        reading_stats.merge(data,chrome);reading_stats.save(data)
        report=self.report().json
        self.assertEqual(report['totalWords'],2100)
        self.assertEqual(report['articleCount'],1)
        self.assertEqual(report['articles'][0]['timing'],activity.timing())
        self.assertFalse(activity.database().exists())
        first=datetime.fromtimestamp(old['firstReadAt']/1000,timezone.utc).date().isoformat()
        daily={d['date']:d for d in report['analytics']['daily']}
        self.assertEqual(daily[first]['completedWords'],2100)
        self.assertEqual(sum(d['completedWords'] for d in daily.values()),2100)
        self.assertTrue(all(d['wordsPerMinute'] is None for d in daily.values()))

    def test_speed_is_weighted_by_duration_and_includes_in_progress_articles(self):
        self.pulse()
        self.now+=30_000;self.pulse(1,30_000,100)
        second='clock-session-00000002'
        self.pulse(session=second,index=1)
        self.now+=60_000;self.pulse(1,60_000,50,session=second,index=1)
        stats=self.report().json
        self.assertEqual(stats['articleCount'],0)
        self.assertEqual(stats['analytics']['totals']['wordsPerMinute'],100)
        self.assertEqual(stats['analytics']['totals']['activeMs'],90_000)
        self.assertEqual(stats['analytics']['totals']['measuredWords'],150)
        self.assertEqual(stats['analytics']['activeDays'],1)

    def test_auth_and_invalid_payloads_do_not_modify_ledger(self):
        path='/api/books/sample/articles/0/reading-time'
        self.assertEqual(self.app.test_client().post(path,json={}).status_code,401)
        self.assertEqual(self.client.post(path,json={}).status_code,403)
        base=dict(sessionId=self.session,sequence=0,elapsedMs=0,paragraphIndex=0,paragraphOffset=0)
        for change in [dict(sessionId='../bad'),dict(sequence=True),dict(sequence=-1),dict(elapsedMs=True),
                dict(elapsedMs=-1),dict(elapsedMs=10**20),dict(paragraphIndex=1000),dict(paragraphOffset=-1)]:
            response=self.post(path[4:],{**base,**change})
            self.assertEqual(response.status_code,400,response.json)
        self.assertFalse(activity.database().exists())
        self.pulse()
        self.assertEqual(self.pulse(1,100,0,index=1).status_code,400)
        self.now+=1000;self.pulse(1,1000,0)
        self.assertEqual(self.pulse(2,0,0).status_code,400)
        for params in [dict(days='bad'),dict(days=10000),dict(timezoneOffset=2000)]:
            self.assertEqual(self.report(**params).status_code,400)
        self.assertEqual(len(self.report(days=7).json['analytics']['daily']),7)


if __name__=='__main__':
    unittest.main(defaultTest='ActivityTests',verbosity=2)
