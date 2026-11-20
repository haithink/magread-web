"""Sentence thinking policy and wire payloads; temporary data and mocked models."""
import copy
import json
import unittest
from unittest.mock import patch

from run_web_tests import WebTests, store, gen, llm


class SentenceThinkingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        WebTests.setUpClass()

    @classmethod
    def tearDownClass(cls):
        WebTests.tearDownClass()

    def setUp(self):
        self.sentence = WebTests.article['paragraphs'][0]
        self.result = dict(vocab=[], grammarComponents=[dict(text=self.sentence, label='原句',
            start=0, end=len(self.sentence))], grammarPoints=[], translation='这个回答是坦率的。', context='评价回答的态度。')
        self.path = store._cache_dir('sample') / f'analysis_{store.analysis_key("sample", 0, self.sentence)}.json'
        self.path.unlink(missing_ok=True)

    def tearDown(self):
        self.path.unlink(missing_ok=True)

    def analyze(self, cfg):
        return gen.analyze(WebTests.meta, WebTests.article, self.sentence, '', '', cfg)

    def test_both_deepseek_protocols_send_medium_and_upgrade_old_results(self):
        for provider, model in [('openai', 'deepseek-flash'), ('anthropic', 'deepseek-v4-flash')]:
            with self.subTest(provider=provider):
                cfg=dict(provider=provider, model=model, thinkingMode='disabled')
                original=copy.deepcopy(cfg)
                old=copy.deepcopy(self.result)
                old['generationStats']=dict(model=model, thinkingMode='disabled')
                store.save_analysis('sample', self.sentence, 0, old)
                def respond(request_cfg, system, user, **kwargs):
                    body=llm._body(request_cfg, system, user, kwargs['max_tokens'], .3, True)
                    self.assertEqual(body['thinking'], {'type':'enabled'})
                    if provider=='anthropic':
                        self.assertEqual(body['output_config'], {'effort':'medium'})
                        self.assertNotIn('reasoning_effort',body)
                    else:
                        self.assertEqual(body['reasoning_effort'],'medium')
                        self.assertNotIn('output_config',body)
                    return json.dumps(self.result, ensure_ascii=False)
                with patch.object(llm,'chat',side_effect=respond) as chat:
                    result=self.analyze(cfg)
                    self.assertEqual(result['generationStats']['thinkingMode'],'enabled')
                    self.assertEqual(result['generationStats']['reasoningEffort'],'medium')
                    self.assertFalse(result['partial'])
                    self.assertEqual(self.analyze(cfg),result)
                    self.assertEqual(chat.call_count,1)
                self.assertEqual(cfg,original)

    def test_old_enabled_without_effort_and_other_model_cache_regenerate(self):
        cfg=dict(provider='openai',model='deepseek-flash')
        for stats in [dict(model='deepseek-flash',thinkingMode='enabled'),
                      dict(model='deepseek-v4-pro',thinkingMode='enabled',reasoningEffort='medium')]:
            with self.subTest(stats=stats):
                store.save_analysis('sample',self.sentence,0,dict(self.result,generationStats=stats))
                with patch.object(llm,'chat',return_value=json.dumps(self.result)) as chat:
                    self.analyze(cfg)
                    self.assertEqual(chat.call_count,1)

    def test_other_models_keep_cached_results_and_default_payload(self):
        cfg=dict(provider='openai',model='gpt-6.1-sol')
        store.save_analysis('sample',self.sentence,0,self.result)
        with patch.object(llm,'chat',side_effect=AssertionError('Cached result should be reused')):
            self.assertEqual(self.analyze(cfg),self.result)
        body=llm._body(cfg,'system','user',4000,.3,True)
        self.assertNotIn('thinking',body)
        self.assertNotIn('reasoning_effort',body)
        self.assertNotIn('output_config',body)


if __name__=='__main__':
    unittest.main(defaultTest='SentenceThinkingTests',verbosity=2)
