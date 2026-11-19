"""Authenticated, bounded diagnostics with no user content or credentials."""
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest

from run_web_tests import WebTests
from webapp.diagnostics import Diagnostics


class DiagnosticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        WebTests.setUpClass()

    @classmethod
    def tearDownClass(cls):
        WebTests.tearDownClass()

    def setUp(self):
        self.client = WebTests.app.test_client()
        response = self.client.post('/api/login', json={'password': WebTests.authcode})
        self.headers = {'X-CSRF-Token': response.json['csrf'], 'Origin': 'http://localhost'}
        self.diagnostics = WebTests.app.extensions['diagnostics']
        self.diagnostics.requests.clear()

    def post(self, value, **kwargs):
        return self.client.post('/api/diagnostics', json=value, headers=self.headers, **kwargs)

    def test_auth_csrf_sizes_and_rate(self):
        body = dict(events=[dict(event='analysis.start')])
        self.assertEqual(WebTests.app.test_client().post('/api/diagnostics', json=body).status_code, 401)
        self.assertEqual(self.client.post('/api/diagnostics', json=body).status_code, 403)
        self.assertEqual(self.post(body).status_code, 200)
        self.assertEqual(self.post(dict(events=body['events'] * 13)).status_code, 400)
        self.assertEqual(self.post(dict(events=[dict(event='not.allowed')])).status_code, 400)
        self.assertEqual(self.post(dict(events='wrong')).status_code, 400)
        self.assertEqual(self.post(dict(events=[dict(event='analysis.start', text='x' * 13000)])).status_code, 413)
        with self.diagnostics.lock:
            while len(self.diagnostics.requests) < 120:
                self.diagnostics.reserve()
        self.assertEqual(self.post(body).status_code, 429)

    def test_log_whitelist_and_rotation(self):
        forbidden = 'PRIVATE_SENTENCE_AND_TOKEN'
        response = self.post(dict(events=[dict(event='page.error', page='0123456789abcdef', trace='abcdef012345',
            phase='cache-read', errorName='TypeError', source='app.js', line=99, column=12,
            dialogOpen=False, dialogModal=False, waitPresent=True, elapsedMs=20,
            highlightRanges=9372, modalHighlightsSuspended=True,
            text=forbidden, message=forbidden, stack=forbidden, apiKey=forbidden,
            build=forbidden, job=forbidden + '\n', control=forbidden, engine=forbidden)]))
        self.assertEqual(response.status_code, 200)
        row = json.loads(self.diagnostics.path.read_text().splitlines()[-1])
        self.assertEqual(row['line'], 99)
        self.assertEqual(row['phase'], 'cache-read')
        self.assertEqual(row['highlightRanges'], 9372)
        self.assertTrue(row['modalHighlightsSuspended'])
        self.assertNotIn(forbidden, json.dumps(row))
        self.assertFalse(any(key in row for key in ('text', 'message', 'stack', 'apiKey', 'build', 'job', 'control', 'engine')))
        response = self.post(dict(events=[dict(event='page.reading', highlightParagraphs=7, highlightRanges=84,
            gapMs=15000, text=forbidden)]))
        self.assertEqual(response.status_code, 200)
        row = json.loads(self.diagnostics.path.read_text().splitlines()[-1])
        self.assertEqual(row['highlightParagraphs'], 7)
        self.assertNotIn(forbidden, json.dumps(row))
        with tempfile.TemporaryDirectory(prefix='kandu-diagnostics-') as directory:
            log = Diagnostics(directory, max_bytes=100, backups=2)
            for n in range(20):
                log.write(dict(event='job.done', sequence=n))
            self.assertLessEqual(len(list((Path(directory) / 'diagnostics').iterdir())), 3)
            self.assertEqual(json.loads(log.path.read_text().splitlines()[-1])['sequence'], 19)

    def test_server_job_events_do_not_log_input_or_result(self):
        jobs = WebTests.app.extensions['jobs']
        done = threading.Event()
        def action(update, control):
            done.wait(3)
            return {'secret_result': 'PRIVATE_MODEL_OUTPUT'}
        job = jobs.submit(('analysis', 'PRIVATE_ARTICLE', 0, 'PRIVATE_SENTENCE'), action)
        done.set()
        for _ in range(100):
            rows = [json.loads(line) for line in self.diagnostics.path.read_text().splitlines()]
            events = [row for row in rows if row.get('job') == job]
            if any(row['event'] == 'job.done' for row in events):
                break
            time.sleep(.02)
        self.assertEqual([row['event'] for row in events], ['job.submitted', 'job.started', 'job.done'])
        self.assertNotIn('PRIVATE_', json.dumps(events))


if __name__ == '__main__':
    unittest.main(defaultTest='DiagnosticTests', verbosity=2)
