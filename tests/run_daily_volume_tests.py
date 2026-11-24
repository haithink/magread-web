"""Unified daily volume using temporary ledgers; no personal data or model calls."""
from datetime import datetime, timedelta, timezone
import unittest

from run_reading_activity_tests import ActivityTests, activity, reading_stats, WebTests


class DailyVolumeTests(unittest.TestCase):
    setUpClass = classmethod(ActivityTests.setUpClass.__func__)
    tearDownClass = classmethod(ActivityTests.tearDownClass.__func__)
    setUp = ActivityTests.setUp
    tearDown = ActivityTests.tearDown
    post = ActivityTests.post
    report = ActivityTests.report

    def stamp(self, days=0, hour=0):
        date = datetime.now(timezone.utc).date()+timedelta(days=days)
        return int(datetime.combine(date, datetime.min.time(), timezone.utc).timestamp()*1000)+hour*3_600_000

    def interval(self, when, words, source='web:sample:0', title='A candid view', duration=60_000):
        with activity.connection() as db:
            db.execute('INSERT INTO intervals(source_id,title_key,started_at,ended_at,active_ms,words) '
                'VALUES(?,?,?,?,?,?)', (source, reading_stats.title_key(title), when, when+duration, duration, words))

    def complete(self, when, words=2000, title='A candid view', source='chrome:sample'):
        data = reading_stats.load()
        reading_stats.merge(data, dict(id=source, kind='chrome', title=title, wordCount=words,
            readAt=when, source='example.invalid', url='https://example.invalid/'+source))
        reading_stats.save(data)

    def daily(self, days=365, offset=0):
        result = self.report(days=days, timezoneOffset=offset).json['analytics']
        return result, {d['date']:d for d in result['daily']}

    def day(self, stamp, offset=0):
        return datetime.fromtimestamp(stamp/1000, timezone(timedelta(minutes=-offset))).date().isoformat()

    def test_untimed_plugin_is_default_volume_and_reading_day_without_fake_speed(self):
        self.complete(self.stamp(), words=564, title='Spacecraft reaches Mercury')
        report, days = self.daily()
        today = days[self.day(self.stamp())]
        self.assertEqual((today['readWords'], today['completedWords'], today['completedArticles']), (564,564,1))
        self.assertEqual((today['measuredWords'], today['activeMs'], today['wordsPerMinute']), (0,0,None))
        self.assertEqual(report['activeDays'], 1)
        self.assertEqual(report['currentStreak'], 1)
        self.assertFalse(activity.database().exists())

    def test_completion_tops_up_on_its_day_and_retries_or_other_sources_do_not_duplicate(self):
        earlier, yesterday, today = self.stamp(-2), self.stamp(-1), self.stamp()
        self.interval(earlier,100)
        self.interval(yesterday,200)
        self.complete(today,1000)
        self.complete(today+1000,1000)
        self.complete(today+2000,1200,title='A CANDID VIEW!',source='chrome:duplicate')
        report, days = self.daily()
        self.assertEqual([days[self.day(t)]['readWords'] for t in [earlier,yesterday,today]], [100,200,900])
        self.assertEqual(sum(d['readWords'] for d in days.values()), report['readingVolume']['totalWords'])
        self.assertEqual(sum(d['completedArticles'] for d in days.values()), 1)
        self.assertEqual(report['totals'], activity.timing(300,120_000))

    def test_partial_sources_credit_only_increases_in_the_largest_cumulative_source(self):
        first, second, third = self.stamp(-2), self.stamp(-1), self.stamp()
        self.interval(first,100)
        self.interval(second,150,source='web:copy:0')
        self.interval(third,50)
        report, days = self.daily()
        self.assertEqual([days[self.day(t)]['readWords'] for t in [first,second,third]], [100,50,0])
        self.assertEqual(sum(d['readWords'] for d in days.values()), 150)
        self.assertEqual(report['readingVolume']['totalWords'], 150)
        self.assertEqual(report['totals']['measuredWords'], 300)

    def test_window_does_not_recredit_earlier_days(self):
        self.interval(self.stamp(-40),500)
        self.interval(self.stamp(-1),100)
        self.complete(self.stamp(),1000)
        short, days = self.daily(days=7)
        long, _ = self.daily()
        self.assertEqual(sum(d['readWords'] for d in days.values()), 500)
        self.assertEqual(days[self.day(self.stamp())]['readWords'], 400)
        self.assertEqual(short['readingVolume'], long['readingVolume'])

    def test_midnight_and_timezone_split_volume_without_changing_totals(self):
        start = self.stamp(-1)+86_390_000
        self.interval(start,90,duration=30_000)
        report, days = self.daily()
        self.assertEqual(days[self.day(start)]['readWords'], 30)
        self.assertEqual(days[self.day(start+30_000)]['readWords'], 60)
        shifted, shifted_days = self.daily(offset=-480)
        self.assertEqual(sum(d['readWords'] for d in shifted_days.values()), 90)
        self.assertEqual(sum(d['readWords']>0 for d in shifted_days.values()), 1)
        self.assertEqual(report['totals'], shifted['totals'])

    def test_completion_caps_old_samples_and_rereading_only_affects_timing(self):
        self.interval(self.stamp(-2),2500)
        self.complete(self.stamp(-1),2000)
        self.interval(self.stamp(),100)
        report, days = self.daily()
        self.assertEqual(days[self.day(self.stamp(-2))]['readWords'], 2000)
        self.assertEqual(days[self.day(self.stamp(-1))]['readWords'], 0)
        self.assertEqual(days[self.day(self.stamp())]['readWords'], 0)
        self.assertTrue(all(d['readWords']>=0 for d in days.values()))
        self.assertEqual(sum(d['readWords'] for d in days.values()), 2000)
        self.assertEqual(report['totals']['measuredWords'], 2600)

    def test_unmarking_rebuilds_existing_samples_and_keeps_other_articles(self):
        self.interval(self.stamp(-1),100)
        reading_stats.mark_web(WebTests.meta,self.art,True)
        self.complete(self.stamp(),564,title='Another article')
        self.assertEqual(sum(d['readWords'] for d in self.daily()[1].values()), 2564)
        reading_stats.mark_web(WebTests.meta,self.art,False)
        report, days = self.daily()
        self.assertEqual(sum(d['readWords'] for d in days.values()), 664)
        self.assertEqual(report['readingVolume']['totalWords'], 664)
        self.assertEqual(report['totals']['measuredWords'], 100)


if __name__ == '__main__':
    unittest.main(defaultTest='DailyVolumeTests',verbosity=2)
