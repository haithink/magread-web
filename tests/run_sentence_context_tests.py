"""Context, streaming, explicit review and safe replacement with mocked models."""
import copy
import json
import threading
import unittest
from unittest.mock import patch

from run_web_tests import WebTests, config, store, gen, llm
from kandu import core_sentence as sentences

REAL_CHAT = llm.chat


class SentenceContextTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        WebTests.setUpClass()

    @classmethod
    def tearDownClass(cls):
        WebTests.tearDownClass()

    def setUp(self):
        self.api = WebTests('test_sentence_analysis_uses_existing_cache')
        self.api.setUp()
        for path in store._cache_dir('sample').glob('analysis_*.json'):
            path.unlink()
        self.cfg = config.load_settings()['llms'][0]
        self.sentence = WebTests.article['paragraphs'][0]
        self.context = sentences.context(WebTests.article, 0, self.sentence)
        self.identity = sentences.identity(self.context, self.cfg)
        self.result = dict(context='根据段落评价回答的态度。', translation='这个回答很坦率。',
            grammarComponents=[dict(text=self.sentence, label='主句')], grammarPoints=[], vocab=[])

    def generate(self, **kwargs):
        return gen.analyze(WebTests.meta, WebTests.article, self.sentence, '', '', self.cfg,
            context=self.context, cache_identity=self.identity, **kwargs)

    def test_adjacent_paragraphs_unicode_occurrence_and_bounded_window(self):
        text = '🌙 A reply. A reply.'
        art = dict(title='A title', paragraphs=['Before.', text, 'After.'])
        at = text.rfind('A reply.')
        context = sentences.context(art, 1, 'A reply.', at)
        self.assertEqual(context['selectedStart'], at)
        self.assertEqual(context['previousParagraph'], 'Before.')
        self.assertEqual(context['nextParagraph'], 'After.')
        self.assertEqual(context['paragraph'][context['selectedStart']:context['selectedEnd']], 'A reply.')
        other = sentences.context(art, 1, 'A reply.', text.find('A reply.'))
        self.assertNotEqual(sentences.identity(context, self.cfg), sentences.identity(other, self.cfg))
        for bad in [-1, True, 2.5, at+1]:
            with self.assertRaises(ValueError):
                sentences.context(art, 1, 'A reply.', bad)
        large = dict(title='Long', paragraphs=['z'*8000, 'x'*15000+'Selected sentence.'+'y'*15000, 'w'*8000])
        context = sentences.context(large, 1, 'Selected sentence.', 15000)
        self.assertLessEqual(len(context['paragraph']), 6000)
        self.assertEqual(len(context['previousParagraph']), 1000)
        self.assertEqual(len(context['nextParagraph']), 1000)
        self.assertEqual(context['paragraph'][context['selectedStart']:context['selectedEnd']], 'Selected sentence.')
        repeated = dict(title='Repeated', paragraphs=['A reply. '*2000])
        first = sentences.context(repeated, 0, 'A reply.', 4500)
        second = sentences.context(repeated, 0, 'A reply.', 9000)
        self.assertEqual(first['paragraph'], second['paragraph'])
        self.assertNotEqual(sentences.identity(first, self.cfg), sentences.identity(second, self.cfg))

    def test_identity_separates_source_and_model_but_excludes_credentials(self):
        for key in ['provider', 'baseUrl', 'model']:
            self.assertNotEqual(self.identity, sentences.identity(self.context, dict(self.cfg, **{key:'changed'})))
        changed = dict(self.context, previousParagraph='Different context')
        self.assertNotEqual(self.identity, sentences.identity(changed, self.cfg))
        self.assertEqual(self.identity, sentences.identity(self.context,
            dict(self.cfg, apiKey='OTHER_TEST_KEY', thinkingMode='enabled', reasoningEffort='medium')))

    def test_partial_json_accepts_only_completed_fields_and_escaped_strings(self):
        first = dict(context='A "quote", a } brace and a \\ slash. 中文', translation='译文')
        prefix = json.dumps(first, ensure_ascii=False)[:-1]
        partial = prefix+', "grammarComponents":[{"text":"unfinished'
        self.assertEqual(sentences.partial_object('```json\n'+partial), first)
        self.assertEqual(sentences.partial_object('{"context":"unfinished'), {})
        self.assertEqual(sentences.partial_object('{"context":"ok","translation":"\\u4e'), {'context':'ok'})

    def test_complete_contextual_cache_reused_force_and_deep_replace_it(self):
        store.save_analysis('sample', self.sentence, 0, self.result, self.identity)
        with patch.object(llm, 'chat', side_effect=AssertionError('Cache hit needs no model')):
            self.assertEqual(self.generate(), self.result)
        replacement = dict(self.result, translation='重新讲解的译文。')
        with patch.object(llm, 'chat', return_value=json.dumps(replacement)) as chat:
            fresh = self.generate(force=True)
            self.assertEqual(chat.call_count, 1)
            self.assertEqual(fresh['translation'], replacement['translation'])
            self.assertEqual(store.load_analysis('sample', self.sentence, 0, self.identity), fresh)
            self.generate(deep=True)
            self.assertEqual(chat.call_count, 2)

    def test_legacy_sentence_only_cache_not_used_for_new_context(self):
        store.save_analysis('sample', self.sentence, 0, dict(self.result, translation='OLD'))
        with patch.object(llm, 'chat', return_value=json.dumps(self.result)) as chat:
            self.assertNotEqual(self.generate()['translation'], 'OLD')
            self.assertEqual(chat.call_count, 1)
            self.assertEqual(store.load_analysis('sample', self.sentence, 0)['translation'], 'OLD')

    def test_failed_incomplete_and_cancelled_review_preserve_previous_result(self):
        store.save_analysis('sample', self.sentence, 0, self.result, self.identity)
        for response in [llm.LlmError('test failure'), '{"context":"truncated',
                         json.dumps(dict(self.result, translation='')), llm.RequestCancelled('Stopped')]:
            with self.subTest(response=type(response).__name__):
                options = {'side_effect':response} if isinstance(response, Exception) else {'return_value':response}
                with patch.object(llm, 'chat', **options) as chat, patch.object(gen.time, 'sleep') as sleep:
                    with self.assertRaises((llm.LlmError, gen.GenError, llm.RequestCancelled)):
                        self.generate(force=True)
                    self.assertEqual(chat.call_count, 1)
                    sleep.assert_not_called()
                self.assertEqual(store.load_analysis('sample', self.sentence, 0, self.identity), self.result)

    def test_deep_review_protocol_parameters_and_default_no_thinking(self):
        for provider in ['openai', 'anthropic']:
            for deep in [False, True]:
                cfg = dict(self.cfg, provider=provider, model='deepseek-flash',
                    thinkingMode='enabled', reasoningEffort='high')
                original = copy.deepcopy(cfg)
                def respond(request_cfg, system, user, **kwargs):
                    body = llm._body(request_cfg, system, user, kwargs['max_tokens'], .3, True)
                    self.assertEqual(body['thinking']['type'], 'enabled' if deep else 'disabled')
                    if deep:
                        self.assertEqual(body.get('reasoning_effort') if provider == 'openai'
                            else body.get('output_config', {}).get('effort'), 'medium')
                        self.assertIn('深度复核', system)
                    else:
                        self.assertNotIn('reasoning_effort', body)
                        self.assertNotIn('output_config', body)
                    context = json.loads(user)
                    self.assertEqual(context['nextParagraph'], WebTests.article['paragraphs'][1])
                    self.assertEqual(context['sentence'], self.sentence)
                    self.assertTrue(kwargs['require_complete'])
                    return json.dumps(self.result)
                with patch.object(llm, 'chat', side_effect=respond):
                    gen.analyze(WebTests.meta, WebTests.article, self.sentence, '', '', cfg,
                        context=self.context, force=True, deep=deep)
                self.assertEqual(cfg, original)

    def test_other_models_deep_review_keep_provider_configuration(self):
        cfg = dict(self.cfg, model='other-model')
        with patch.object(llm, 'chat', return_value=json.dumps(self.result)) as chat:
            gen.analyze(WebTests.meta, WebTests.article, self.sentence, '', '', cfg, deep=True)
            self.assertEqual(chat.call_args.args[0], cfg)
            self.assertIn('深度复核', chat.call_args.args[1])

    def test_real_sse_both_protocols_publish_meaning_before_completion_once(self):
        # Exercise chat's real SSE parsing and public job polling, not just a
        # fake progress callback. Hold the stream after the two early fields.
        for provider in ['openai', 'anthropic']:
            gate, entered = threading.Event(), threading.Event()
            def line(obj):
                return ('data: '+json.dumps(obj, ensure_ascii=False)).encode()
            def delta(text):
                return (dict(type='content_block_delta', delta=dict(type='text_delta', text=text))
                    if provider == 'anthropic' else dict(choices=[dict(delta=dict(content=text))]))
            prefix = json.dumps(dict(context='EARLY_MEANING', translation='EARLY_TRANSLATION'))[:-1]+','
            tail = json.dumps({k:v for k,v in self.result.items() if k not in ('context', 'translation')})[1:]
            class Response:
                status_code = 200
                headers = {'Content-Type':'text/event-stream'}
                def __enter__(self): return self
                def __exit__(self, *args): pass
                def close(self): gate.set()
                def iter_lines(self, **kwargs):
                    yield line(delta(prefix))
                    entered.set()
                    if not gate.wait(5): raise AssertionError('Test stream timed out')
                    yield line(delta(tail))
                    yield line(dict(type='message_stop') if provider == 'anthropic'
                        else dict(choices=[dict(delta={}, finish_reason='stop')]))
                    yield b'data: [DONE]'
            cfg = dict(self.cfg, provider=provider)
            settings = dict(config.load_settings(), llms=[cfg])
            try:
                with (patch.object(config, 'load_settings', return_value=settings),
                      patch.object(llm, 'chat', wraps=REAL_CHAT),
                      patch.object(llm.requests, 'post', return_value=Response()) as post):
                    response = self.api.post('/books/sample/articles/0/analyze',
                        dict(sentence=self.sentence, paragraph=0, start=0, force=True))
                    self.assertEqual(response.status_code, 202)
                    self.assertTrue(entered.wait(2))
                    job = self.api.client.get('/api/jobs/'+response.json['jobId']).json
                    self.assertEqual(job['state'], 'running')
                    self.assertEqual(job['partial']['translation'], 'EARLY_TRANSLATION')
                    self.assertNotIn('grammarComponents', job['partial'])
                    self.assertNotIn('result', job['partial'])
                    gate.set()
                    result = self.api.wait_job(response)
                    self.assertEqual(result['state'], 'done', result)
                    self.assertEqual(result['result']['translation'], 'EARLY_TRANSLATION')
                    self.assertEqual(post.call_count, 1)
            finally:
                gate.set()

    def test_cancelled_stream_stops_without_retry_or_cache_replacement(self):
        control = llm.RequestControl()
        store.save_analysis('sample', self.sentence, 0, self.result, self.identity)
        def stream(cfg, system, user, budget, temperature, state):
            state.saw_response = True
            state.append('{"context":"EARLY",')
            control.cancel()
            raise OSError('Socket closed during cancellation')
        with patch.object(llm, 'chat', wraps=REAL_CHAT), patch.object(llm, '_stream_once', side_effect=stream) as call:
            with self.assertRaises(llm.RequestCancelled):
                self.generate(force=True, control=control)
            self.assertEqual(call.call_count, 1)
        self.assertEqual(store.load_analysis('sample', self.sentence, 0, self.identity), self.result)

    def test_api_validates_offset_mode_and_does_not_wait_for_book_generation(self):
        path = '/books/sample/articles/0/analyze'
        for extra in [dict(start=1), dict(start=True), dict(deep='yes'), dict(force=1)]:
            self.assertEqual(self.api.post(path, dict(sentence=self.sentence, paragraph=0, **extra)).status_code, 400)
        gate, entered = threading.Event(), threading.Event()
        def vocabulary(*args, **kwargs):
            entered.set()
            if not gate.wait(5): raise AssertionError('Test vocabulary timed out')
            return dict(failedChunks=0)
        try:
            with (patch.object(gen, 'generate_vocab', side_effect=vocabulary),
                  patch.object(llm, 'chat', return_value=json.dumps(self.result))):
                vocab = self.api.post('/books/sample/articles/0/generate-vocab', {})
                self.assertTrue(entered.wait(2))
                result = self.api.wait_job(self.api.post(path, dict(sentence=self.sentence, paragraph=0)))
                self.assertEqual(result['state'], 'done', result)
                self.assertFalse(gate.is_set())
                gate.set()
                self.assertEqual(self.api.wait_job(vocab)['state'], 'done')
        finally:
            gate.set()


if __name__ == '__main__':
    unittest.main(defaultTest='SentenceContextTests', verbosity=2)
