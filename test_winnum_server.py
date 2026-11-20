"""Exercise the real server with offline Winnum fixtures, without an Excel file."""
import importlib
import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from test_winnum import FakeClient, params
from winnum_client import WinnumError
from winnum_service import WinnumService


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {'DATA_SOURCE': 'winnum',
                                           'DATA_PATH': str(Path(self.temp.name) / 'absent.xlsx'),
                                           'LOG_DIR': self.temp.name})
        self.env.start()
        self.model = Mock()
        self.model.parse_winnum_query.return_value = params('operator_machines', operators=['Иванов'])
        self.model.generate_winnum_response.return_value = 'Иванов работал на станке 3.4_АРМА.'
        self.fake = FakeClient()
        # Do not initialize unrelated Bitrix storage or background integrations.
        sys.modules.pop('server', None)
        with patch('llm.AIAnalyst', return_value=self.model), \
             patch('bitrix_bot.install_bitrix_bot_api'), \
             patch('data_manager.load_excel_data', side_effect=AssertionError('Excel must not be read')):
            self.server = importlib.import_module('server')
        self.server.winnum = WinnumService(self.fake)
        self.client = TestClient(self.server.app)

    def tearDown(self):
        self.client.close()
        sys.modules.pop('server', None)
        for logger in (logging.getLogger(), logging.getLogger('http_access')):
            for handler in list(logger.handlers):
                if getattr(handler, 'analyst_owned', False) or logger.name == 'http_access':
                    logger.removeHandler(handler)
                    handler.close()
        self.env.stop()
        self.temp.cleanup()

    def test_startup_no_excel_and_request_returns_structured_data(self):
        self.assertEqual(self.client.get('/ping').status_code, 200)
        self.fake.products.assert_not_called()
        response = self.client.post('/api/analyze2', json={'query': 'На каких станках работал Иванов за 1 октября 2026?'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['data']['rows'][0]['Минуты регистрации'], 60)
        self.fake.states.assert_not_called()
        self.fake.signals.assert_not_called()

    def test_missing_period_asks_without_network(self):
        self.model.parse_winnum_query.return_value = {'intent': 'kim'}
        response = self.client.post('/api/analyze2', json={'query': 'КИМ станка?'})
        self.assertEqual(response.status_code, 200)
        self.assertIn('период', response.json()['answer'])
        self.fake.products.assert_not_called()

    def test_upstream_failure_has_502_and_safe_message(self):
        self.fake.operators.side_effect = WinnumError('Нет соединения с Winnum.')
        response = self.client.post('/api/analyze2', json={'query': 'Станки оператора за октябрь'})
        self.assertEqual(response.status_code, 502)
        self.assertIn('Winnum', response.json()['detail'])
        self.model.generate_winnum_response.assert_not_called()

    def test_reload_only_clears_catalog(self):
        self.fake.clear_cache = Mock()
        response = self.client.post('/api/reload')
        self.assertEqual(response.status_code, 200)
        self.fake.clear_cache.assert_called_once()
        self.fake.products.assert_not_called()


if __name__ == '__main__':
    unittest.main()
