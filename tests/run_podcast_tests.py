"""Podcast imports in an isolated library, with fake official HTML and model calls."""
from run_web_tests import WebTests, config, store, llm
from pathlib import Path
import json
import threading
import shutil
import unittest
from unittest.mock import patch
import zipfile

from webapp import podcasts


def catalog_html(count=55):
    rows = ''.join(f'<div class="episode-item"><div class="episode-title">A conversation {i}</div>'
        f'<span class="ep-person">Guest {i}</span><a href="https://lexfridman.com/guest-{i}-transcript">Transcript</a></div>'
        for i in range(count, 0, -1))
    return '<div class="guest"><a href="https://lexfridman.com/old-selected-transcript">Transcript</a></div>' + \
        f'<div class="episodes-all">{rows}</div>'


def transcript_html(number=55):
    turns = ''.join(f'<div class="ts-segment"><span class="ts-name">{"Lex" if i%2 else "Guest"}</span>'
        f'<span class="ts-timestamp">(00:{i:02}:00)</span><span class="ts-text">'
        f'Part {i}: We discuss mathematics and the natural world in this conversation. ' +
        'Scientific questions require careful thought and evidence. '*6 + '</span></div>' for i in range(8))
    return f'<h1 class="entry-title">Transcript for A conversation | Lex Fridman Podcast #{number}</h1>' \
        '<time class="entry-date" datetime="2026-11-10T18:52:28-05:00"></time>' \
        '<div class="entry-content"><p>UNWANTED NAVIGATION</p><h2>Table of Contents</h2>' \
        '<a href="https://example.invalid/video.mp4">UNWANTED VIDEO</a><h2>Introduction</h2>' + turns + \
        '<h2>Ending</h2><div class="ts-segment"><span class="ts-name">Lex</span>' \
        '<span class="ts-text">These are the final observations and conclusions.</span></div></div>'


def fake_fetch(spec, url, control):
    control.check()
    if url == spec['url']:
        return catalog_html()
    return transcript_html(int(url.split('guest-')[1].split('-')[0]))


def fake_model(cfg, system, messages, on_text=None, control=None):
    value = json.dumps(dict(titleZh='关于科学的对话', summaryZh='嘉宾讨论科学、数学及其相互联系，并比较不同观点。'), ensure_ascii=False)
    if on_text:
        on_text(value)
    return value


class PodcastTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        WebTests.setUpClass()
        cls.app, cls.authcode = WebTests.app, WebTests.authcode

    @classmethod
    def tearDownClass(cls):
        WebTests.tearDownClass()

    post, wait_job = WebTests.post, WebTests.wait_job

    def setUp(self):
        WebTests.setUp(self)
        root = config.app_dir().resolve()
        assert root.parent.name.startswith('kandu-web-tests-')
        for folder in (root/'cache').glob('lex-fridman-*'):
            assert folder.resolve().is_relative_to(root)
            shutil.rmtree(folder)
        store.save_library([WebTests.meta])
        podcasts.state_file('lex-fridman').unlink(missing_ok=True)

    def import_fake(self, limit=2):
        return self.wait_job(self.post('/podcast-sources/lex-fridman/import', dict(limit=limit)))

    def test_latest_full_list_only_and_idempotent_ids(self):
        spec = podcasts.source('lex-fridman')
        episodes = podcasts.catalog(spec, catalog_html(), 50)
        self.assertEqual(len(episodes), 50)
        self.assertEqual(episodes[0]['title'], 'A conversation 55')
        self.assertEqual(episodes[-1]['title'], 'A conversation 6')
        self.assertEqual(episodes, podcasts.catalog(spec, catalog_html(), 50))
        duplicate = catalog_html(2).replace('<div class="episodes-all">',
            '<div class="episodes-all"><div class="episode-item"><div class="episode-title">A conversation 2</div>'
            '<a href="https://lexfridman.com/guest-2-transcript/">Transcript</a></div>')
        self.assertEqual(len(podcasts.catalog(spec, duplicate, 50)), 2)

    def test_only_dialogue_and_one_text_epub_article(self):
        spec = podcasts.source('lex-fridman')
        episode = podcasts.catalog(spec, catalog_html(), 1)[0]
        item = podcasts.transcript(episode, transcript_html())
        self.assertEqual(item['episodeNumber'], 55)
        self.assertEqual(item['publishedAt'], '2026-11-10')
        self.assertEqual(len(item['paragraphs']), 9)
        self.assertIn('Lex (00:01:00):', item['paragraphs'][1])
        path = config.app_dir()/'test-podcast.epub'
        podcasts.write_epub(path, item, spec)
        with zipfile.ZipFile(path) as zf:
            self.assertEqual(zf.namelist()[0], 'mimetype')
            self.assertFalse(any(n.endswith(('.jpg','.mp3','.mp4')) for n in zf.namelist()))
        cover, articles = store.epub_parse(str(path))
        self.assertIsNone(cover)
        self.assertEqual(len(articles), 1)
        self.assertEqual(articles[0]['paragraphs'], item['paragraphs'])
        self.assertNotIn('UNWANTED', json.dumps(articles))
        self.assertEqual([c['title'] for c in item['chapters']], ['Introduction','Ending'])

    def test_invalid_webpages_and_unapproved_urls_rejected(self):
        spec = podcasts.source('lex-fridman')
        for url in ['http://lexfridman.com/x-transcript', 'https://evil.invalid/x-transcript',
                'https://lexfridman.com@evil.invalid/x', 'https://lexfridman.com:443/x',
                'https://lexfridman.com/x?redirect=evil', 'https://127.0.0.1/x']:
            with self.subTest(url=url), self.assertRaises(ValueError):
                podcasts.checked_url(spec, url)
        with self.assertRaises(ValueError):
            podcasts.catalog(spec, '<p>Cloudflare challenge</p>', 50)
        with self.assertRaises(ValueError):
            podcasts.transcript({}, '<p>Not a transcript</p>')
        with self.assertRaises(ValueError):
            podcasts.transcript({}, transcript_html().replace('ts-text', 'wrong-text'))

    def test_redirect_is_checked_before_fetching(self):
        response = unittest.mock.MagicMock()
        response.__enter__.return_value = response
        response.status_code = 302
        response.headers = {'Location':'https://127.0.0.1/private'}
        with patch.object(podcasts.requests, 'get', return_value=response) as request:
            with self.assertRaises(ValueError):
                podcasts.fetch_html(podcasts.source('lex-fridman'), 'https://lexfridman.com/podcast/', llm.RequestControl())
        self.assertEqual(request.call_count, 1)

    def test_summary_excerpts_span_beginning_middle_end_and_are_bounded(self):
        text = ['BEGINNING'+'a'*12000, 'MIDDLE'+'b'*12000, 'ENDING'+'z'*12000]
        context = podcasts.summary_context(text, [{'title':'Topic','paragraph':1}])
        self.assertIn('excerpts', context)
        self.assertEqual(context['excerpts'][0]['text'][:9], 'BEGINNING')
        self.assertTrue(context['excerpts'][-1]['text'].endswith('z'*50))
        self.assertTrue(any('b'*100 in e['text'] for e in context['excerpts']))
        self.assertLessEqual(sum(len(e['text']) for e in context['excerpts']), 32000)
        self.assertEqual(podcasts.summary_context(['short'], [])['transcript'], 'short')

    def test_import_category_metadata_summary_and_no_vocab_then_rerun(self):
        with patch.object(podcasts, 'fetch_html', side_effect=fake_fetch) as network, \
                patch.object(llm, 'conversation', side_effect=fake_model) as model, \
                patch('kandu.core_gen.generate_vocab', side_effect=AssertionError('No auto vocabulary')):
            result = self.import_fake()
            self.assertEqual(result['state'], 'done', result)
            self.assertEqual(result['result']['imported'], 2)
            self.assertEqual(result['result']['summaries'], 2)
            self.assertEqual(result['result']['failed'], 0)
            self.assertEqual(model.call_count, 2)
            books = [b for b in self.client.get('/api/library').json['books'] if b['sourceType']=='podcast']
            self.assertEqual(len(books), 2)
            for book in books:
                self.assertEqual(book['publication'], 'Lex Fridman Podcast')
                self.assertEqual(book['articleCount'], 1)
                self.assertEqual(book['summaryCount'], 1)
                self.assertNotIn('path', book)
                article = self.client.get('/api/books/'+book['id']).json
                self.assertIn('sourceUrl', article)
                self.assertEqual(len(article['articles']), 1)
                self.assertFalse(list(store._cache_dir(book['id']).glob('vocab*')))
                saved = self.post('/books/'+book['id']+'/articles/0/progress', dict(paragraphIndex=3, paragraphOffset=0))
                self.assertEqual(saved.status_code, 200)
            network.reset_mock(); model.reset_mock()
            repeated = self.import_fake()
            self.assertEqual(repeated['result']['imported'], 0)
            self.assertEqual(repeated['result']['existing'], 2)
            self.assertEqual(model.call_count, 0)
            self.assertEqual(network.call_count, 1)
            self.assertEqual(len(store.load_library()), 3)
            self.assertEqual(store.load_reading_history(books[0]['id'])['0']['paragraphIndex'], 3)
            marked = self.post('/books/'+books[0]['id']+'/state', dict(articleIndex=0,state='read'))
            self.assertEqual(marked.status_code, 200)
            stats = self.client.get('/api/reading-stats').json
            self.assertIn(books[0]['title'], json.dumps(stats, ensure_ascii=False))

    def test_failed_summary_keeps_text_and_retry_only_fills_missing(self):
        with patch.object(podcasts, 'fetch_html', side_effect=fake_fetch), \
                patch.object(llm, 'conversation', side_effect=llm.LlmError('Temporary model error')):
            result = self.import_fake(1)
        self.assertEqual(result['result']['failed'], 1)
        bid = result['result']['books'][0]
        self.assertTrue(Path(store.get_book(bid)['path']).is_file())
        self.assertTrue(gen_incomplete(bid))
        with patch.object(podcasts, 'fetch_html', side_effect=fake_fetch) as network, \
                patch.object(llm, 'conversation', side_effect=fake_model) as model:
            result = self.import_fake(1)
        self.assertEqual(result['result']['failed'], 0)
        self.assertEqual(network.call_count, 1)
        self.assertEqual(model.call_count, 1)
        self.assertFalse(gen_incomplete(bid))

    def test_manual_podcast_digest_uses_whole_episode_path_and_cache(self):
        with patch.object(podcasts, 'fetch_html', side_effect=fake_fetch), \
                patch.object(llm, 'conversation', side_effect=llm.LlmError('Temporary model error')):
            result = self.import_fake(1)
        bid = result['result']['books'][0]
        with patch('kandu.core_gen.generate_digest', side_effect=AssertionError('Do not summarize intro only')), \
                patch.object(llm, 'conversation', side_effect=fake_model) as model:
            manual = self.wait_job(self.post('/books/'+bid+'/generate-digest', {}))
            self.assertEqual(manual['state'], 'done', manual)
            sent = json.loads(model.call_args.args[2][0]['content'])
            self.assertIn('final observations', sent['transcript'])
            self.assertEqual(store.load_digest(bid)['summaryMethod'], podcasts.SUMMARY_METHOD)
            self.wait_job(self.post('/books/'+bid+'/generate-digest', {}))
            self.assertEqual(model.call_count, 1)

    def test_transcript_failure_does_not_block_other_episodes(self):
        def fetch(spec, url, control):
            if 'guest-55-' in url:
                raise ValueError('bad response')
            return fake_fetch(spec, url, control)
        with patch.object(podcasts, 'fetch_html', side_effect=fetch), patch.object(llm, 'conversation', side_effect=fake_model):
            result = self.import_fake(2)['result']
        self.assertEqual((result['imported'],result['summaries'],result['failed']), (1,1,1))
        self.assertEqual(result['failures'][0]['stage'], 'transcript')

    def test_import_validation_auth_and_single_active_job(self):
        for limit in [0,51,True,'50',None]:
            self.assertEqual(self.post('/podcast-sources/lex-fridman/import', dict(limit=limit)).status_code, 400)
        self.assertEqual(self.post('/podcast-sources/unknown/import', {}).status_code, 400)
        self.assertEqual(self.app.test_client().get('/api/podcast-sources').status_code, 401)
        gate = threading.Event()
        def slow(*args):
            gate.wait(3)
            return dict(failed=0)
        try:
            with patch.object(podcasts, 'import_source', side_effect=slow):
                first = self.post('/podcast-sources/lex-fridman/import', {})
                second = self.post('/podcast-sources/lex-fridman/import', {})
                self.assertEqual(first.json['jobId'], second.json['jobId'])
                active = self.client.get('/api/podcast-sources').json['sources'][0]
                self.assertEqual(active['jobId'], first.json['jobId'])
                gate.set()
                self.wait_job(first)
        finally:
            gate.set()

    def test_cancellation_keeps_saved_transcript_for_resume(self):
        control = llm.RequestControl()
        def fetch(spec, url, cancellation):
            if 'guest-54-' in url:
                cancellation.cancel(); cancellation.check()
            return fake_fetch(spec, url, cancellation)
        with patch.object(podcasts, 'fetch_html', side_effect=fetch), self.assertRaises(llm.RequestCancelled):
            podcasts.import_source('lex-fridman', 2, {}, lambda **v:None, control)
        self.assertEqual(podcasts.status('lex-fridman')['state'], 'stopped')
        self.assertEqual(len(store.load_library()), 2)


def gen_incomplete(bid):
    from kandu import core_gen
    return core_gen.digest_incomplete(store.get_book(bid))


if __name__ == '__main__':
    unittest.main(defaultTest='PodcastTests')
