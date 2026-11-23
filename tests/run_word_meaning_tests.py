"""Isolated contextual lookup tests: no personal books or real model calls."""
import json
import threading
import unittest
from unittest.mock import patch

from run_web_tests import WebTests, config, store, llm, wordbook, core_dict
from webapp import word_meaning


class MeaningTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        WebTests.setUpClass()
        cls.app, cls.authcode = WebTests.app, WebTests.authcode
        cls.config_password = WebTests.config_password
        cls.baseline_settings=config.load_settings()

    @classmethod
    def tearDownClass(cls):
        WebTests.tearDownClass()

    setUpClient = WebTests.setUp
    post = WebTests.post
    wait_job = WebTests.wait_job

    def setUp(self):
        self.setUpClient()
        self.article = dict(index=0, title='A trip to the moon', paragraphs=[
            'A fully loaded, moon-bound Starship expends almost all of its fuel to get to low Earth orbit. It was bound by gravity.',
            'The moon-bound vehicle needs more fuel.'])
        store._atomic_json_write(config.app_dir()/'cache/sample/content.json',dict(
            bookSize=WebTests.meta['size'],format='epub',articles=[self.article]))
        settings=json.loads(json.dumps(self.baseline_settings))
        settings['llms'][0]['model']='deepseek-test'
        config.save_settings(settings)
        self.original_settings=settings
        wordbook.delete('bound')
        for path in store._cache_dir('sample').glob('word-meaning_*.json'):
            path.unlink()
        self.dict_patch=patch.object(core_dict,'ensure_db')
        self.lookup_patch=patch.object(core_dict,'lookup',return_value=dict(word='bound',lemma='bound',translation='界限；受约束的'))
        self.dict_patch.start();self.lookup_patch.start()
        self.addCleanup(self.dict_patch.stop);self.addCleanup(self.lookup_patch.stop)

    def lookup(self, **changes):
        return self.post('/books/sample/articles/0/lookup',dict(word='bound',paragraph=0,**changes))

    def explain(self, **changes):
        return self.post('/books/sample/articles/0/lookup-context',dict(word='bound',paragraph=0,savedWord='bound',**changes))

    def response(self):
        return json.dumps(dict(sense='驶向月球的；以月球为目的地的',pos='形容词后缀',
            explanation='bound 与 moon 构成 moon-bound，表示目的地是月球；这里不是“界限”。',translation='满载、驶向月球的星舰几乎耗尽燃料。'),ensure_ascii=False)

    def test_async_explanation_dedup_cache_short_prompt_and_saved_source(self):
        saved=self.lookup().json['saved']
        entered,release=threading.Event(),threading.Event()
        def answer(cfg,system,user,**kwargs):
            self.assertEqual(cfg['thinkingMode'],'disabled')
            self.assertEqual(kwargs['max_tokens'],1200)
            self.assertTrue(kwargs['require_complete'])
            context=json.loads(user)
            self.assertIn('moon-bound',context['sentence'])
            self.assertEqual(context['word'],'bound')
            self.assertLess(len(user),1500)
            kwargs['on_delta'](30)
            entered.set();release.wait(3)
            return self.response()
        history=store.load_reading_history('sample')
        with patch.object(llm,'chat',side_effect=answer) as call:
            first=self.explain();self.assertTrue(entered.wait(2))
            second=self.explain();self.assertEqual(first.json['jobId'],second.json['jobId'])
            self.assertEqual(self.client.get('/api/library').status_code,200)
            release.set()
            job=self.wait_job(first);self.assertEqual(job['state'],'done',job)
            record=wordbook.get('bound')
            self.assertEqual(record['sense'],saved['sense'])
            self.assertIn('驶向月球',record['source']['contextMeaning']['sense'])
            again=self.lookup().json
            self.assertIn('驶向月球',again['contextMeaning']['sense'])
            self.assertEqual(call.call_count,1)
        self.assertNotIn('thinkingMode',config.load_settings()['llms'][0])
        self.assertEqual(store.load_reading_history('sample'),history)

    def test_selected_occurrence_context_and_model_are_part_of_cache_identity(self):
        art=self.article
        first=word_meaning.selection(art,0,'bound',art['paragraphs'][0].index('bound'))
        second=word_meaning.selection(art,0,'bound',art['paragraphs'][0].rindex('bound'))
        self.assertIn('moon-bound',first['sentence'])
        self.assertEqual(second['sentence'],'It was bound by gravity.')
        self.assertNotEqual(word_meaning.cache_key(first,{}),word_meaning.cache_key(second,{}))
        self.assertNotEqual(word_meaning.cache_key(first,{}),word_meaning.cache_key(first,{'model':'another'}))
        huge=dict(art,paragraphs=['filler '*15000+'moon-bound '+ 'tail '*15000])
        context=word_meaning.selection(huge,0,'bound')
        self.assertLessEqual(len(context['paragraph']),6000)
        self.assertLessEqual(len(context['sentence']),5000)
        self.assertIn('moon-bound',context['sentence'])

    def test_two_and_three_word_phrases_context_collection_and_limits(self):
        article = dict(index=0, title='Uncertainties', paragraphs=[
            'They paper over problems and fall short of expectations. They paper over problems again.'])
        store._atomic_json_write(store._cache_dir('sample')/'content.json', dict(
            bookSize=WebTests.meta['size'], format='epub', articles=[article]))
        with patch.object(core_dict, 'lookup', return_value={}):
            for phrase in ['paper over', 'fall short of']:
                with self.subTest(phrase=phrase):
                    wordbook.delete(phrase)
                    request = dict(word=phrase, paragraph=0, start=article['paragraphs'][0].index(phrase))
                    response = self.post('/books/sample/articles/0/lookup', request)
                    self.assertEqual(response.status_code, 200, response.json)
                    self.assertEqual(response.json['saved']['word'], phrase)
                    self.assertEqual(response.json['saved']['source']['selectedWord'], phrase)
                    meaning = dict(sense='掩盖' if phrase=='paper over' else '未达到', pos='phr.',
                        explanation='这里结合宾语表达本文中的实际含义。', translation='他们掩盖问题，而且未达到预期。')
                    def answer(cfg, system, user, **kwargs):
                        selected = json.loads(user)
                        self.assertEqual(selected['word'], phrase)
                        self.assertIn(phrase, selected['sentence'])
                        return json.dumps(meaning, ensure_ascii=False)
                    with patch.object(llm, 'chat', side_effect=answer) as call:
                        job = self.wait_job(self.post('/books/sample/articles/0/lookup-context',
                            dict(request, savedWord=phrase)))
                        self.assertEqual(job['state'], 'done', job)
                        self.assertEqual(wordbook.get(phrase)['source']['contextMeaning']['sense'], meaning['sense'])
                        cached = self.post('/books/sample/articles/0/lookup', request).json
                        self.assertEqual(cached['contextMeaning']['sense'], meaning['sense'])
                        self.assertEqual(call.call_count, 1)
            first = word_meaning.selection(article, 0, 'paper over', article['paragraphs'][0].index('paper over'))
            second = word_meaning.selection(article, 0, 'paper over', article['paragraphs'][0].rindex('paper over'))
            self.assertNotEqual(word_meaning.cache_key(first, {}), word_meaning.cache_key(second, {}))
            for quote in ['paper over problems and', 'They paper over problems and fall short of expectations.', 'paper, over']:
                with self.subTest(quote=quote):
                    for path in ['/lookup', '/lookup-context']:
                        with patch.object(llm, 'chat') as call:
                            response = self.post('/books/sample/articles/0'+path, dict(word=quote, paragraph=0))
                            self.assertEqual(response.status_code, 400, response.json)
                            call.assert_not_called()
        self.assertEqual(word_meaning.selection(article, 0, 'paper\n over')['word'], 'paper over')

    def test_malformed_response_is_not_cached_and_can_retry(self):
        self.lookup()
        with patch.object(llm,'chat',return_value='{"sense":"截断'):
            job=self.wait_job(self.explain())
        self.assertEqual(job['state'],'error')
        self.assertFalse(list(store._cache_dir('sample').glob('word-meaning_*.json')))
        self.assertIsNotNone(wordbook.get('bound'))
        with patch.object(llm,'chat',return_value=self.response()):
            self.assertEqual(self.wait_job(self.explain())['state'],'done')

    def test_personal_edits_survive_and_deleted_words_are_not_recreated(self):
        for delete in [False,True]:
            self.lookup()
            entered,release=threading.Event(),threading.Event()
            def answer(*args,**kwargs):
                entered.set();release.wait(3);return self.response()
            for path in store._cache_dir('sample').glob('word-meaning_*.json'):path.unlink()
            with patch.object(llm,'chat',side_effect=answer):
                job=self.explain();self.assertTrue(entered.wait(2))
                if delete:wordbook.delete('bound')
                else:wordbook.save(dict(word='bound',sense='个人释义',note='个人笔记'))
                release.set();self.assertEqual(self.wait_job(job)['state'],'done')
            if delete:self.assertIsNone(wordbook.get('bound'))
            else:
                self.assertEqual(wordbook.get('bound')['sense'],'个人释义')
                self.assertEqual(wordbook.get('bound')['note'],'个人笔记')

    def test_newer_lookup_source_is_not_overwritten_by_an_older_job(self):
        self.lookup()
        entered,release=threading.Event(),threading.Event()
        def answer(*args,**kwargs):
            entered.set();release.wait(3);return self.response()
        with patch.object(llm,'chat',side_effect=answer):
            job=self.explain();self.assertTrue(entered.wait(2))
            other=self.post('/books/sample/articles/0/lookup',dict(word='bound',paragraph=1))
            self.assertEqual(other.status_code,200)
            release.set();self.wait_job(job)
        source=wordbook.get('bound')['source']
        self.assertEqual(source['paragraph'],1)
        self.assertNotIn('contextMeaning',source)

    def test_invalid_selection_or_missing_model_does_not_call_a_model(self):
        with patch.object(llm,'chat') as call:
            for value in [dict(word='missing',paragraph=0),dict(word='bound',paragraph=-1),
                          dict(word='bound',paragraph=0,start=999),dict(word='bound',paragraph=0,start=True)]:
                self.assertEqual(self.post('/books/sample/articles/0/lookup',value).status_code,400)
                self.assertEqual(self.post('/books/sample/articles/0/lookup-context',value).status_code,400)
            self.assertIsNone(wordbook.get('bound'))
            config.save_settings(dict(self.original_settings,llms=[],defaultLlm=''))
            self.assertFalse(self.lookup().json['canExplain'])
            self.assertEqual(self.explain().status_code,400)
            call.assert_not_called()


if __name__=='__main__':
    unittest.main(defaultTest='MeaningTests',verbosity=2)
