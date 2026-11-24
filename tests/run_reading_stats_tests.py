"""Isolated completed-reading and restricted plugin-sync contract tests."""
import hashlib
from concurrent.futures import ThreadPoolExecutor
import threading
import time
import unittest
from unittest.mock import patch

from run_web_tests import WebTests, config, store
from webapp import reading_stats
from webapp.reading_sync import SyncDevices


class ReadingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        WebTests.setUpClass()
        cls.app, cls.authcode = WebTests.app, WebTests.authcode
        cls.config_password = WebTests.config_password

    @classmethod
    def tearDownClass(cls):
        WebTests.tearDownClass()

    def setUp(self):
        WebTests.setUp(self)
        self.saved = {}
        for name in ['reading-stats.json', 'reading-sync-auth.json', 'article_states/sample.json', 'reading_history/sample.json']:
            path = config.app_dir() / name
            self.saved[path] = path.read_bytes() if path.exists() else None
            path.unlink(missing_ok=True)

    def tearDown(self):
        for path, content in self.saved.items():
            if content is None:
                path.unlink(missing_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)

    post = WebTests.post

    def stats(self):
        response = self.client.get('/api/reading-stats')
        self.assertEqual(response.status_code, 200)
        return response.json

    def pair(self):
        token = self.post('/reading-sync', {}).json['token']
        return {'Authorization': 'Bearer ' + token, 'Origin': 'chrome-extension://test'}

    def record(self, **changes):
        return dict(title='A Candid View!', url='https://example.invalid/article#paragraph', wordCount=100,
                    readAt=int(time.time()*1000), **changes)

    def test_scroll_to_end_never_completes_but_explicit_completion_persists(self):
        self.assertEqual(self.stats()['articleCount'], 0)
        self.post('/books/sample/articles/0/progress', dict(paragraphIndex=0, paragraphOffset=4))
        self.assertEqual(self.stats()['articleCount'], 0)
        self.post('/books/sample/articles/0/progress', dict(paragraphIndex=1, paragraphOffset=999))
        self.assertEqual(self.stats()['articleCount'], 0)
        self.assertEqual(store.load_article_states('sample'), {})
        history = store.load_reading_history('sample')['0']
        self.assertTrue(history['atEnd'])
        self.assertEqual(history['completionMode'], 'explicit')
        # Even a statistics rebuild must not treat a saved end viewport as read.
        (config.app_dir() / 'reading-stats.json').unlink(missing_ok=True)
        self.assertEqual(self.stats()['articleCount'], 0)
        self.post('/books/sample/state', dict(articleIndex=0, state='read'))
        completed = self.stats()
        self.assertEqual(completed['articleCount'], 1)
        self.assertEqual(completed['totalWords'], 7)
        self.post('/books/sample/articles/0/progress', dict(paragraphIndex=0, paragraphOffset=1))
        self.assertEqual(self.stats()['articleCount'], 1)
        self.post('/books/sample/state', dict(articleIndex=0, state='unread'))
        self.assertEqual(self.stats()['articleCount'], 0)
        self.post('/books/sample/state', dict(articleIndex=0, state='read'))
        self.assertEqual(self.stats()['articleCount'], 1)

    def test_title_dedup_across_publications_and_chrome_idempotent_retries(self):
        self.post('/books/sample/state', dict(articleIndex=0, state='read'))
        meta = dict(WebTests.meta, id='duplicate', fileName='Another publication.epub')
        data = reading_stats.load()
        reading_stats.merge(data, reading_stats.web_source(meta, WebTests.article))
        reading_stats.save(data)
        headers = self.pair()
        client = self.app.test_client()
        record = self.record()
        for _ in range(2):
            response = client.post('/api/reading-sync/import', json={'articles':[record]}, headers=headers)
            self.assertEqual(response.status_code, 200, response.json)
        stats = self.stats()
        self.assertEqual((stats['articleCount'], stats['totalWords']), (1, 100))
        self.assertEqual(len(stats['articles'][0]['sources']), 3)
        self.assertEqual(stats['articles'][0]['sources'][-1]['url'], 'https://example.invalid/article')
        self.assertEqual(reading_stats.title_key('You’ve  Never—Heard'), reading_stats.title_key("YOU'VE NEVER HEARD!"))

    def test_legacy_completion_migrates_without_parsing_or_models(self):
        store.set_article_state('sample', 0, 'read')
        with patch.object(store, 'ensure_content', side_effect=AssertionError('Must use caches')):
            self.assertEqual(self.stats()['articleCount'], 1)
        self.post('/books/sample/state', dict(articleIndex=0, state='unread'))
        self.assertEqual(self.stats()['articleCount'], 0)
        self.post('/books/sample/articles/0/progress', dict(paragraphIndex=1, paragraphOffset=999))
        self.assertEqual(self.stats()['articleCount'], 0)
        # Real legacy evidence is still migrated, before explicit marking was
        # required. Existing completion records are never silently deleted.
        old = store.load_reading_history('sample')['0']
        old.pop('completionMode')
        store._atomic_json_write(config.app_dir() / 'reading-stats.json', dict(sources={}, excluded=[]))
        store._atomic_json_write(config.app_dir() / 'article_states/sample.json', {})
        store.save_reading_progress('sample', 0, old)
        self.assertEqual(self.stats()['articleCount'], 1)
        self.post('/books/sample/state', dict(articleIndex=0, state='uninterested'))
        self.assertEqual(self.stats()['articleCount'], 0)

    def test_sync_token_is_hashed_revocable_and_has_no_other_permissions(self):
        headers = self.pair()
        token = headers['Authorization'][7:]
        text = (config.app_dir() / 'reading-sync-auth.json').read_text(encoding='utf-8')
        self.assertNotIn(token, text)
        self.assertIn(hashlib.sha256(token.encode()).hexdigest(), text)
        client = self.app.test_client()
        for path in ['/api/library', '/api/settings', '/api/reading-stats', '/api/reading-sync']:
            self.assertEqual(client.get(path, headers=headers).status_code, 401)
        self.assertIn(client.post('/api/reading-sync', json={}, headers=headers).status_code, (401, 403))
        response = client.post('/api/reading-sync/import', json={'articles':[self.record()]}, headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(token, response.text)
        self.assertNotIn(token, self.client.get('/api/reading-sync').text)
        self.client.delete('/api/reading-sync', headers=self.headers)
        self.assertEqual(client.post('/api/reading-sync/import', json={'articles':[self.record()]}, headers=headers).status_code, 401)

    def test_import_validates_entire_batch_and_does_not_relax_csrf(self):
        headers = self.pair()
        client = self.app.test_client()
        for invalid in [dict(url='javascript:alert(1)'), dict(url='https://user:pass@example.invalid/a'),
                        dict(title='!!!'), dict(wordCount=-1), dict(wordCount=True), dict(readAt=10**20)]:
            bad = {**self.record(), **invalid}
            result = client.post('/api/reading-sync/import', json={'articles':[self.record(),bad]}, headers=headers)
            self.assertEqual(result.status_code, 400, result.json)
            self.assertEqual(self.stats()['articleCount'], 0)
        self.assertEqual(client.post('/api/reading-sync/import', json={'articles':[self.record()]},
                                    headers={**headers, 'Origin':'https://evil.invalid'}).status_code, 403)
        self.assertEqual(self.client.post('/api/reading-sync', json={}).status_code, 403)
        self.assertEqual(client.post('/api/reading-sync/import', json={'articles':[self.record()]},
                                    headers={**headers, 'Host':'evil.invalid'}).status_code, 400)
        self.assertEqual(client.post('/api/reading-sync/import', data=b'x'*(129*1024), headers=headers).status_code, 413)

    def test_multiple_devices_and_individual_revocation(self):
        first = self.post('/reading-sync', {'name':'办公室 Chrome'}).json
        second = self.post('/reading-sync', {'name':'家里 Chrome'}).json
        self.assertNotEqual(first['token'], second['token'])
        client = self.app.test_client()
        def upload(pair, records):
            return client.post('/api/reading-sync/import', json={'articles':records},
                headers={'Authorization':'Bearer '+pair['token']})
        status = self.client.get('/api/reading-sync').json
        self.assertEqual(len(status['devices']), 2)
        self.assertTrue(all(d['lastSyncAt'] is None for d in status['devices']))
        self.assertEqual(upload(first, [self.record()]).status_code, 200)
        self.assertEqual(upload(second, []).status_code, 200)
        status = self.client.get('/api/reading-sync').json
        self.assertTrue(all(d['lastSyncAt'] for d in status['devices']))
        self.assertEqual(status['devices'][0]['lastBatchCount'], 1)
        self.assertTrue(status['devices'][0]['lastUploadAt'])
        self.assertIsNone(status['devices'][1]['lastUploadAt'])
        # Empty connection checks preserve information about the last upload.
        self.assertEqual(upload(first, []).status_code, 200)
        self.assertEqual(self.client.get('/api/reading-sync').json['devices'][0]['lastBatchCount'], 1)
        path = '/api/reading-sync/'+first['device']['id']
        self.assertEqual(self.client.delete(path).status_code, 403)
        self.assertEqual(client.delete(path, headers={'Authorization':'Bearer '+second['token']}).status_code, 401)
        self.assertEqual(self.client.delete(path, headers=self.headers).status_code, 200)
        self.assertEqual(upload(first, []).status_code, 401)
        self.assertEqual(upload(second, [self.record()]).status_code, 200)
        self.assertEqual(self.stats()['articleCount'], 1)
        self.assertEqual(self.client.delete(path, headers=self.headers).status_code, 404)
        serialized = self.client.get('/api/reading-sync').text
        for pair in [first, second]:
            self.assertNotIn(pair['token'], serialized)
            self.assertNotIn(hashlib.sha256(pair['token'].encode()).hexdigest(), serialized)
        self.client.delete('/api/reading-sync', headers=self.headers)
        self.assertFalse(self.client.get('/api/reading-sync').json['enabled'])
        self.assertEqual(upload(second, []).status_code, 401)

    def test_legacy_code_migrates_without_disconnect_or_secret_disclosure(self):
        legacy_token = 'previously-issued-sync-token'
        path = config.app_dir() / 'reading-sync-auth.json'
        store._atomic_json_write(path, {'tokenHash':hashlib.sha256(legacy_token.encode()).hexdigest()})
        # Issuing a new code must also migrate correctly before a status GET.
        new = self.pair()
        client = self.app.test_client()
        old = {'Authorization':'Bearer '+legacy_token}
        for headers in [old, new]:
            self.assertEqual(client.post('/api/reading-sync/import', json={'articles':[]}, headers=headers).status_code, 200)
        status = self.client.get('/api/reading-sync').json
        self.assertEqual(len(status['devices']), 2)
        self.assertTrue(status['devices'][0]['legacy'])
        self.assertIsNone(status['devices'][0]['createdAt'])
        self.assertNotIn(legacy_token, path.read_text(encoding='utf-8'))
        self.assertNotIn('tokenHash', status['devices'][0])
        # A new registry instance reads the migrated codes after restart.
        registry = SyncDevices(config.app_dir(), threading.RLock())
        self.assertEqual(registry.authenticate(old['Authorization']), 'legacy')
        self.assertTrue(registry.authenticate(new['Authorization']))

    def test_rejected_sync_does_not_update_device_activity(self):
        headers = self.pair()
        client = self.app.test_client()
        for records in [[{**self.record(), 'wordCount':-1}], None]:
            self.assertEqual(client.post('/api/reading-sync/import', json={'articles':records}, headers=headers).status_code, 400)
        device = self.client.get('/api/reading-sync').json['devices'][0]
        self.assertIsNone(device['lastSyncAt'])
        self.assertIsNone(device['lastUploadAt'])

    def test_pair_validates_names_and_bounds_active_codes(self):
        for name in [None, 3, [], 'x'*81, 'name\nline']:
            self.assertEqual(self.post('/reading-sync', {'name':name}).status_code, 400)
        registry = SyncDevices(config.app_dir(), threading.RLock())
        for _ in range(50):
            registry.pair('test')
        self.assertEqual(self.post('/reading-sync', {}).status_code, 409)
        self.assertEqual(len(self.client.get('/api/reading-sync').json['devices']), 50)

    def test_concurrent_issuance_and_revoked_inflight_import(self):
        registry = SyncDevices(config.app_dir(), threading.RLock())
        with ThreadPoolExecutor(max_workers=8) as pool:
            pairs = list(pool.map(lambda i:registry.pair(f'device {i}'), range(12)))
        self.assertEqual(len(registry.status()['devices']), 12)
        self.assertEqual(len({p['device']['id'] for p in pairs}), 12)
        for pair in pairs:
            self.assertEqual(registry.authenticate('Bearer '+pair['token']), pair['device']['id'])
        # Simulate revocation between the request guard and import handler.
        device_id = registry.authenticate('Bearer '+pairs[0]['token'])
        registry.revoke(device_id)
        called = []
        self.assertIsNone(registry.import_records(device_id, lambda:called.append(True)))
        self.assertEqual(called, [])
        self.assertTrue(registry.authenticate('Bearer '+pairs[1]['token']))


if __name__ == '__main__':
    unittest.main(defaultTest='ReadingTests', verbosity=2)
