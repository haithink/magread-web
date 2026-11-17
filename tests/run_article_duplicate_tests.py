"""Earlier-issue title filtering: isolated library, no personal books or paid models."""
from datetime import date
import unittest
from unittest.mock import patch

from run_web_tests import WebTests, config, store
from webapp import article_duplicates as duplicates


def publication(book_id, file_name, titles, *, ext='epub', cached=True, summarized=True):
    root = config.app_dir()
    source = root/'books'/file_name
    source.write_bytes(b'temporary publication')
    meta = dict(id=book_id, fileName=file_name, ext=ext, size=source.stat().st_size,
                path=str(source), lastModified=9999999999999)
    articles = [dict(index=i, title=title, paragraphs=[f'The body of article {i}.'])
                for i, title in enumerate(titles)]
    if cached:
        store._atomic_json_write(root/'cache'/book_id/'content.json', dict(
            format=ext, bookSize=meta['size'], pdfParserVersion=store.PDF_PARSER_VERSION, articles=articles))
    if summarized:
        store.save_digest(dict(bookId=book_id, pdfParserVersion=store.PDF_PARSER_VERSION,
            pdfSourceSize=meta['size'], articles=[dict(index=a['index'], titleEn=a['title'],
            titleZh=f'测试文章 {a["index"]}', summaryZh=f'第 {a["index"]} 篇摘要') for a in articles]))
    return meta, articles


class DuplicateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        WebTests.setUpClass()
        cls.app, cls.authcode = WebTests.app, WebTests.authcode

    @classmethod
    def tearDownClass(cls):
        WebTests.tearDownClass()

    def setUp(self):
        WebTests.setUp(self)
        duplicates._cached_titles.cache_clear()
        self.current, self.articles = publication('new', 'Wired_2026.11.02.epub', [
            'You’ve Never Heard', 'The newly published story', 'Ruby Is Not a Serious Programming Language'])
        self.older, _ = publication('old', 'Wired_2026.08.02.epub', [
            'An earlier story', "YOU'VE NEVER—HEARD!", 'Ruby Is Not a Serious Programming Language'])
        self.library = [self.current, self.older]
        store.save_library(self.library)

    def get(self):
        response = self.client.get('/api/books/new')
        self.assertEqual(response.status_code, 200, response.json)
        return response.json

    def test_api_marks_earlier_titles_preserving_indices_states_history_and_files(self):
        store.set_article_state('new', 0, 'read')
        store.save_reading_progress('new', 0, dict(
            paragraphIndex=0, paragraphOffset=4, paragraphCount=1, wordCount=6,
            progress=.3, title=self.articles[0]['title']))
        paths = [config.app_dir()/'cache/new/content.json', config.app_dir()/'cache/new/digest.json',
                 config.app_dir()/'article_states/new.json', config.app_dir()/'reading_history/new.json']
        before = {p:p.read_bytes() for p in paths}
        value = self.get()
        self.assertEqual(value['duplicateCount'], 2)
        self.assertEqual([a['index'] for a in value['articles']], [0, 1, 2])
        old = value['articles'][0]['earlierOccurrence']
        self.assertEqual((old['bookId'], old['articleIndex'], old['issueDate']), ('old', 1, '2026-08-02'))
        self.assertEqual(value['articles'][0]['state'], 'read')
        self.assertEqual(value['articles'][0]['history']['paragraphOffset'], 4)
        self.assertIsNone(value['articles'][1]['earlierOccurrence'])
        self.assertEqual(before, {p:p.read_bytes() for p in paths})
        # Hidden in the directory only: original reader and summary remain accessible.
        article = self.client.get('/api/books/new/articles/0')
        self.assertEqual(article.status_code, 200)
        self.assertEqual(article.json['article']['index'], 0)
        self.assertEqual(article.json['summaryZh'], '第 0 篇摘要')

    def test_earliest_issue_wins_across_publications_not_import_order(self):
        first, _ = publication('first', 'Other_2025.09.02.pdf', ["You've Never Heard"], ext='pdf')
        future, _ = publication('future', 'Wired_2026.12.02.epub', ['The newly published story'])
        store.save_library([future, self.current, self.older, first])
        value = self.get()
        self.assertEqual(value['articles'][0]['earlierOccurrence']['bookId'], 'first')
        self.assertIsNone(value['articles'][1]['earlierOccurrence'])
        # The oldest issue has no earlier occurrence even though later copies exist.
        self.assertEqual(self.client.get('/api/books/first').json['duplicateCount'], 0)

    def test_same_date_unknown_date_and_self_never_hide_each_other(self):
        entries = [self.current]
        for ident, name in [('same', 'Wired_2026.11.02.pdf'), ('unknown', 'Wired_undated.epub'),
                            ('bad', 'Wired_2026.13.02.epub')]:
            meta, _ = publication(ident, name, [self.articles[0]['title']], ext='pdf' if ident=='same' else 'epub')
            entries.append(meta)
        store.save_library(entries)
        self.assertEqual(self.get()['duplicateCount'], 0)
        self.assertEqual(self.client.get('/api/books/unknown').json['duplicateCount'], 0)
        for name, expected in [('Wired_2026.11.02.epub', date(2026,11,2)),
                               ('Wired_2026-11.epub', date(2026,11,1)),
                               ('Weekly_2026_2_3.pdf', date(2026,2,3)), ('A book.epub', None),
                               ('Wired_2026.02.30.pdf', None)]:
            self.assertEqual(duplicates.issue_date(dict(fileName=name)), expected)

    def test_prior_issues_use_cached_titles_without_parsing_source_or_llm(self):
        missing, _ = publication('missing', 'Wired_2026.01.02.epub', ['The newly published story'],
                                 cached=False, summarized=False)
        store.save_library([self.current, missing, self.older])
        with (patch.object(store, 'ensure_content', wraps=store.ensure_content) as ensure,
              patch.object(store, 'epub_parse', side_effect=AssertionError('No source parsing')),
              patch.object(store, 'pdf_parse', side_effect=AssertionError('No source parsing'))):
            value = self.get()
        self.assertEqual(ensure.call_count, 1)
        self.assertEqual(ensure.call_args.args[0]['id'], 'new')
        self.assertEqual(value['duplicateCount'], 2)
        self.assertFalse((config.app_dir()/'cache/missing').exists())

    def test_cached_index_is_reused_and_invalidates_after_cache_or_library_change(self):
        with patch.object(store, '_json_read', wraps=store._json_read) as read:
            self.assertEqual(len(duplicates.earlier_occurrences(self.current, self.articles)), 2)
            count = read.call_count
            self.assertEqual(len(duplicates.earlier_occurrences(self.current, self.articles)), 2)
            self.assertEqual(read.call_count, count)
        path = config.app_dir()/'cache/old/content.json'
        value = store._json_read(path)
        value['articles'].append(dict(index=99, title='The newly published story', paragraphs=['New content.']))
        store._atomic_json_write(path, value)
        self.assertEqual(self.get()['duplicateCount'], 3)
        store.save_library([self.current])
        self.assertEqual(self.get()['duplicateCount'], 0)

    def test_stale_pdf_or_changed_source_cache_does_not_hide_articles(self):
        for changes in [dict(bookSize=1), dict(format='pdf'), dict(articles=None)]:
            path = config.app_dir()/'cache/old/content.json'
            original = store._json_read(path)
            store._atomic_json_write(path, dict(original, **changes))
            self.assertEqual(self.get()['duplicateCount'], 0)
            store._atomic_json_write(path, original)
        meta, _ = publication('pdf', 'Other_2026.01.02.pdf', [self.articles[1]['title']], ext='pdf')
        path = config.app_dir()/'cache/pdf/content.json'
        store._atomic_json_write(path, dict(store._json_read(path), pdfParserVersion=-1))
        store.save_library([self.current, meta])
        self.assertEqual(self.get()['duplicateCount'], 0)

    def test_summary_only_prior_issue_and_unread_prior_article_also_count(self):
        earlier, _ = publication('digest_only', 'Other_2026.01.02.epub', [self.articles[1]['title']], cached=False)
        store.save_library([self.current, earlier])
        value = self.get()
        self.assertEqual(value['duplicateCount'], 1)
        self.assertEqual(value['articles'][1]['earlierOccurrence']['bookId'], 'digest_only')
        self.assertEqual(self.client.get('/api/books/old').status_code, 404)


if __name__ == '__main__':
    unittest.main(defaultTest='DuplicateTests', verbosity=2)
