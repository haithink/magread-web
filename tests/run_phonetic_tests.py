"""Legacy pronunciation fixes against bundled data and isolated caches/wordbook."""
from run_web_tests import WebTests, config, store, core_dict, wordbook
from contextlib import closing
import copy
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from kandu import core_phonetics

LEGACY = "'^lu:t\u04d9meit"
CORRECT = "'\u0261lu:t\u04d9meit"


class PhoneticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        WebTests.setUpClass()
        cls.app, cls.authcode = WebTests.app, WebTests.authcode
        cls.config_password = WebTests.config_password

    @classmethod
    def tearDownClass(cls):
        WebTests.tearDownClass()

    setUp, post = WebTests.setUp, WebTests.post

    def test_actual_bundled_glutamate_and_existing_db_return_ipa_g(self):
        with zipfile.ZipFile(core_dict._asset_path()) as archive:
            with archive.open('ecdict_compact.tsv') as stream:
                row = next(line.decode('utf-8').rstrip('\n').split('\t') for line in stream
                           if line.startswith(b'glutamate\t'))
        self.assertEqual(row[2], LEGACY)
        with tempfile.TemporaryDirectory(prefix='kandu-phonetic-db-') as temporary:
            path = Path(temporary)/'ecdict.db'
            with closing(sqlite3.connect(path)) as db:
                db.execute('CREATE TABLE entries(word TEXT PRIMARY KEY, pos TEXT, phonetic TEXT, translation TEXT, exchange TEXT)')
                db.execute('INSERT INTO entries VALUES (?,?,?,?,?)', row)
                db.commit()
            with patch.object(core_dict, '_db_path', return_value=path), patch.dict(core_dict.STATE, ready=True):
                self.assertEqual(core_dict.lookup('glutamate')['phonetic'], CORRECT)
                self.assertEqual(core_dict.lookup_many(['glutamate'])['glutamate']['phonetic'], CORRECT)
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute('SELECT phonetic FROM entries').fetchone()[0], LEGACY)

    def test_normalization_only_changes_carets_in_pronunciation_fields(self):
        original = dict(phonetic=LEGACY, sense='x^2 remains x^2', note='g ^', word='g',
            entries=[dict(phonetic='/g\u0261^/', quote='2^3', phonetics='^')])
        before = copy.deepcopy(original)
        result = core_phonetics.normalize_fields(original)
        self.assertEqual(result['phonetic'], CORRECT)
        self.assertEqual(result['entries'][0]['phonetic'], '/g\u0261\u0261/')
        self.assertEqual(result['entries'][0]['quote'], '2^3')
        self.assertEqual(result['entries'][0]['phonetics'], '^')
        self.assertEqual(result['sense'], before['sense'])
        self.assertEqual(original, before)
        self.assertEqual(core_phonetics.normalize_fields(result), result)
        for value in (None, '', "gud", '\u0261', '/\u0259\u02c8gri\u02d0/', 0):
            self.assertEqual(core_phonetics.normalize(value), value)

    def test_old_vocab_and_sentence_cache_read_without_regeneration_and_new_saves_fixed(self):
        value = dict(articleIndex=0, level='cet4', titleEntries=[], idioms=[],
            processedParagraphs=[0, 1], paragraphs=[dict(index=0, entries=[dict(
                word='glutamate', phonetic=LEGACY, sense='x^2')])])
        path = store._cache_dir('sample')/'vocab_0_cet4.json'
        store._atomic_json_write(path, value)
        loaded = store.load_vocab('sample', 0, 'cet4')
        self.assertEqual(loaded['paragraphs'][0]['entries'][0]['phonetic'], CORRECT)
        self.assertEqual(store._json_read(path), value)
        response = self.client.get('/api/books/sample/articles/0/vocab')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['vocab']['paragraphs'][0]['entries'][0]['phonetic'], CORRECT)
        store.save_vocab('sample', value)
        self.assertEqual(store._json_read(path)['paragraphs'][0]['entries'][0]['phonetic'], CORRECT)
        analysis = dict(vocab=[dict(word='glutamate', phonetic=LEGACY)], translation='x^2')
        path = store._cache_dir('sample')/f'analysis_{store.analysis_key("sample",0,"fixture")}.json'
        store._atomic_json_write(path, analysis)
        self.assertEqual(store.load_analysis('sample','fixture',0)['vocab'][0]['phonetic'], CORRECT)
        store.save_analysis('sample','fixture',0,analysis)
        self.assertEqual(store._json_read(path)['vocab'][0]['phonetic'], CORRECT)
        self.assertEqual(analysis['vocab'][0]['phonetic'], LEGACY)

    def test_old_wordbook_read_and_edit_preserve_notes_and_metadata(self):
        wordbook.save(dict(word='glutamate', phonetic='', sense='An amino acid', note='x^2'))
        with wordbook.connection() as db:
            db.execute('UPDATE words SET phonetic=? WHERE word=?', (LEGACY,'glutamate'))
        before = wordbook.get('glutamate')
        self.assertEqual(before['phonetic'], CORRECT)
        response = self.client.get('/api/wordbook?q=glutamate')
        self.assertEqual(response.json['words'][0]['phonetic'], CORRECT)
        after = wordbook.save(dict(word='glutamate', phonetic=LEGACY, note='x^3'))
        self.assertEqual(after['phonetic'], CORRECT)
        self.assertEqual(after['note'], 'x^3')
        for field in ('created_at','lookups','source','sense'):
            self.assertEqual(after[field], before[field])
        with wordbook.connection() as db:
            self.assertEqual(db.execute('SELECT phonetic FROM words WHERE word=?', ('glutamate',)).fetchone()[0], CORRECT)


if __name__ == '__main__':
    unittest.main(defaultTest='PhoneticTests', verbosity=2)
