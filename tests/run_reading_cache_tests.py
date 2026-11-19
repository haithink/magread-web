"""Cache HTTP policy, delayed progress and delayed clock replay; synthetic data only."""
import re
import unittest

from run_reading_activity_tests import ActivityTests


class CacheTests(ActivityTests):
    def test_static_versions_private_validation_and_sensitive_responses(self):
        html=self.client.get('/').text
        urls=re.findall(r'(\/static\/[^" ]+\?v=[a-f0-9]+)',html)
        self.assertGreaterEqual(len(urls),5)
        for url in urls:
            response=self.client.get(url)
            self.assertEqual(response.status_code,200)
            self.assertIn('immutable',response.headers['Cache-Control'])
            unchanged=self.client.get(url,headers={'If-None-Match':response.headers['ETag']})
            self.assertEqual(unchanged.status_code,304)
            self.assertIn('immutable',unchanged.headers['Cache-Control'])
        self.assertEqual(self.client.get('/static/app.js?v=old').headers['Cache-Control'],'no-cache')
        for path in ['/api/library','/api/books/sample','/api/books/sample/articles/0',
                     '/api/books/sample/articles/0/vocab']:
            response=self.client.get(path)
            self.assertEqual(response.headers['Cache-Control'],'private, no-cache')
            self.assertEqual(self.client.get(path,headers={'If-None-Match':response.headers['ETag']}).status_code,304)
            self.assertEqual(self.app.test_client().get(path,headers={'If-None-Match':response.headers['ETag']}).status_code,401)
        for path in ['/api/session','/api/settings','/api/settings/access','/api/reading-sync','/api/wordbook']:
            self.assertEqual(self.client.get(path).headers['Cache-Control'],'no-store')
        identity=self.client.get('/api/session').json
        self.assertTrue(identity['cacheScope'])
        self.assertIn('serverTime',identity)
        self.assertNotIn('cacheScope',self.app.test_client().get('/api/session').json)

    def test_delayed_progress_cannot_overwrite_a_newer_device(self):
        import time
        stamp=int(time.time()*1000)-60_000
        endpoint='/books/sample/articles/0/progress'
        first=self.post(endpoint,dict(paragraphIndex=4,paragraphOffset=0,observedAt=stamp))
        self.assertEqual(first.json['record']['lastReadAt'],stamp)
        newest=self.post(endpoint,dict(paragraphIndex=8,paragraphOffset=0,observedAt=stamp+30_000))
        old=self.post(endpoint,dict(paragraphIndex=2,paragraphOffset=0,observedAt=stamp+15_000))
        self.assertFalse(old.json['accepted'])
        self.assertEqual(old.json['record'],newest.json['record'])
        self.assertEqual(store_history(self)['paragraphIndex'],8)
        self.assertEqual(self.post(endpoint,dict(paragraphIndex=0,observedAt=stamp-8*86400_000)).status_code,400)

    def test_offline_clock_samples_replay_in_original_time_without_double_counting(self):
        started=self.now-180_000
        endpoint='/books/sample/articles/0/reading-time'
        sample=dict(sessionId='offline-clock-session-0001',paragraphIndex=0,paragraphOffset=0,
                    sequence=0,elapsedMs=0,sampledAt=started)
        self.assertEqual(self.post(endpoint,sample).json['acceptedMs'],0)
        for i in range(1,5):
            sample=dict(sample,sequence=i,elapsedMs=i*30_000,sampledAt=started+i*30_000,paragraphOffset=i*100)
            response=self.post(endpoint,sample)
            self.assertEqual(response.json['acceptedMs'],30_000,response.json)
            self.assertEqual(self.post(endpoint,sample).json['acceptedMs'],0)
        self.assertEqual(response.json['totalActiveMs'],120_000)
        self.assertEqual(self.post(endpoint,dict(sample,sequence=5,sampledAt=self.now+60_000)).status_code,400)

    def test_live_progress_is_small_private_and_does_not_write(self):
        endpoint='/api/books/sample/articles/0/progress'
        self.assertEqual(self.app.test_client().get(endpoint).status_code,401)
        initial=self.client.get(endpoint)
        self.assertEqual(initial.status_code,200)
        self.assertEqual(initial.headers['Cache-Control'],'no-store')
        self.assertIsNone(initial.json['record'])
        self.assertNotIn('article',initial.json)
        self.assertNotIn('word '*20,initial.text)
        saved=self.post('/books/sample/articles/0/progress',dict(paragraphIndex=8,paragraphOffset=35)).json['record']
        snapshot=self.client.get(endpoint).json
        self.assertEqual(snapshot['record'],saved)
        self.assertEqual(snapshot['contentVersion'],self.client.get('/api/books/sample/articles/0').json['contentVersion'])
        self.assertEqual(store_history(self),saved)

    def test_active_old_device_cannot_overwrite_a_new_progress_until_resolved(self):
        import time
        endpoint='/books/sample/articles/0/progress'
        stamp=int(time.time()*1000)-60_000
        first=self.post(endpoint,dict(paragraphIndex=2,paragraphOffset=0,observedAt=stamp,basedOnReadAt=0)).json['record']
        pc=self.post(endpoint,dict(paragraphIndex=8,paragraphOffset=30,observedAt=stamp+20_000,
                                  basedOnReadAt=first['lastReadAt'])).json['record']
        # A stale phone makes a new observation with a later timestamp, rather
        # than an old offline replay. It must still leave the PC position intact.
        old=self.post(endpoint,dict(paragraphIndex=3,paragraphOffset=0,observedAt=stamp+40_000,
                                   basedOnReadAt=first['lastReadAt']))
        self.assertFalse(old.json['accepted'])
        self.assertEqual(old.json['record'],pc)
        self.assertEqual(store_history(self),pc)
        # Explicitly choosing to keep reading here acknowledges the remote base.
        chosen=self.post(endpoint,dict(paragraphIndex=3,paragraphOffset=0,observedAt=stamp+45_000,
                                      basedOnReadAt=pc['lastReadAt']))
        self.assertNotEqual(chosen.json.get('accepted'),False)
        self.assertEqual(store_history(self)['paragraphIndex'],3)
        # Acknowledged remote progress establishes order even if the phone's
        # server-time estimate lags the PC's because its login reply was slow.
        behind=self.post(endpoint,dict(paragraphIndex=4,paragraphOffset=0,observedAt=stamp+35_000,
                                      basedOnReadAt=chosen.json['record']['lastReadAt']))
        self.assertNotEqual(behind.json.get('accepted'),False)
        self.assertGreater(behind.json['record']['lastReadAt'],chosen.json['record']['lastReadAt'])
        self.assertEqual(self.post(endpoint,dict(paragraphIndex=0,basedOnReadAt=True)).status_code,400)


def store_history(test):
    return test.client.get('/api/books/sample/articles/0').json['history']


if __name__=='__main__':
    # Only this suite; ActivityTests is imported as a fixture, not duplicated.
    result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(CacheTests))
    raise SystemExit(not result.wasSuccessful())
