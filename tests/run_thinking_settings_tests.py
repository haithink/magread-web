"""Per-model thinking preferences: isolated settings, real SSE parsing, mocked HTTP."""
import copy
import hashlib
import json
import unittest
from unittest.mock import patch

from run_web_tests import WebTests, config, store, gen, llm
from kandu import core_sentence
from webapp import word_meaning

REAL_CHAT = llm.chat
REAL_CONVERSATION = llm.conversation


class ThinkingSettingsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        WebTests.setUpClass()

    @classmethod
    def tearDownClass(cls):
        WebTests.tearDownClass()

    def setUp(self):
        self.api = WebTests('test_sentence_analysis_uses_existing_cache')
        self.api.setUp()
        self.settings = config.load_settings()
        self.addCleanup(config.save_settings, self.settings)
        self.cfg = self.settings['llms'][0]
        self.sentence = WebTests.article['paragraphs'][0]
        self.result = dict(context='描述回答的态度。', translation='回答很坦率。',
            grammarComponents=[dict(text=self.sentence, label='主句')], vocab=[])

    def test_edit_all_levels_round_trip_preserves_key_and_rejects_invalid_values(self):
        for level in llm.THINKING_LEVELS:
            response = self.api.post('/settings', dict(originalName='test', modelConfig=dict(thinkingLevel=level, apiKey='')))
            self.assertEqual(response.status_code, 200, response.json)
            saved = config.load_settings()['llms'][0]
            self.assertEqual(saved['thinkingLevel'], level)
            self.assertEqual(saved['apiKey'], self.cfg['apiKey'])
            public = self.api.client.get('/api/settings')
            self.assertEqual(public.json['models'][0]['thinkingLevel'], level)
            self.assertNotIn(self.cfg['apiKey'], public.text)
        before = config.settings_path().read_bytes()
        for level in [None, [], {}, True, 2, 'invalid', 'enabled']:
            response = self.api.post('/settings', dict(originalName='test', modelConfig=dict(thinkingLevel=level)))
            self.assertEqual(response.status_code, 400, response.json)
            self.assertEqual(config.settings_path().read_bytes(), before)
        self.api.post('/settings/lock', {})
        self.assertEqual(self.api.post('/settings', dict(originalName='test', modelConfig=dict(thinkingLevel='high'))).status_code, 403)
        self.assertEqual(config.settings_path().read_bytes(), before)

    def test_import_default_and_explicit_level_and_invalid_import_is_atomic(self):
        pasted = json.dumps(dict(baseUrl='https://example.invalid', apiKey='import-test-only', model='sample'))
        for extra, expected in [({}, 'auto'), (dict(thinkingLevel='low'), 'low'), (dict(thinkingLevel='disabled'), 'disabled')]:
            response = self.api.post('/settings', dict(configJson=pasted, name='imported', **extra))
            self.assertEqual(response.status_code, 200, response.json)
            self.assertEqual(config.load_settings()['llms'][-1]['thinkingLevel'], expected)
        before = config.settings_path().read_bytes()
        response = self.api.post('/settings', dict(configJson=pasted, thinkingLevel=['low']))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(config.settings_path().read_bytes(), before)

    def transmit(self, cfg, *, conversation=False):
        provider = cfg.get('provider')
        class Response:
            status_code = 200
            headers = {'Content-Type':'text/event-stream'}
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def close(self): pass
            def iter_lines(self, **kwargs):
                obj = (dict(type='content_block_delta', delta=dict(type='text_delta', text='ok'))
                       if provider == 'anthropic' else dict(choices=[dict(delta=dict(content='ok'))]))
                yield ('data: '+json.dumps(obj)).encode()
                yield b'data: [DONE]'
        with patch.object(llm.requests, 'post', return_value=Response()) as post:
            if conversation:
                text = REAL_CONVERSATION(cfg, 'system', [dict(role='user', content='hello')])
            else:
                text = REAL_CHAT(cfg, 'system', 'hello', max_tokens=2000)
            self.assertEqual(text, 'ok')
            self.assertEqual(post.call_count, 1)
            return post.call_args.kwargs['json']

    def test_deepseek_choices_reach_both_wire_protocols_and_discussion(self):
        for provider in ['openai', 'anthropic']:
            for level in ['disabled', 'low', 'medium', 'high']:
                for conversation in [False, True]:
                    with self.subTest(provider=provider, level=level, conversation=conversation):
                        cfg = dict(self.cfg, provider=provider, model='deepseek-flash', thinkingLevel=level)
                        original = copy.deepcopy(cfg)
                        body = self.transmit(cfg, conversation=conversation)
                        self.assertEqual(body['thinking']['type'], 'disabled' if level == 'disabled' else 'enabled')
                        effort = body.get('reasoning_effort') if provider == 'openai' else body.get('output_config', {}).get('effort')
                        self.assertEqual(effort, None if level == 'disabled' else level)
                        self.assertEqual(cfg, original)

    def test_native_protocol_fields_and_default_payload_preserved(self):
        for level in ['disabled', 'low', 'medium', 'high']:
            body = self.transmit(dict(self.cfg, model='gpt-6.1-sol', thinkingLevel=level))
            self.assertEqual(body['reasoning_effort'], 'none' if level == 'disabled' else level)
            self.assertNotIn('thinking', body)
            self.assertNotIn('temperature', body)
            self.assertEqual(body['max_completion_tokens'], 2000)
            body = self.transmit(dict(self.cfg, provider='anthropic', model='claude-sonnet-4-6', thinkingLevel=level))
            self.assertEqual(body['thinking']['type'], 'disabled' if level == 'disabled' else 'adaptive')
            if level != 'disabled':
                self.assertEqual(body['output_config']['effort'], level)
                self.assertNotIn('temperature', body)
        original = self.transmit(self.cfg)
        self.assertEqual(original, self.transmit(dict(self.cfg, thinkingLevel='auto')))
        self.assertNotIn('thinking', original)
        self.assertNotIn('reasoning_effort', original)

    def test_unsupported_thinking_parameter_reports_error_without_budget_retries(self):
        class Response:
            status_code = 400
            text = '{"error":{"type":"invalid_request_error","message":"Unsupported reasoning_effort: none"}}'
            def __enter__(self): return self
            def __exit__(self, *args): pass
        with patch.object(llm.requests, 'post', return_value=Response()) as post:
            with self.assertRaisesRegex(llm.LlmError, 'reasoning_effort'):
                REAL_CHAT(dict(self.cfg, thinkingLevel='disabled'), 'system', 'hello')
            self.assertEqual(post.call_count, 1)
        self.assertTrue(llm._is_budget_reject('invalid_request_error: max_tokens must be less than 8192'))

    def test_sentence_override_wins_for_normal_and_deep_review_without_mutation(self):
        for level in ['auto', 'disabled', 'low', 'medium', 'high']:
            for deep in [False, True]:
                cfg = dict(self.cfg, model='deepseek-flash', thinkingLevel=level)
                original = copy.deepcopy(cfg)
                with patch.object(llm, 'chat', return_value=json.dumps(self.result)) as chat:
                    result = gen.analyze(WebTests.meta, WebTests.article, self.sentence, '', '', cfg, force=True, deep=deep)
                    request_cfg = chat.call_args.args[0]
                    expected = ('medium' if deep else 'disabled') if level == 'auto' else level
                    self.assertEqual(request_cfg['thinkingMode'], 'disabled' if expected == 'disabled' else 'enabled')
                    self.assertEqual(request_cfg.get('reasoningEffort'), None if expected == 'disabled' else expected)
                    self.assertEqual(result['generationStats']['thinkingMode'], request_cfg['thinkingMode'])
                    self.assertEqual(result['generationStats']['reasoningEffort'], request_cfg.get('reasoningEffort', 'default'))
                self.assertEqual(cfg, original)

    def test_word_meaning_override_wins_and_cached_result_uses_chosen_level(self):
        context = word_meaning.selection(WebTests.article, 0, 'candid')
        answer = json.dumps(dict(sense='坦率的', explanation='评价回答的态度。'))
        for level in ['disabled', 'low', 'high']:
            cfg = dict(self.cfg, model='deepseek-flash', thinkingLevel=level)
            with patch.object(llm, 'chat', return_value=answer) as chat:
                value = word_meaning.explain('sample', context, cfg, lambda **kw:None, llm.RequestControl())
                actual = chat.call_args.args[0]
                self.assertEqual(actual['thinkingMode'], 'disabled' if level == 'disabled' else 'enabled')
                self.assertEqual(word_meaning.explain('sample', context, cfg, lambda **kw:None, llm.RequestControl()), value)
                self.assertEqual(chat.call_count, 1)

    def test_cache_keys_preserve_default_results_and_separate_explicit_levels(self):
        context = core_sentence.context(WebTests.article, 0, self.sentence)
        old = hashlib.sha256(json.dumps([core_sentence.CONTEXT_VERSION, context,
            [self.cfg.get(k, '') for k in ('provider', 'baseUrl', 'model')]], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        self.assertEqual(core_sentence.identity(context, self.cfg), old)
        self.assertEqual(core_sentence.identity(context, dict(self.cfg, thinkingLevel='auto')), old)
        self.assertEqual(word_meaning.cache_key(context, self.cfg), word_meaning.cache_key(context, dict(self.cfg, thinkingLevel='auto')))
        for key in [core_sentence.identity, word_meaning.cache_key]:
            keys = {key(context, dict(self.cfg, thinkingLevel=level)) for level in llm.THINKING_LEVELS}
            self.assertEqual(len(keys), len(llm.THINKING_LEVELS))


if __name__ == '__main__':
    unittest.main(defaultTest='ThinkingSettingsTests', verbosity=2)
