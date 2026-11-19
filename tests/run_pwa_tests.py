"""Public PWA bundle contract; isolated fixtures and no personal runtime data."""
import io
import json
from pathlib import Path
import re
import shutil
import tempfile
import unittest

from PIL import Image
from run_web_tests import WebTests
from webapp import pwa
from webapp.server import STATIC


class PwaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        WebTests.setUpClass()
        cls.client = WebTests.app.test_client()

    @classmethod
    def tearDownClass(cls):
        WebTests.tearDownClass()

    def test_public_install_metadata_and_icons(self):
        response = self.client.get('/manifest.webmanifest')
        self.assertEqual(response.status_code, 200)
        self.assertIn('application/manifest+json', response.content_type)
        data = json.loads(response.data)
        response.close()
        self.assertEqual(data['id'], '/')
        self.assertEqual(data['scope'], '/')
        self.assertEqual(data['display'], 'standalone')
        self.assertEqual(data['start_url'], '/#/library')
        self.assertEqual(data['short_name'], '刊读')
        for icon in data['icons']:
            image = self.client.get(icon['src'])
            self.assertEqual(image.status_code, 200)
            with Image.open(io.BytesIO(image.data)) as png:
                self.assertEqual(f'{png.width}x{png.height}', icon['sizes'])
            image.close()
        html = self.client.get('/').text
        self.assertIn('apple-mobile-web-app-capable', html)
        self.assertIn('apple-touch-icon', html)
        self.assertRegex(html, r'/static/icon-180.png\?v=[a-f0-9]+')
        self.assertRegex(html, r'/manifest.webmanifest\?v=[a-f0-9]+')

    def test_public_bundle_versions_and_private_api_boundaries(self):
        version, urls = pwa.bundle(STATIC)
        response = self.client.get('/')
        self.assertEqual(response.headers['X-Kandu-Shell-Version'], version)
        self.assertIn("worker-src 'self'", response.headers['Content-Security-Policy'])
        worker = self.client.get('/sw.js')
        self.assertEqual(worker.status_code, 200)
        self.assertEqual(worker.headers['Service-Worker-Allowed'], '/')
        self.assertNotIn('immutable', worker.headers['Cache-Control'])
        self.assertNotIn('__KANDU_', worker.text)
        self.assertIn(version, worker.text)
        for url in urls:
            resource = self.client.get(url)
            self.assertEqual(resource.status_code, 200)
            self.assertEqual(resource.headers['X-Kandu-Asset-Version'], url.split('v=')[1])
            resource.close()
        for path in ['/api/books/sample/articles/0', '/api/library', '/api/settings', '/api/wordbook']:
            self.assertEqual(self.client.get(path).status_code, 401)
        self.assertFalse(self.client.get('/api/session').json['authenticated'])
        self.assertEqual(self.client.get('/static/service-worker.js').status_code, 404)
        self.assertNotIn('SOURCE_SECRET_DO_NOT_EXPOSE', worker.text + response.text)

    def test_every_shell_change_invalidates_worker(self):
        with tempfile.TemporaryDirectory(prefix='kandu-pwa-assets-') as folder:
            root = Path(folder) / 'static'
            shutil.copytree(STATIC, root)
            original = pwa.worker(root)
            for name in ['app.js', 'pwa.js', 'app.css', 'index.html', 'manifest.webmanifest',
                         'icon-180.png', 'service-worker.js']:
                path = root / name
                old = path.read_bytes()
                path.write_bytes(old + b'\n')
                self.assertNotEqual(pwa.worker(root), original, name)
                path.write_bytes(old)
            self.assertEqual(pwa.worker(root), original)
            # Every HTML script is a versioned member of the shell bundle.
            html = pwa.index(root)
            _, urls = pwa.bundle(root)
            for url in re.findall(r'<script src="([^"]+)"', html):
                self.assertIn(url, urls)


if __name__ == '__main__':
    unittest.main(defaultTest='PwaTests', verbosity=2)
