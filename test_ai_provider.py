import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests
from ai_provider import AIClient


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = Path(self.temp.name) / 'ai.json'
        self.config.write_text((Path(__file__).parent / 'ai-providers.example.json').read_text(), encoding='utf-8')
        env = patch.dict(os.environ, {'AI_CONFIG': str(self.config), 'GOROUTER_API_KEY': 'g-secret', 'VEGA_API_KEY': 'v-secret', 'ZVENO_API_KEY': 'z-secret'}, clear=True)
        env.start()
        self.addCleanup(env.stop)

    def test_all_profiles_route_with_own_credentials(self):
        for provider, host, key in [('gorouter', 'gorouter.ru', 'g-secret'), ('vega', 'api.vega.chat', 'v-secret'), ('zveno', 'api.zveno.ai', 'z-secret')]:
            os.environ['AI_PROVIDER'] = provider
            response = Mock()
            response.json.return_value = {'choices': [{'message': {'content': 'result'}}]}
            with patch('ai_provider.requests.post', return_value=response) as post:
                self.assertEqual(AIClient().complete([{'role': 'user', 'content': 'hello'}]), 'result')
                self.assertIn(host, post.call_args.args[0])
                self.assertEqual(post.call_args.kwargs['headers']['Authorization'], 'Bearer ' + key)

    def test_missing_key_and_unknown_profile_fail_closed(self):
        os.environ['AI_PROVIDER'] = 'vega'
        del os.environ['VEGA_API_KEY']
        with self.assertRaises(ValueError): AIClient()
        os.environ['AI_PROVIDER'] = 'unknown'
        with self.assertRaises(ValueError): AIClient()

    def test_snapshot_stable_for_existing_client(self):
        old = AIClient()
        os.environ['AI_PROVIDER'] = 'zveno'
        self.assertEqual(old.config.name, 'gorouter')
        self.assertEqual(AIClient().config.name, 'zveno')

    def test_errors_do_not_expose_secrets_or_retry_elsewhere(self):
        with patch('ai_provider.requests.post', side_effect=requests.ConnectionError('g-secret')) as post:
            answer = AIClient().complete([])
            self.assertTrue(answer.startswith('ОШИБКА'))
            self.assertNotIn('g-secret', answer)
            self.assertEqual(post.call_count, 1)

    def test_reasoning_is_not_final_answer(self):
        response = Mock()
        response.json.return_value = {'choices': [{'message': {'reasoning_content': 'private reasoning'}}]}
        with patch('ai_provider.requests.post', return_value=response):
            self.assertTrue(AIClient().complete([]).startswith('ОШИБКА'))

    def test_invalid_configuration(self):
        data = json.loads(self.config.read_text())
        for field, value in [('protocol', 'unknown'), ('url', 'http://remote.example/chat'), ('timeout_seconds', -1)]:
            broken = json.loads(json.dumps(data))
            broken['providers']['gorouter'][field] = value
            self.config.write_text(json.dumps(broken))
            with self.assertRaises(ValueError): AIClient()

    def test_legacy_environment(self):
        del os.environ['AI_CONFIG']
        self.assertEqual(AIClient().config.name, 'gorouter')


if __name__ == '__main__': unittest.main()
