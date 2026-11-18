"""Bundled asset integrity, fresh installs and real offline lookup in temporary storage."""
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from kandu import core_config as config, core_dict


class DictionaryAssetsTests(unittest.TestCase):
    def test_bundled_resources_match_source_manifest(self):
        assets = ROOT / 'kandu/assets'
        meta = json.loads((assets / 'dictionary_sources.json').read_text(encoding='utf-8'))
        for name in ('dictionary', 'cet4'):
            artifact = meta[name]
            self.assertEqual(hashlib.sha256((assets / artifact['file']).read_bytes()).hexdigest(), artifact['sha256'])
        self.assertEqual(meta['license'], 'MIT')
        self.assertIn(meta['commit'], meta['source'])
        self.assertIn(meta['commit'], meta['licenseSource'])
        self.assertEqual(hashlib.sha256((ROOT / meta['licenseFile']).read_bytes()).hexdigest(), meta['licenseSha256'])
        with zipfile.ZipFile(assets / meta['dictionary']['file']) as archive:
            self.assertEqual(archive.namelist(), [meta['dictionary']['member']])
            content = archive.read(meta['dictionary']['member'])
        self.assertEqual(hashlib.sha256(content).hexdigest(), meta['dictionary']['memberSha256'])
        self.assertEqual(len(content.splitlines()), meta['dictionary']['rows'])
        self.assertTrue(all(len(line.split(b'\t')) == 5 for line in content.splitlines()))

    def test_new_wordlist_and_existing_user_wordlist_are_preserved(self):
        with tempfile.TemporaryDirectory(prefix='kandu-dictionary-test-') as temporary:
            with patch.dict(os.environ, {'KANDU_DATA_DIR': temporary}):
                config.ensure_wordlists()
                target = Path(temporary) / 'vocab/cet4_words.txt'
                self.assertEqual(target.read_bytes(), config.asset_path('cet4_words.txt').read_bytes())
                words = config.wordset('cet4')
                self.assertTrue({'read', 'book', 'science'} <= words)
                target.write_text('reader-custom-entry\n', encoding='utf-8')
                config.ensure_wordlists()
                self.assertEqual(target.read_text(encoding='utf-8'), 'reader-custom-entry\n')
                self.assertIn('reader-custom-entry', config.wordset('cet4'))
                self.assertIn('read', config.wordset('cet4'))

    def test_dictionary_build_and_lookup_are_offline_and_external(self):
        with tempfile.TemporaryDirectory(prefix='kandu-dictionary-test-') as temporary:
            with patch.dict(os.environ, {'KANDU_DATA_DIR': temporary}), patch.dict(core_dict.STATE, ready=False, building=False, error=''):
                core_dict.ensure_db(background=False)
                self.assertTrue(core_dict.STATE['ready'], core_dict.STATE['error'])
                self.assertTrue((Path(temporary) / 'ecdict.db').exists())
                entries = core_dict.lookup_many(['candid', 'stubborn', 'chart', 'reading', 'London'])
                for word in ['candid', 'stubborn', 'chart', 'london']:
                    self.assertFalse(entries[word].get('miss'), word)
                    self.assertTrue(entries[word]['translation'])
                self.assertFalse(entries['reading'].get('miss'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
