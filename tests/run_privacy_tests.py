"""Verify fresh Web installations, data placement and the source privacy checker."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))

from kandu import core_config as config
from tools.check_public_files import find_issues
from webapp.bootstrap import import_desktop
from webapp.server import create_app


class PrivacyTests(unittest.TestCase):
    def test_web_default_and_empty_model_settings(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch.dict(os.environ, {'LOCALAPPDATA': temporary}, clear=True):
                self.assertEqual(config.app_dir(), Path(temporary).resolve() / 'KanduWeb')
                self.assertEqual(config.load_settings()['llms'], [])
                self.assertEqual(config.load_settings()['defaultLlm'], '')

    def test_source_directory_rejected_by_core_and_import(self):
        for path in (SOURCE, SOURCE / 'data', SOURCE / 'webapp' / 'private'):
            with self.subTest(path=path):
                with patch.dict(os.environ, {'KANDU_DATA_DIR': str(path)}):
                    with self.assertRaisesRegex(ValueError, 'outside the source'):
                        config.app_dir()
                with self.assertRaisesRegex(ValueError, 'outside the source'):
                    import_desktop(SOURCE.parent / 'nonexistent-desktop-fixture', path)

    def test_cli_rejects_data_directory_before_writing(self):
        path = SOURCE / 'blocked-private-data'
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
        result = subprocess.run([sys.executable, str(SOURCE / 'run_web.py'), '--data-dir', str(path)],
                                capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 2)
        self.assertIn('outside the source', result.stderr)
        self.assertFalse(path.exists())

    def test_fresh_auth_and_models_only_in_external_data(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch.dict(os.environ, {'KANDU_DATA_DIR': temporary}):
                app = create_app()
                try:
                    root = Path(temporary)
                    code = (root / 'access-code.txt').read_text().strip()
                    client = app.test_client()
                    self.assertEqual(client.get('/api/library').status_code, 401)
                    self.assertEqual(client.post('/api/login', json={'password': code}).status_code, 200)
                    self.assertEqual(client.get('/api/settings').status_code, 403)
                    self.assertEqual(client.get('/api/settings/access').json['configured'], False)
                    self.assertEqual(client.get('/api/preferences').json['vocabLevel'], 'cet4')
                    self.assertEqual(client.get('/api/library').json['books'], [])
                    self.assertTrue((root / 'web-auth.json').is_file())
                    self.assertFalse((SOURCE / 'access-code.txt').exists())
                    self.assertFalse((SOURCE / 'settings.json').exists())
                finally:
                    app.extensions['jobs'].pool.shutdown(wait=True)

    def test_checker_flags_ignored_data_and_tokens_without_echoing_values(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake = 'sk-' + 'a' * 32
            (root / 'settings.json').write_text('{}')
            (root / 'sample.py').write_text('value = ' + repr(fake))
            (root / '.env').write_text('DEMO=fixture')
            _, issues = find_issues(root)
            self.assertTrue(any(name == 'settings.json' for name, _ in issues))
            self.assertTrue(any(name == '.env' for name, _ in issues))
            self.assertTrue(any(reason == 'API token pattern' for _, reason in issues))
            self.assertNotIn(fake, str(issues))


if __name__ == '__main__':
    unittest.main(verbosity=2)
