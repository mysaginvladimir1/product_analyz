import os
import unittest
from unittest.mock import patch
from uuid import uuid4

import pandas as pd
from fastapi import FastAPI
from fastapi.testclient import TestClient
from chat_api import install_chat_api
from llm import GoRouterAnalyst


class ChatTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"ANALYST_API_TOKEN": "test-token"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.calls = []
        self.state = {"df": pd.DataFrame({"value": [1]})}
        self.answer = "Отчёт"
        def process(*args):
            self.calls.append(args)
            return self.answer, None
        app = FastAPI()
        install_chat_api(app, self.state, None, process, 720)
        self.client = TestClient(app)
        self.body = {"userGitLabID": 12, "messages": [
            {"messageUUID": str(uuid4()), "author": "user", "message": "Простои вчера"},
            {"messageUUID": str(uuid4()), "author": "llm", "message": "Отчёт вчера"},
            {"messageUUID": str(uuid4()), "author": "user", "message": "А сегодня?"},
        ]}

    def post(self):
        return self.client.post('/api/chat', json=self.body, headers={"Authorization": "Bearer test-token"})

    def test_history_and_correlation(self):
        response = self.post()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['replyToMessageUUID'], self.body['messages'][-1]['messageUUID'])
        self.assertEqual(self.calls[0][-1][1], {"role": "assistant", "content": "Отчёт вчера"})
        self.assertEqual(self.calls[0][1], 'А сегодня?')

    def test_authentication(self):
        self.assertEqual(self.client.post('/api/chat', json=self.body).status_code, 401)
        self.assertEqual(self.calls, [])

    def test_invalid_history(self):
        self.body['messages'][-1]['author'] = 'llm'
        self.assertEqual(self.post().status_code, 422)
        self.body['messages'] = []
        self.assertEqual(self.post().status_code, 422)

    def test_unavailable_data_and_model(self):
        self.answer = 'ОШИБКА: upstream failure'
        self.assertEqual(self.post().status_code, 502)
        self.state['df'] = pd.DataFrame()
        self.assertEqual(self.post().status_code, 503)

    def test_llm_receives_history(self):
        model = GoRouterAnalyst(api_key='test')
        history = [{"role": "assistant", "content": "Предыдущий ответ"}]
        with patch.object(model, '_call_llm', return_value='{"metrics_type":"kim"}') as call:
            model.parse_user_query('А сегодня?', history=history)
            self.assertEqual(call.call_args.kwargs['messages'][1], history[0])


if __name__ == '__main__':
    unittest.main()
