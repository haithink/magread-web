"""Podcast imports in an isolated library, with fake official HTML and model calls."""
from run_web_tests import WebTests, config, store, llm
from pathlib import Path
import json
import threading
import shutil
import unittest
from unittest.mock import patch
import zipfile
from io import BytesIO
from PIL import Image

from webapp import podcasts, podcast_covers, podcast_library


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
        cls.config_password = WebTests.config_password

    @classmethod
    def tearDownClass(cls):
        WebTests.tearDownClass()

    post, wait_job = WebTests.post, WebTests.wait_job

    def setUp(self):
        WebTests.setUp(self)
        self.cover_network = patch.object(podcast_covers, '_official_image', side_effect=ValueError('Offline fixture'))
        self.cover_network.start()
        self.addCleanup(self.cover_network.stop)
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
            cards = [b for b in self.client.get('/api/library').json['books'] if b['sourceType']=='podcast-series']
            self.assertEqual(len(cards), 1)
            series = cards[0]
            self.assertEqual((series['articleCount'],series['summaryCount']), (2,2))
            self.assertEqual(series['unreadCount'], 2)
            directory = self.client.get('/api/books/'+series['id']).json
            self.assertEqual([a['episodeNumber'] for a in directory['articles']], [55,54])
            cover = self.client.get('/api/books/'+series['id']+'/cover')
            self.assertEqual(cover.status_code, 200)
            cover.close()
            books = [b for b in store.load_library() if b.get('sourceType')=='podcast']
            for book in books:
                self.assertEqual(book['publication'], 'Lex Fridman Podcast')
                cover = self.client.get('/api/books/'+book['id']+'/cover')
                self.assertEqual(cover.status_code, 200)
                self.assertEqual(cover.mimetype, 'image/jpeg')
                with Image.open(BytesIO(cover.data)) as image:
                    self.assertEqual(image.size, (480,640))
                cover.close()
                self.assertNotIn('path', series)
                article = self.client.get('/api/books/'+book['id']).json
                self.assertIn('sourceUrl', article)
                self.assertEqual(len(article['articles']), 1)
                self.assertFalse(list(store._cache_dir(book['id']).glob('vocab*')))
                saved = self.post('/books/'+book['id']+'/articles/0/progress', dict(paragraphIndex=3, paragraphOffset=0))
                self.assertEqual(saved.status_code, 200)
            network.reset_mock(); model.reset_mock()
            first_cover = store.cover_file(books[0]['id'])
            original_cover_stat = first_cover.stat()
            repeated = self.import_fake()
            self.assertEqual(repeated['result']['imported'], 0)
            self.assertEqual(repeated['result']['existing'], 2)
            self.assertEqual(model.call_count, 0)
            self.assertEqual(network.call_count, 1)
            self.assertEqual(first_cover.stat().st_mtime_ns, original_cover_stat.st_mtime_ns)
            # Existing installations get missing covers through the ordinary cover endpoint.
            first_cover.unlink()
            self.assertTrue(next(b for b in self.client.get('/api/library').json['books'] if b['id']==series['id'])['cover'])
            regenerated = self.client.get('/api/books/'+books[0]['id']+'/cover')
            self.assertEqual(regenerated.status_code, 200)
            regenerated.close()
            self.assertTrue(first_cover.is_file())
            self.assertEqual(model.call_count, 0)
            self.assertEqual(network.call_count, 1)
            self.assertEqual(len(store.load_library()), 3)
            self.assertEqual(store.load_reading_history(books[0]['id'])['0']['paragraphIndex'], 3)
            marked = self.post('/books/'+books[0]['id']+'/state', dict(articleIndex=0,state='read'))
            self.assertEqual(marked.status_code, 200)
            stats = self.client.get('/api/reading-stats').json
            self.assertIn(books[0]['title'], json.dumps(stats, ensure_ascii=False))
            def unread_count():
                return next(b for b in self.client.get('/api/library').json['books'] if b['id']==series['id'])['unreadCount']
            self.assertEqual(unread_count(), 1)
            self.assertEqual(self.post('/books/'+books[1]['id']+'/state', dict(articleIndex=0,state='uninterested')).status_code, 200)
            self.assertEqual(unread_count(), 0)
            self.assertEqual(self.post('/books/'+books[0]['id']+'/state', dict(articleIndex=0,state='unread')).status_code, 200)
            self.assertEqual(unread_count(), 1)

    def test_series_reuses_learning_data_and_episode_identity_after_new_import(self):
        with patch.object(podcasts, 'fetch_html', side_effect=fake_fetch), patch.object(llm, 'conversation', side_effect=fake_model):
            result = self.import_fake(2)['result']
        bid = result['books'][1]
        sid = podcast_library.PREFIX+'lex-fridman'
        self.post('/books/'+bid+'/state', dict(articleIndex=0, state='read'))
        self.assertEqual(self.post('/books/'+bid+'/articles/0/tags', dict(tags=['AI 精选','AI 产业'])).status_code,200)
        self.post('/books/'+bid+'/articles/0/progress', dict(paragraphIndex=3, paragraphOffset=4))
        before = self.client.get('/api/books/'+sid).json
        old = next(a for a in before['articles'] if a['bookId'] == bid)
        # Adding a higher-numbered episode changes list order, never the link identity.
        def newer(spec, url, control):
            if url == spec['url']:
                return catalog_html(56)
            return fake_fetch(spec, url, control)
        with patch.object(podcasts, 'fetch_html', side_effect=newer), patch.object(llm, 'conversation', side_effect=fake_model):
            self.import_fake(3)
        after = self.client.get('/api/books/'+sid).json
        same = next(a for a in after['articles'] if a['bookId'] == bid)
        self.assertEqual(same['articleIndex'], old['articleIndex'])
        self.assertEqual(same['summaryZh'], old['summaryZh'])
        self.assertEqual(same['state'], 'read')
        self.assertEqual(same['tags'], ['AI 精选','AI 产业'])
        self.assertEqual(self.client.get('/api/books/'+bid).json['articles'][0]['tags'], same['tags'])
        self.assertEqual(same['history']['paragraphIndex'], 3)
        self.assertEqual(same['history']['paragraphOffset'], 4)
        read = self.client.get('/api/books/'+bid+'/articles/0').json
        self.assertEqual(read['parentBookId'], sid)
        recent = self.client.get('/api/library').json['recent']
        self.assertNotIn(bid, [r['bookId'] for r in recent])
        self.assertEqual(self.client.get('/api/library').json['articleStates'][bid]['0'], 'read')
        with patch.object(llm, 'conversation', side_effect=AssertionError('Must reuse summaries')):
            job = self.wait_job(self.post('/books/'+sid+'/generate-digest', {}))
            self.assertEqual(job['state'], 'done', job)

    def test_official_series_cover_cached_and_offline_fallback_upgraded(self):
        spec = podcasts.source('lex-fridman')
        path = store.cover_file(podcast_library.PREFIX+spec['id'])
        path.unlink(missing_ok=True)
        path.with_suffix('.source.json').unlink(missing_ok=True)
        podcast_covers.ensure_series_cover(spec)
        self.assertFalse(store._json_read(path.with_suffix('.source.json'))['official'])
        with patch.object(podcast_covers, '_official_image', side_effect=lambda *a:Image.new('RGB',(640,640),'navy')) as fetch:
            podcast_covers.ensure_series_cover(spec, download=True)
            self.assertTrue(store._json_read(path.with_suffix('.source.json'))['official'])
            podcast_covers.ensure_series_cover(spec, download=True)
            podcast_covers.ensure_series_cover(spec)
            self.assertEqual(fetch.call_count, 1)
        with Image.open(path) as cover:
            self.assertEqual(cover.size, (640,640))

    def test_official_cover_redirect_validation_and_image_decoding(self):
        spec = podcasts.source('lex-fridman')
        response = unittest.mock.MagicMock()
        response.__enter__.return_value = response
        response.status_code = 302
        response.headers = {'Location':'https://127.0.0.1/private.jpg'}
        # Stop only the fixture-level image stub to exercise the real downloader.
        self.cover_network.stop()
        with patch.object(podcast_covers.requests, 'get', return_value=response) as get:
            with self.assertRaises(ValueError):
                podcast_covers._official_image(spec)
            self.assertEqual(get.call_count, 1)
        response.status_code = 200
        response.iter_content.return_value = [b'<html>Not an image</html>']
        with patch.object(podcast_covers.requests, 'get', return_value=response), self.assertRaises(OSError):
            podcast_covers._official_image(spec)

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

    def test_series_digest_fills_missing_whole_episode_and_old_endpoint_reuses_cache(self):
        with patch.object(podcasts, 'fetch_html', side_effect=fake_fetch), \
                patch.object(llm, 'conversation', side_effect=llm.LlmError('Temporary model error')):
            result = self.import_fake(1)
        bid = result['result']['books'][0]
        with patch('kandu.core_gen.generate_digest', side_effect=AssertionError('Do not summarize intro only')), \
                patch.object(llm, 'conversation', side_effect=fake_model) as model:
            manual = self.wait_job(self.post('/books/podcast-series-lex-fridman/generate-digest', {}))
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
        def slow(*args, **kwargs):
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

    def test_selected_older_transcripts_only_and_repeat_preserves_state(self):
        selected = ['https://lexfridman.com/guest-2-transcript', 'https://lexfridman.com/guest-1-transcript']
        with patch.object(podcasts, 'fetch_html', side_effect=fake_fetch) as fetch, patch.object(llm, 'conversation', side_effect=fake_model) as model:
            result = self.wait_job(self.post('/podcast-sources/lex-fridman/import', dict(transcriptUrls=selected)))
            self.assertEqual(result['state'], 'done', result)
            self.assertEqual((result['result']['imported'],result['result']['total']), (2,2))
            entries = [b for b in store.load_library() if b.get('sourceType')=='podcast']
            self.assertEqual({b['sourceUrl'] for b in entries}, set(selected))
            self.assertEqual({b['episodeNumber'] for b in entries}, {1,2})
            self.assertEqual(model.call_count, 2)
            store.set_article_state(entries[0]['id'], 0, 'read')
            fetch.reset_mock(); model.reset_mock()
            again = self.wait_job(self.post('/podcast-sources/lex-fridman/import', dict(transcriptUrls=selected)))
            self.assertEqual(again['result']['imported'], 0)
            self.assertEqual(again['result']['existing'], 2)
            self.assertEqual(model.call_count, 0)
            self.assertEqual(fetch.call_count, 1)
            self.assertEqual(store.load_article_states(entries[0]['id'])['0'], 'read')

    def test_selected_urls_reject_foreign_duplicate_invalid_and_missing_catalog_entries(self):
        url = 'https://lexfridman.com/guest-2-transcript'
        for urls in [[], 'all', [url]*51, [url,url+'/'], [None], ['http://lexfridman.com/test-transcript'],
                     ['https://example.invalid/test-transcript'], [url+'?x=1'], ['https://lexfridman.com/podcast/']]:
            self.assertEqual(self.post('/podcast-sources/lex-fridman/import', dict(transcriptUrls=urls)).status_code, 400)
        before = store.load_library()
        with patch.object(podcasts, 'fetch_html', side_effect=fake_fetch) as fetch:
            result = self.wait_job(self.post('/podcast-sources/lex-fridman/import', dict(transcriptUrls=[url,'https://lexfridman.com/missing-transcript'])))
        self.assertEqual(result['state'], 'error')
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(store.load_library(), before)

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
