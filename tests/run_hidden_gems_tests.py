"""Isolated source, download, local worker and series API tests. No real audio or LLM."""
from run_podcast_tests import WebTests, PodcastTests, config, store, llm, podcasts, fake_model
import json
from pathlib import Path
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from webapp import hidden_gems, podcast_transcribe, podcast_library


def catalog_html():
    return '<div id="episodes-list">' + ''.join(
        f'<div><h4><a href="/podcasts/hidden-gems/2026-11-{i}-conversation">Conversation {i}</a></h4>'
        '<div class="prose"><p>Guests: A guest</p><p>Description is not a transcript.</p></div>'
        f'<div><audio src="https://www.podtrac.com/pts/redirect.mp3/traffic.megaphone.fm/TEST{i}.mp3"></audio></div></div>'
        for i in (22, 23)) + '</div>'


def fake_transcript(episode, control, update):
    update(status='本地转写 00:01:00 / 00:05:00 · 20%')
    return dict(episode, paragraphs=['(00:00:00) We discuss business and science. '*30]*3,
        chapters=[], transcriptType='machine', transcriptionModel='small.en', transcriptionSegments=[])


class HiddenGemsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        WebTests.setUpClass()
        cls.app, cls.authcode = WebTests.app, WebTests.authcode
        cls.config_password = WebTests.config_password

    @classmethod
    def tearDownClass(cls):
        WebTests.tearDownClass()

    def setUp(self):
        PodcastTests.setUp(self)
        import shutil
        root = config.app_dir().resolve()
        assert root.parent.name.startswith('kandu-web-tests-')
        for folder in (root/'cache').glob('hidden-gems-*'):
            assert folder.resolve().is_relative_to(root)
            shutil.rmtree(folder)

    post, wait_job = WebTests.post, WebTests.wait_job

    def test_catalog_uses_audio_and_episode_identity_not_description(self):
        spec = podcasts.source('hidden-gems')
        rows = podcasts.catalog(spec, catalog_html(), 9)
        self.assertEqual([r['publishedAt'] for r in rows], ['2026-11-23', '2026-11-22'])
        self.assertEqual(rows[0]['audioUrl'], 'https://traffic.megaphone.fm/TEST23.mp3')
        self.assertNotIn('paragraphs', rows[0])
        self.assertEqual(rows, podcasts.catalog(spec, catalog_html(), 9))
        self.assertEqual(podcasts.validate_selection(spec, [rows[0]['sourceUrl']]), [rows[0]['sourceUrl']])
        for html in ['<p>A description</p>', catalog_html().replace('traffic.megaphone.fm', 'localhost')]:
            with self.assertRaises(ValueError):
                podcasts.catalog(spec, html, 9)

    def test_source_import_one_shelf_entry_summary_no_vocab_retry_reuses(self):
        with patch.object(podcasts, 'fetch_html', return_value=catalog_html()), \
                patch.object(hidden_gems, 'transcript', side_effect=fake_transcript) as speech, \
                patch.object(llm, 'conversation', side_effect=fake_model) as model, \
                patch('kandu.core_gen.generate_vocab', side_effect=AssertionError('No auto vocabulary')):
            result = self.wait_job(self.post('/podcast-sources/hidden-gems/import', dict(limit=2)))
            self.assertEqual(result['state'], 'done', result)
            self.assertEqual(result['result']['failed'], 0)
            self.assertEqual(speech.call_count, 2)
            self.assertEqual(model.call_count, 2)
            cards = [b for b in self.client.get('/api/library').json['books'] if b.get('sourceId') == 'hidden-gems']
            self.assertEqual(len(cards), 1)
            self.assertEqual(cards[0]['articleCount'], 2)
            directory = self.client.get('/api/books/'+cards[0]['id']).json
            self.assertTrue(directory['machineTranscripts'])
            self.assertEqual([a['publishedAt'] for a in directory['articles']], ['2026-11-23', '2026-11-22'])
            bid = directory['articles'][0]['bookId']
            read = self.client.get('/api/books/'+bid+'/articles/0').json
            self.assertEqual(read['transcriptType'], 'machine')
            self.assertEqual(read['transcriptionModel'], 'small.en')
            self.assertEqual(read['parentBookId'], 'podcast-series-hidden-gems')
            self.assertTrue(read['article']['paragraphs'][0].startswith('(00:00:00)'))
            self.post('/books/'+bid+'/state', dict(articleIndex=0, state='read'))
            speech.reset_mock(); model.reset_mock()
            again = self.wait_job(self.post('/podcast-sources/hidden-gems/import', dict(limit=2)))
            self.assertEqual(again['result']['existing'], 2)
            self.assertEqual((speech.call_count, model.call_count), (0, 0))
            self.assertEqual(store.load_article_states(bid)['0'], 'read')

    def test_missing_dependency_is_clear_and_never_downloads(self):
        episode = podcasts.catalog(podcasts.source('hidden-gems'), catalog_html(), 1)[0]
        with patch.object(podcast_transcribe, 'available', return_value=False), \
                patch.object(hidden_gems, 'download') as download, \
                self.assertRaisesRegex(ValueError, 'requirements-transcription'):
            hidden_gems.transcript(episode, llm.RequestControl(), lambda **v:None)
        download.assert_not_called()

    def test_failed_transcription_keeps_other_episode_and_reports_reason(self):
        def speech(episode, *args):
            if episode['publishedAt'] == '2026-11-23':
                raise ValueError('Local transcription failed; retry available')
            return fake_transcript(episode, *args)
        with patch.object(podcasts, 'fetch_html', return_value=catalog_html()), \
                patch.object(hidden_gems, 'transcript', side_effect=speech), \
                patch.object(llm, 'conversation', side_effect=fake_model) as model:
            result = self.wait_job(self.post('/podcast-sources/hidden-gems/import', dict(limit=2)))['result']
        self.assertEqual((result['imported'], result['failed'], model.call_count), (1, 1, 1))
        self.assertIn('Local transcription failed', result['failures'][0]['message'])
        self.assertEqual(len(podcast_library.episodes('hidden-gems')), 1)

    def test_cached_machine_transcript_needs_no_audio_model_or_network(self):
        episode = podcasts.catalog(podcasts.source('hidden-gems'), catalog_html(), 1)[0]
        path = config.app_dir()/'podcasts'/'transcripts'/(episode['id']+'.json')
        try:
            store._atomic_json_write(path, dict(complete=True, model='small.en', paragraphs=['Cached text.'], segments=[]))
            with patch.object(hidden_gems, 'download', side_effect=AssertionError('No download')), \
                    patch.object(podcast_transcribe, 'transcribe', side_effect=AssertionError('No model')):
                item = hidden_gems.transcript(episode, llm.RequestControl(), lambda **v:None)
            self.assertEqual(item['paragraphs'], ['Cached text.'])
        finally:
            path.unlink(missing_ok=True)

    def test_download_checks_redirect_before_following_and_removes_partial(self):
        episode = dict(id='download-test', audioUrl='https://traffic.megaphone.fm/TEST.mp3')
        response = MagicMock()
        response.__enter__.return_value = response
        response.status_code = 302
        response.headers = {'Location':'https://127.0.0.1/private.mp3'}
        with patch.object(hidden_gems.requests, 'get', return_value=response) as get, self.assertRaises(ValueError):
            hidden_gems.download(episode, llm.RequestControl(), lambda **v:None)
        self.assertEqual(get.call_count, 1)
        response.status_code = 200
        response.headers = {'Content-Type':'audio/mpeg'}
        response.iter_content.return_value = [b'a'*10, b'b'*10]
        with patch.object(hidden_gems.requests, 'get', return_value=response), \
                patch.object(hidden_gems, 'MAX_AUDIO_BYTES', 15), self.assertRaises(ValueError):
            hidden_gems.download(episode, llm.RequestControl(), lambda **v:None)
        self.assertFalse(list((config.app_dir()/'podcasts'/'audio'/'hidden-gems').glob('download-test*')))
        for url in ['http://traffic.megaphone.fm/TEST.mp3', 'https://traffic.megaphone.fm.evil.invalid/TEST.mp3',
                    'https://traffic.megaphone.fm:443/TEST.mp3', 'https://user@traffic.megaphone.fm/TEST.mp3']:
            with self.assertRaises(ValueError):
                hidden_gems.audio_url(url)

    def test_paragraphs_preserve_recognized_text_and_timestamps(self):
        text = 'We discuss a business problem and consider the evidence. '
        segments = [dict(start=i*10, end=(i+1)*10, text=text) for i in range(20)]
        paragraphs = podcast_transcribe.paragraphs(segments)
        self.assertGreater(len(paragraphs), 1)
        import re
        plain = ' '.join(re.sub(r'^\(\d{2}:\d{2}:\d{2}\) ', '', p) for p in paragraphs)
        self.assertEqual(plain, ' '.join(s['text'].strip() for s in segments))
        self.assertTrue(paragraphs[0].startswith('(00:00:00) '))

    def test_cancel_terminates_worker_during_model_loading_and_releases_lock(self):
        real_popen = subprocess.Popen
        processes = []
        pid_file = config.app_dir()/'cancel-worker-pid.txt'
        pid_file.unlink(missing_ok=True)
        def spawn(arguments, log):
            p = real_popen([sys.executable, '-c',
                'import os,sys,time;open(sys.argv[1],"w").write(str(os.getpid()));time.sleep(60)', str(pid_file)], stdout=log, stderr=log,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0)
            processes.append(p)
            return p
        control = llm.RequestControl()
        timer = threading.Timer(.5, control.cancel)
        timer.start()
        try:
            with patch.object(podcast_transcribe, 'available', return_value=True), \
                    patch.object(podcast_transcribe, 'start_worker', side_effect=spawn), \
                    self.assertRaises(llm.RequestCancelled):
                podcast_transcribe.transcribe(Path('fixture.mp3'), 'cancel-fixture', control, lambda **v:None)
            self.assertIsNotNone(processes[0].poll())
            if sys.platform == 'win32':
                import ctypes
                child_pid = int(pid_file.read_text())
                api = ctypes.windll.kernel32
                api.OpenProcess.restype = ctypes.c_void_p
                handle = api.OpenProcess(0x100000, False, child_pid)
                if handle:
                    try:
                        api.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
                        self.assertEqual(api.WaitForSingleObject(handle, 1000), 0, 'Recognizer child remains alive')
                    finally:
                        api.CloseHandle.argtypes = [ctypes.c_void_p]
                        api.CloseHandle(handle)
            self.assertTrue(podcast_transcribe.WORKER_LOCK.acquire(blocking=False))
            podcast_transcribe.WORKER_LOCK.release()
        finally:
            timer.cancel()
            for p in processes:
                if p.poll() is None:
                    p.kill(); p.wait()


if __name__ == '__main__':
    unittest.main(defaultTest='HiddenGemsTests')
