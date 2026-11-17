"""Isolated login abuse tests; no personal data, networks, or model calls."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from werkzeug.security import generate_password_hash
from webapp.login_security import ClientAddressError, LoginLimiter, login_address
from webapp.server import create_app


class LoginSecurityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='kandu-login-tests-')
        self.root = Path(self.temporary.name)
        self.environment = patch.dict(os.environ, {'KANDU_DATA_DIR': str(self.root)})
        self.environment.start()
        self.root.joinpath('web-auth.json').write_text(json.dumps(dict(
            secret='isolated-test-secret', passwordHash=generate_password_hash('fixture-password', method='pbkdf2:sha256:1000'))), encoding='utf-8')
        self.app = create_app(cloudflare_hosts=['READ.EXAMPLE.COM'])
        self.app.testing = True
        self.client = self.app.test_client()
        self.limiter = LoginLimiter(self.root / 'unit-rate.sqlite3', 'fixture-secret')

    def tearDown(self):
        self.app.extensions['jobs'].pool.shutdown(wait=True)
        self.environment.stop()
        self.temporary.cleanup()

    def login(self, ip='203.0.113.7', password='incorrect', **kwargs):
        return self.client.post('/api/login', base_url='https://read.example.com',
                                headers={'CF-Connecting-IP': ip}, json={'password': password}, **kwargs)

    def test_tunnel_visitors_have_independent_budgets(self):
        for _ in range(10):
            self.assertEqual(self.login().status_code, 401)
        with patch('webapp.server.check_password_hash', side_effect=AssertionError('No hash after rate limit')):
            limited = self.login(password='fixture-password')
        self.assertEqual(limited.status_code, 429)
        self.assertEqual(int(limited.headers['Retry-After']), limited.json['retryAfter'])
        self.assertTrue(1 <= limited.json['retryAfter'] <= 600)
        other = self.login(ip='203.0.113.8', password='fixture-password')
        self.assertEqual(other.status_code, 200)
        self.assertIn('Secure;', other.headers['Set-Cookie'])
        self.assertEqual(self.client.get('/api/library', base_url='https://read.example.com').status_code, 200)

    def test_loopback_and_lan_headers_cannot_select_arbitrary_buckets(self):
        for peer, url in [('127.0.0.1', 'http://localhost'), ('192.168.1.22', 'http://localhost')]:
            for index in range(10):
                response = self.client.post('/api/login', base_url=url, json={'password': 'incorrect'},
                    headers={'CF-Connecting-IP': f'203.0.113.{index}', 'X-Forwarded-For': f'198.51.100.{index}'},
                    environ_overrides={'REMOTE_ADDR': peer})
                self.assertEqual(response.status_code, 401)
            self.assertEqual(self.client.post('/api/login', base_url=url, json={'password': 'fixture-password'},
                headers={'CF-Connecting-IP': '203.0.113.99'}, environ_overrides={'REMOTE_ADDR': peer}).status_code, 429)

    def test_tunnel_header_rejected_from_lan_even_with_public_host(self):
        response = self.login(environ_overrides={'REMOTE_ADDR': '192.168.1.22'})
        self.assertEqual(response.status_code, 400)

    def test_missing_or_malformed_tunnel_header_fails_closed(self):
        for header in ['', 'unknown', '203.0.113.1, 203.0.113.2', '203.0.113.1:1234']:
            with self.subTest(header=header):
                self.assertEqual(self.login(ip=header).status_code, 400)
        response = self.client.post('/api/login', base_url='https://read.example.com',
            json={'password': 'fixture-password'}, headers={'X-Forwarded-For': '203.0.113.2'})
        self.assertEqual(response.status_code, 400)

    def test_plain_https_host_does_not_enable_proxy_trust(self):
        app = create_app(https_hosts=['other.example.com'])
        try:
            client = app.test_client()
            for index in range(10):
                self.assertEqual(client.post('/api/login', base_url='https://other.example.com',
                    headers={'CF-Connecting-IP': f'203.0.113.{index}'}, json={'password': 'incorrect'}).status_code, 401)
            self.assertEqual(client.post('/api/login', base_url='https://other.example.com',
                json={'password': 'fixture-password'}).status_code, 429)
        finally:
            app.extensions['jobs'].pool.shutdown(wait=True)

    def test_normalized_ipv4_and_ipv6_networks(self):
        hosts = {'read.example.com'}
        for ip in ['203.0.113.8', '::ffff:203.0.113.8']:
            self.assertEqual(login_address('::ffff:127.0.0.1', 'read.example.com', hosts, ip), '203.0.113.8')
        first = login_address('::1', 'read.example.com', hosts, '2001:db8:ab::1')
        second = login_address('::1', 'read.example.com', hosts, '2001:db8:ab:0:ffff::2')
        self.assertEqual(first, second)
        self.assertNotEqual(first, login_address('::1', 'read.example.com', hosts, '2001:db8:ab:1::1'))
        with self.assertRaises(ClientAddressError):
            login_address('', 'localhost', set(), '')

    def test_rotating_ipv6_addresses_within_subnet_still_limited(self):
        for index in range(10):
            self.assertEqual(self.login(ip=f'2001:db8:abcd::{index}').status_code, 401)
        self.assertEqual(self.login(ip='2001:db8:abcd::ff').status_code, 429)
        self.assertEqual(self.login(ip='2001:db8:abce::ff', password='fixture-password').status_code, 200)

    def test_limits_survive_app_restart(self):
        for _ in range(10):
            self.assertEqual(self.login().status_code, 401)
        app = create_app(cloudflare_hosts=['read.example.com'])
        try:
            response = app.test_client().post('/api/login', base_url='https://read.example.com',
                headers={'CF-Connecting-IP': '203.0.113.7'}, json={'password': 'fixture-password'})
            self.assertEqual(response.status_code, 429)
        finally:
            app.extensions['jobs'].pool.shutdown(wait=True)

    def test_success_clears_earlier_failures_only(self):
        for _ in range(9):
            self.assertEqual(self.login().status_code, 401)
        self.assertEqual(self.login(password='fixture-password').status_code, 200)
        for _ in range(10):
            self.assertEqual(self.login().status_code, 401)
        self.assertEqual(self.login().status_code, 429)

    def test_existing_sessions_work_while_same_address_is_throttled(self):
        self.assertEqual(self.login(password='fixture-password').status_code, 200)
        csrf = self.client.get('/api/session', base_url='https://read.example.com').json['csrf']
        for _ in range(10):
            self.login()
        self.assertEqual(self.login().status_code, 429)
        self.assertEqual(self.client.get('/api/library', base_url='https://read.example.com').status_code, 200)
        self.assertEqual(self.client.post('/api/logout', base_url='https://read.example.com',
            headers={'X-CSRF-Token': csrf}, json={}).status_code, 200)

    def test_oversized_body_and_invalid_password_skip_hashing(self):
        with patch('webapp.server.check_password_hash', side_effect=AssertionError('No expensive hashing')):
            self.assertEqual(self.login(password='x' * 9000).status_code, 413)
            for password in [None, [], {}, 5, 'x' * 201]:
                self.assertEqual(self.login(password=password).status_code, 401)
        self.assertEqual(self.login(password='fixture-password').status_code, 200)

    def test_hashing_concurrency_is_bounded_and_slots_recover(self):
        started = threading.Barrier(3)
        release = threading.Event()

        def verify(*args):
            started.wait(timeout=5)
            self.assertTrue(release.wait(timeout=5))
            return False

        def request(ip):
            return self.app.test_client().post('/api/login', base_url='https://read.example.com',
                headers={'CF-Connecting-IP': ip}, json={'password': 'incorrect'}).status_code

        with patch('webapp.server.check_password_hash', side_effect=verify), ThreadPoolExecutor(2) as pool:
            futures = [pool.submit(request, f'203.0.113.{i}') for i in [1, 2]]
            try:
                started.wait(timeout=5)
                busy = self.login(ip='203.0.113.3')
                self.assertEqual(busy.status_code, 503)
                self.assertEqual(busy.headers['Retry-After'], '2')
                self.assertEqual(self.client.get('/api/session').status_code, 200)
            finally:
                release.set()
            self.assertEqual([future.result() for future in futures], [401, 401])
        self.assertEqual(self.login(password='fixture-password').status_code, 200)

    def test_database_unavailable_fails_closed_and_releases_slot(self):
        with patch.object(LoginLimiter, 'reserve', side_effect=sqlite3.OperationalError('fixture unavailable')):
            for _ in range(4):
                response = self.login(password='fixture-password')
                self.assertEqual(response.status_code, 503)
                self.assertNotIn('fixture unavailable', response.text)
        self.assertEqual(self.login(password='fixture-password').status_code, 200)

    def test_sliding_window_expiry_and_rejection_do_not_extend_lockout(self):
        with patch('webapp.login_security.time.time', return_value=1000):
            for _ in range(10):
                self.assertEqual(self.limiter.reserve('203.0.113.1')[1], 0)
        with patch('webapp.login_security.time.time', return_value=1599):
            self.assertEqual(self.limiter.reserve('203.0.113.1'), (None, 1))
        with patch('webapp.login_security.time.time', return_value=1600):
            self.assertEqual(self.limiter.reserve('203.0.113.1')[1], 0)

    def test_concurrent_reservations_never_exceed_budget(self):
        second = LoginLimiter(self.limiter.path, 'fixture-secret')
        with ThreadPoolExecutor(8) as pool:
            results = list(pool.map(lambda i: (self.limiter if i % 2 else second).reserve('203.0.113.1'), range(30)))
        self.assertEqual(sum(ticket is not None for ticket, _ in results), 10)

    def test_concurrent_success_does_not_erase_later_failures(self):
        ticket, _ = self.limiter.reserve('203.0.113.1')
        for _ in range(9):
            self.limiter.reserve('203.0.113.1')
        self.limiter.success('203.0.113.1', ticket)
        self.assertIsNotNone(self.limiter.reserve('203.0.113.1')[0])
        self.assertIsNone(self.limiter.reserve('203.0.113.1')[0])

    def test_expired_rows_are_cleaned_without_saving_raw_ips(self):
        with patch('webapp.login_security.time.time', return_value=1000):
            self.limiter.reserve('203.0.113.1')
        self.assertNotIn(b'203.0.113.1', self.limiter.path.read_bytes())
        with patch('webapp.login_security.time.time', return_value=1601):
            self.limiter.reserve('203.0.113.2')
        with closing(sqlite3.connect(self.limiter.path)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0], 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
