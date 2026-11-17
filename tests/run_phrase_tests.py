"""Phrase coverage regressions: temporary library, offline dictionary and mocked LLM only."""
import copy
import json
import unittest
from unittest.mock import patch

from run_web_tests import WebTests, config, store, gen, llm, core_dict
from kandu.core_vocab_rules import phrase_candidates


ARTICLE = dict(index=0, title='Artemis and the moon', paragraphs=[
    'It certainly won’t paper over Artemis’ many uncertainties.',
    'The delay glosses over a deeper problem. This falls short of expectations.',
    'We read books.'])


def old_vocab():
    return dict(articleIndex=0, level='cet4', titleEntries=[], idioms=[],
        paragraphs=[dict(index=0, startWords=ARTICLE['paragraphs'][0][:30], entries=[dict(
            word='uncertainty', pos='n.', sense='不确定性', inText='many uncertainties')])],
        processedParagraphs=[-1, 0, 1], phraseProcessedParagraphs=[-1, 0, 1], coverageVersion=gen.VOCAB_COVERAGE_VERSION)


class PhraseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        WebTests.setUpClass()
        cls.app, cls.authcode = WebTests.app, WebTests.authcode

    @classmethod
    def tearDownClass(cls):
        WebTests.tearDownClass()

    post = WebTests.post
    wait_job = WebTests.wait_job

    def setUp(self):
        WebTests.setUp(self)
        store._atomic_json_write(config.app_dir()/'cache/sample/content.json', dict(
            bookSize=WebTests.meta['size'], format='epub', articles=[ARTICLE]))
        store.save_vocab('sample', dict(articleIndex=0, level='cet4'))
        self.cfg = dict(name='test', model='deepseek-test', apiKey='test-placeholder')
        for mock in [patch.object(core_dict, 'ensure_db'), patch.object(core_dict, 'lookup_many', return_value={}),
                     patch.object(config, 'ensure_wordlists'), patch.object(config, 'wordset', return_value={
                         'paper', 'over', 'uncertainty', 'certainly', 'many', 'moon', 'read', 'book'}),
                     patch.dict(core_dict.STATE, ready=True)]:
            mock.start(); self.addCleanup(mock.stop)

    def response(self, entries=None, indices=(-1, 0, 1, 2)):
        return json.dumps(dict(paragraphs=[dict(index=i, entries=(entries or {}).get(i, [])) for i in indices]))

    def test_phrase_candidates_keep_inflections_and_boundaries(self):
        for text, expected in [
            (ARTICLE['paragraphs'][0], ['paper over']),
            ('They papered over the cracks and are papering over disagreements.', ['papered over', 'papering over']),
            ('She glossed over it in light of the news.', ['glossed over', 'in light of']),
            ('They have fallen short of the goal.', ['fallen short of'])]:
            self.assertEqual(set(phrase_candidates(text)), set(expected))
        for text in ['newspaper over there', 'paper overseas', 'paper. Over the next year',
                     'paper-over', 'paper, over', 'We read books.', 'The paper was over there.']:
            self.assertEqual(phrase_candidates(text), {}, text)

    def test_old_completed_cache_is_upgraded_on_read_without_io_or_model(self):
        cached = old_vocab(); original = copy.deepcopy(cached)
        store.save_vocab('sample', cached)
        with (patch.object(core_dict, 'ensure_db', side_effect=AssertionError('No dictionary construction on read')),
              patch.object(store, 'save_vocab', side_effect=AssertionError('GET must not race job writes')),
              patch.object(llm, 'chat', side_effect=AssertionError('No model calls on read'))):
            result = self.client.get('/api/books/sample/articles/0/vocab')
        self.assertEqual(result.status_code, 200)
        value = result.json['vocab']
        section = next(p for p in value['paragraphs'] if p['index'] == 0)
        phrase = next(e for e in section['entries'] if e['word'] == 'paper over')
        self.assertEqual(phrase['pos'], 'phr.')
        self.assertIn('掩盖', phrase['sense'])
        self.assertEqual(phrase['senseSource'], 'phrase_rule')
        self.assertIn('paper over', phrase['inText'])
        self.assertTrue(phrase['contextPending'])
        self.assertEqual(result.json['missing'], [2])
        self.assertNotIn(2, [p['index'] for p in value['paragraphs']])
        self.assertEqual(cached, original)
        self.assertEqual(store.load_vocab('sample', 0, 'cet4'), original)
        self.assertEqual(gen.audit_phrase_coverage(value, ARTICLE), value)
        self.assertEqual(gen.phrase_pending(value, ARTICLE, 0), 1)

    def test_normal_generation_checks_phrases_even_when_components_known(self):
        def answer(cfg, system, user, **kwargs):
            self.assertIn('paper over', user)
            self.assertIn('须逐项解释的原文短语', user)
            self.assertEqual(cfg['thinkingMode'], 'disabled')
            return self.response({0: [['paper over','phr.','掩盖计划的问题，并未解决它们','',False,'']]})
        with patch.object(llm, 'chat', side_effect=answer) as chat:
            result = gen.generate_vocab(WebTests.meta, ARTICLE, 'cet4', self.cfg)
        self.assertEqual(chat.call_count, 1)
        phrase = result['paragraphs'][0]['entries'][0]
        self.assertEqual(phrase['word'], 'paper over')
        self.assertNotIn('contextPending', phrase)
        self.assertEqual(result['processedParagraphs'], [-1,0,1,2])
        # Combined generation does not disable the explicit, independent phrase pass.
        self.assertEqual(gen.phrase_pending(result, ARTICLE, 0), 1)

    def test_omitted_phrase_has_fallback_and_can_be_refined(self):
        with patch.object(llm, 'chat', return_value=self.response()):
            result = gen.generate_vocab(WebTests.meta, ARTICLE, 'cet4', self.cfg)
        phrase = result['paragraphs'][0]['entries'][0]
        self.assertEqual(phrase['word'], 'paper over')
        self.assertTrue(phrase['contextPending'])
        def answer(cfg, system, user, **kwargs):
            self.assertIn('paper over', user)
            self.assertNotIn('### 段落 1', user)
            return self.response({0: [['paper over','phr.','掩盖阿尔忒弥斯计划的不确定性','',False,'']]}, (0,))
        with (patch.object(llm, 'chat', side_effect=answer) as chat,
              patch.object(core_dict, 'ensure_db', side_effect=AssertionError('Phrase-only requires no dictionary'))):
            result = gen.generate_vocab(WebTests.meta, ARTICLE, 'cet4', self.cfg, phrases_only=True, only_para=0)
        self.assertEqual(chat.call_count, 1)
        phrases = result['paragraphs'][0]['entries']
        self.assertEqual(len(phrases), 1)
        self.assertNotIn('contextPending', phrases[0])
        self.assertEqual(gen.phrase_pending(result, ARTICLE, 0), 0)

    def test_legacy_checked_flag_does_not_skip_explicit_phrase_job(self):
        store.save_vocab('sample', old_vocab())
        answer = self.response({0: [['paper over','phr.','掩盖问题而非解决','',False,'']]}, (0,))
        with patch.object(llm, 'chat', return_value=answer) as chat:
            job = self.wait_job(self.post('/books/sample/articles/0/generate-vocab', dict(phrasesOnly=True, paragraph=0)))
        self.assertEqual(job['state'], 'done', job)
        self.assertEqual(chat.call_count, 1)
        value = self.client.get('/api/books/sample/articles/0/vocab').json['vocab']
        entries = value['paragraphs'][0]['entries']
        self.assertEqual({e['word'] for e in entries}, {'paper over','uncertainty'})
        self.assertEqual(next(e['sense'] for e in entries if e['word']=='paper over'), '掩盖问题而非解决')

    def test_failed_or_empty_phrase_pass_remains_retryable_and_preserves_words(self):
        store.save_vocab('sample', old_vocab())
        for answer in [RuntimeError('simulated unavailable'), self.response(indices=(0,))]:
            kwargs = {'side_effect':answer} if isinstance(answer, Exception) else {'return_value':answer}
            with patch.object(llm, 'chat', **kwargs):
                result = gen.generate_vocab(WebTests.meta, ARTICLE, 'cet4', self.cfg, phrases_only=True, only_para=0)
            self.assertEqual(gen.phrase_pending(result, ARTICLE, 0), 1)
            self.assertEqual({e['word'] for e in result['paragraphs'][0]['entries']}, {'paper over','uncertainty'})

    def test_inflected_fallback_replaced_without_duplicate_lemma(self):
        art = dict(ARTICLE, paragraphs=['They papered over disagreements.'])
        with patch.object(llm, 'chat', return_value=self.response(indices=(-1,0))):
            result = gen.generate_vocab(WebTests.meta, art, 'cet4', self.cfg)
        self.assertEqual(result['paragraphs'][0]['entries'][0]['word'], 'papered over')
        answer = self.response({0: [['paper over','phr.','暂时掩盖分歧','',False,'']]}, (0,))
        with patch.object(llm, 'chat', return_value=answer):
            result = gen.generate_vocab(WebTests.meta, art, 'cet4', self.cfg, phrases_only=True, only_para=0)
        self.assertEqual(len(result['paragraphs'][0]['entries']), 1)
        self.assertEqual(gen.phrase_pending(result, art, 0), 0)

    def test_fresh_phrase_only_pass_needs_no_dictionary_and_can_be_requested_again(self):
        answer = self.response({0: [['paper over','phr.','掩盖问题而非解决','',False,'']]}, (0,))
        with (patch.object(llm, 'chat', return_value=answer) as chat,
              patch.object(core_dict, 'ensure_db', side_effect=AssertionError('No dictionary needed')),
              patch.object(core_dict, 'lookup_many', side_effect=AssertionError('No phonetics needed')),
              patch.object(config, 'ensure_wordlists', side_effect=AssertionError('No wordlist needed'))):
            for _ in range(2):
                result = gen.generate_vocab(WebTests.meta, ARTICLE, 'cet4', self.cfg, phrases_only=True, only_para=0)
        self.assertEqual(chat.call_count, 2)
        self.assertNotIn(0, result['processedParagraphs'])
        self.assertEqual(gen.phrase_pending(result, ARTICLE, 0), 0)
        self.assertEqual(len(gen.audit_phrase_coverage(result, ARTICLE)['paragraphs'][0]['entries']), 1)


if __name__ == '__main__':
    unittest.main(defaultTest='PhraseTests', verbosity=2)
