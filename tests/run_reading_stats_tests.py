"""Isolated completed-reading and restricted plugin-sync contract tests."""
import hashlib
import time
import unittest
from unittest.mock import patch

from run_web_tests import WebTests, config, store
from webapp import reading_stats


class ReadingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        WebTests.setUpClass()
        cls.app, cls.authcode = WebTests.app, WebTests.authcode

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

    def test_completion_persists_after_backscroll_and_explicit_unread_removes(self):
        self.assertEqual(self.stats()['articleCount'], 0)
        self.post('/books/sample/articles/0/progress', dict(paragraphIndex=0, paragraphOffset=4))
        self.assertEqual(self.stats()['articleCount'], 0)
        self.post('/books/sample/articles/0/progress', dict(paragraphIndex=1, paragraphOffset=999))
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
        self.assertEqual(self.stats()['articleCount'], 1)
        self.post('/books/sample/state', dict(articleIndex=0, state='uninterested'))
        self.assertEqual(self.stats()['articleCount'], 0)

    def test_sync_token_is_hashed_revocable_and_has_no_other_permissions(self):
        headers = self.pair()
        token = headers['Authorization'][7:]
        text = (config.app_dir() / 'reading-sync-auth.json').read_text()
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


if __name__ == '__main__':
    unittest.main(defaultTest='ReadingTests', verbosity=2)
