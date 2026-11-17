"""Isolated settings authorization and upload limits. No personal data or models."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from io import BytesIO
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from werkzeug.security import generate_password_hash
from kandu import core_config as config, core_parsers as store, core_llm as llm
from webapp.server import create_app
from webapp.upload_security import UploadQuota, UploadQuotaExceeded, FILE_BYTES, DAILY_BYTES, REQUEST_BYTES


class AccessUploadTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='kandu-access-upload-')
        self.root = Path(self.temporary.name)
        self.env = patch.dict(os.environ, {'KANDU_DATA_DIR': str(self.root)})
        self.env.start()
        self.password = 'isolated-extra-password'
        self.credentials = self.root / 'config-auth.json'
        self.set_password(self.password)
        config.save_settings(dict(config.DEFAULTS, llms=[dict(name='private-name', model='private-model', provider='openai',
            baseUrl='https://model.invalid', apiKey='ISOLATED_KEY_VALUE')], defaultLlm='private-name'))
        self.app = create_app()
        self.app.testing = True
        self.code = (self.root / 'access-code.txt').read_text().strip()
        self.client, self.headers = self.login()
        self.network = patch.object(llm, 'chat', side_effect=AssertionError('No model calls'))
        self.network.start()

    def tearDown(self):
        self.app.extensions['jobs'].pool.shutdown(wait=True)
        self.network.stop()
        self.env.stop()
        self.temporary.cleanup()

    def set_password(self, password):
        store._atomic_json_write(self.credentials, dict(passwordHash=generate_password_hash(password, method='pbkdf2:sha256:1000')))

    def login(self):
        client = self.app.test_client()
        response = client.post('/api/login', json={'password': self.code})
        self.assertEqual(response.status_code, 200)
        return client, {'X-CSRF-Token': response.json['csrf']}

    def unlock(self, password=None):
        return self.client.post('/api/settings/unlock', json={'password': password or self.password}, headers=self.headers)

    def post_settings(self, data):
        return self.client.post('/api/settings', json=data, headers=self.headers)

    def upload(self, data, name='invalid.exe', declared=None, client=None, headers=None):
        request_headers = dict(headers or self.headers)
        if declared is not None:
            request_headers['X-Upload-Size'] = str(declared)
        return (client or self.client).post('/api/books', data={'file': (BytesIO(data), name)}, headers=request_headers)

    def used(self):
        return self.client.get('/api/upload-quota').json['usedBytes']

    def test_all_model_settings_operations_require_extra_password(self):
        original = config.load_settings()
        self.assertEqual(self.client.get('/api/settings').status_code, 403)
        for data in [dict(modelConfig=dict(baseUrl='https://attacker.invalid'), originalName='private-name'),
                     dict(defaultModel='private-name'), dict(configJson='{}'), dict(vocabLevel='gre')]:
            self.assertEqual(self.post_settings(data).status_code, 403)
        self.assertEqual(config.load_settings(), original)
        self.assertNotIn('private-name', self.client.get('/api/settings').text)
        self.assertEqual(self.client.get('/api/library').status_code, 200)

    def test_preferences_cannot_smuggle_model_changes(self):
        original = config.load_settings()
        response = self.client.get('/api/preferences')
        self.assertEqual(response.json, {'vocabLevel': 'cet4'})
        self.assertEqual(self.client.post('/api/preferences', json=dict(vocabLevel='gre', defaultModel='bad'), headers=self.headers).status_code, 400)
        self.assertEqual(config.load_settings(), original)
        self.assertEqual(self.client.post('/api/preferences', json=dict(vocabLevel='gre'), headers=self.headers).status_code, 200)
        self.assertEqual(config.load_settings()['llms'], original['llms'])

    def test_unlock_exposes_details_but_never_key_and_has_fixed_lifetime(self):
        now = int(time.time())
        with patch('webapp.config_security.time.time', return_value=now):
            response = self.unlock()
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json['expiresAt'], (now+900)*1000)
            shown = self.client.get('/api/settings')
            self.assertEqual(shown.status_code, 200)
            self.assertEqual(shown.json['models'][0]['baseUrl'], 'https://model.invalid')
            self.assertNotIn('ISOLATED_KEY_VALUE', shown.text)
            self.assertEqual(self.post_settings(dict(defaultModel='private-name')).status_code, 200)
        with patch('webapp.config_security.time.time', return_value=now+901):
            self.assertEqual(self.client.get('/api/settings').status_code, 403)
            self.assertEqual(self.post_settings(dict(defaultModel='private-name')).status_code, 403)
            self.assertFalse(self.client.get('/api/settings/access').json['unlocked'])

    def test_grant_bound_to_session_and_not_shared_with_other_browser(self):
        self.assertEqual(self.unlock().status_code, 200)
        other, _ = self.login()
        self.assertEqual(other.get('/api/settings').status_code, 403)
        with self.client.session_transaction() as current:
            token = current['configGrant']
        with other.session_transaction() as target:
            target['configGrant'] = token
        self.assertEqual(other.get('/api/settings').status_code, 403)

    def test_lock_and_logout_revoke_replayed_grant_cookie(self):
        for path in ['/api/settings/lock', '/api/logout']:
            self.client, self.headers = self.login()
            self.assertEqual(self.unlock().status_code, 200)
            cookie = self.client.get_cookie('kandu_web').value
            replay = self.app.test_client()
            replay.set_cookie('kandu_web', cookie)
            self.assertEqual(replay.get('/api/settings').status_code, 200)
            self.assertEqual(self.client.post(path, json={}, headers=self.headers).status_code, 200)
            self.assertEqual(replay.get('/api/settings').status_code, 403)

    def test_changing_extra_password_revokes_previous_grants_without_restart(self):
        self.assertEqual(self.unlock().status_code, 200)
        self.set_password('different-isolated-password')
        self.assertEqual(self.client.get('/api/settings').status_code, 403)
        self.assertEqual(self.unlock().status_code, 403)
        self.assertEqual(self.unlock('different-isolated-password').status_code, 200)

    def test_missing_configuration_is_locked_with_no_remote_setup(self):
        self.credentials.unlink()
        self.assertFalse(self.client.get('/api/settings/access').json['configured'])
        self.assertEqual(self.unlock().status_code, 503)
        self.assertEqual(self.client.get('/api/settings').status_code, 403)
        self.assertEqual(self.post_settings(dict(configJson='{}')).status_code, 403)
        self.assertFalse(self.credentials.exists())

    def test_unlock_requires_login_csrf_and_origin_and_small_body(self):
        self.assertEqual(self.app.test_client().post('/api/settings/unlock', json={'password':self.password}).status_code, 401)
        self.assertEqual(self.client.post('/api/settings/unlock', json={'password':self.password}).status_code, 403)
        self.assertEqual(self.client.post('/api/settings/unlock', json={'password':self.password}, headers=dict(self.headers, Origin='https://attacker.invalid')).status_code, 403)
        self.assertEqual(self.unlock('x'*9000).status_code, 413)
        self.assertEqual(self.unlock().status_code, 200)

    def test_wrong_extra_password_has_independent_persistent_limit(self):
        for _ in range(5):
            self.assertEqual(self.unlock('wrong-extra').status_code, 403)
        response = self.unlock()
        self.assertEqual(response.status_code, 429)
        self.assertIn('Retry-After', response.headers)
        app = create_app()
        try:
            client = app.test_client()
            login = client.post('/api/login', json={'password':self.code})
            self.assertEqual(login.status_code, 200)
            self.assertEqual(client.post('/api/settings/unlock', json={'password':self.password}, headers={'X-CSRF-Token':login.json['csrf']}).status_code, 429)
            self.assertEqual(client.get('/api/library').status_code, 200)
        finally:
            app.extensions['jobs'].pool.shutdown(wait=True)

    def test_database_failure_denies_extra_access(self):
        with patch('webapp.config_security.ConfigAccess.issue', side_effect=sqlite3.OperationalError('test storage error')):
            self.assertEqual(self.unlock().status_code, 503)
        self.assertEqual(self.client.get('/api/settings').status_code, 403)
        self.assertEqual(self.unlock().status_code, 200)

    def test_production_limits_and_upload_status(self):
        quota = self.client.get('/api/upload-quota').json
        self.assertEqual(quota['fileBytes'], FILE_BYTES)
        self.assertEqual(quota['dailyBytes'], DAILY_BYTES)
        self.assertEqual(FILE_BYTES, 50*1024**2)
        self.assertEqual(DAILY_BYTES, 500*1024**2)
        self.assertEqual(self.app.config['MAX_CONTENT_LENGTH'], REQUEST_BYTES)
        self.assertEqual(quota['remainingBytes'], DAILY_BYTES)
        self.assertIn('resetsAt', quota)

    def test_oversized_declared_file_or_body_rejected_before_quota_and_parsing(self):
        with patch.object(store, 'epub_parse', side_effect=AssertionError('Must not parse')):
            self.assertEqual(self.upload(b'x', 'book.epub', declared=FILE_BYTES+1).status_code, 413)
            response = self.client.post('/api/books', headers=self.headers,
                environ_overrides={'CONTENT_LENGTH':str(REQUEST_BYTES+1), 'CONTENT_TYPE':'multipart/form-data; boundary=demo'})
            self.assertEqual(response.status_code, 413)
        self.assertEqual(self.used(), 0)

    def test_exact_file_and_daily_boundaries_and_failed_uploads_count(self):
        self.app.config.update(UPLOAD_FILE_BYTES=4096, UPLOAD_DAILY_BYTES=8192)
        self.assertEqual(self.upload(b'x'*4096, declared=4096).status_code, 400)
        self.assertEqual(self.used(), 4096)
        self.assertEqual(self.upload(b'x'*4096, declared=4096).status_code, 400)
        self.assertEqual(self.used(), 8192)
        self.assertEqual(self.upload(b'x', declared=1).status_code, 429)
        self.assertEqual(self.client.get('/api/upload-quota').json['remainingBytes'], 0)

    def test_understated_size_cannot_bypass_stream_limit(self):
        self.app.config.update(UPLOAD_FILE_BYTES=4096, UPLOAD_DAILY_BYTES=8192)
        self.assertEqual(self.upload(b'x'*4096, declared=10).status_code, 413)
        self.assertEqual(self.used(), 4096)
        self.assertEqual(self.upload(b'x'*70000, declared=1).status_code, 413)
        self.assertEqual(self.used(), 4096)

    def test_forged_size_overflow_charged_up_to_remaining_daily_budget(self):
        self.app.config.update(UPLOAD_FILE_BYTES=4096, UPLOAD_DAILY_BYTES=3000)
        self.assertEqual(self.upload(b'x'*4096, declared=1).status_code, 429)
        self.assertEqual(self.used(), 3000)
        self.assertEqual(self.upload(b'x', declared=1).status_code, 429)

    def test_legacy_client_stream_limit_and_unused_reservation_released(self):
        self.app.config.update(UPLOAD_FILE_BYTES=4096)
        self.assertEqual(self.upload(b'x'*4097).status_code, 413)
        self.assertEqual(self.used(), 4096)
        self.assertEqual(self.upload(b'x'*50).status_code, 400)
        self.assertEqual(self.used(), 4146)

    def test_quota_is_global_between_sessions_and_survives_restart(self):
        self.app.config.update(UPLOAD_FILE_BYTES=100, UPLOAD_DAILY_BYTES=150)
        self.assertEqual(self.upload(b'x'*100, declared=100).status_code, 400)
        other, headers = self.login()
        self.assertEqual(self.upload(b'x'*51, declared=51, client=other, headers=headers).status_code, 429)
        app = create_app()
        app.config.update(UPLOAD_FILE_BYTES=100, UPLOAD_DAILY_BYTES=150)
        try:
            client = app.test_client()
            login = client.post('/api/login', json={'password':self.code})
            response = client.post('/api/books', data={'file':(BytesIO(b'x'*51),'other.exe')},
                headers={'X-CSRF-Token':login.json['csrf'], 'X-Upload-Size':'51'})
            self.assertEqual(response.status_code, 429)
        finally:
            app.extensions['jobs'].pool.shutdown(wait=True)

    def test_unauthenticated_and_bad_csrf_uploads_do_not_touch_quota(self):
        self.assertEqual(self.upload(b'x', client=self.app.test_client()).status_code, 401)
        self.assertEqual(self.upload(b'x', headers={'X-CSRF-Token':'bad'}).status_code, 403)
        self.assertEqual(self.used(), 0)

    def test_multiple_file_parts_and_forged_metadata_rejected(self):
        self.assertEqual(self.client.post('/api/books', data={'file':[(BytesIO(b'abc'),'a.exe'),(BytesIO(b'xyz'),'b.exe')]}, headers=self.headers).status_code, 413)
        self.assertLessEqual(self.used(), 3)
        self.assertEqual(self.upload(b'abc', declared='not-an-integer').status_code, 400)
        self.assertEqual(self.upload(b'abc', declared=20).status_code, 400)

    def test_upload_accounting_failure_keeps_reservation_and_storage_failure_denies_new_uploads(self):
        with patch.object(UploadQuota, 'finish', side_effect=sqlite3.OperationalError('test unavailable')):
            self.assertEqual(self.upload(b'abc', declared=3).status_code, 400)
        self.assertEqual(self.used(), 3)
        with patch.object(UploadQuota, 'reserve', side_effect=sqlite3.OperationalError('test unavailable')):
            self.assertEqual(self.upload(b'abc', declared=3).status_code, 503)

    def test_valid_epub_still_imports_with_exact_file_accounting(self):
        stream = BytesIO()
        with zipfile.ZipFile(stream, 'w') as archive:
            archive.writestr('META-INF/container.xml','<container><rootfiles><rootfile full-path="content.opf"/></rootfiles></container>')
            archive.writestr('content.opf','<package><manifest><item id="text" href="text.xhtml"/></manifest><spine><itemref idref="text"/></spine></package>')
            archive.writestr('text.xhtml','<html><body><h1>Isolated upload</h1><p>Enough original English words to test an uploaded publication without contacting any external model.</p></body></html>')
        data = stream.getvalue()
        response = self.upload(data, 'fixture.epub', declared=len(data))
        self.assertEqual(response.status_code, 202, response.json)
        for _ in range(100):
            job = self.client.get('/api/jobs/'+response.json['jobId']).json
            if job['state'] in ('done','error'):
                break
            time.sleep(.02)
        self.assertEqual(job['state'], 'done', job)
        self.assertEqual(self.used(), len(data))

    def test_accounting_reservations_atomic_under_concurrency(self):
        quota = UploadQuota(self.root / 'unit-quota.sqlite3')
        def reserve(_):
            try:
                return quota.reserve(50, limit=500)
            except UploadQuotaExceeded:
                return None
        with ThreadPoolExecutor(8) as pool:
            tickets = [t for t in pool.map(reserve, range(25)) if t]
        self.assertEqual(len(tickets), 10)
        self.assertEqual(quota.status(500)['usedBytes'], 500)
        quota.finish(tickets[0], 20)
        quota.finish(tickets[0], 20)
        self.assertEqual(quota.status(500)['usedBytes'], 470)
        self.assertIsNotNone(quota.reserve(30, limit=500))

    def test_daily_rollover_and_crashed_request_reservations(self):
        quota = UploadQuota(self.root / 'unit-quota.sqlite3')
        with patch.object(quota, 'today', return_value='2026-11-17'):
            ticket = quota.reserve(50, limit=50)
            self.assertEqual(quota.status(50)['remainingBytes'], 0)
            second = UploadQuota(quota.path)
            with patch.object(second, 'today', return_value='2026-11-17'):
                with self.assertRaises(UploadQuotaExceeded):
                    second.reserve(1, limit=50)
        with patch.object(quota, 'today', return_value='2026-11-18'):
            self.assertEqual(quota.status(50)['remainingBytes'], 50)
            quota.reserve(50, limit=50)
            quota.finish(ticket, 30)
            self.assertEqual(quota.status(50)['remainingBytes'], 0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
