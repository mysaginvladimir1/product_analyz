import asyncio
import json
import logging
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI, HTTPException
from fastapi.responses import RedirectResponse
from fastapi.testclient import TestClient
from logging_setup import AccessLogMiddleware, configure_logging, request_id, status_description


class LoggingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.env = patch.dict(os.environ, {"LOG_DIR": self.temp.name, "LOG_MAX_BYTES": "2000", "LOG_BACKUP_COUNT": "2"})
        self.env.start()
        self.root_level = logging.getLogger().level
        configure_logging()
        app = FastAPI()

        @app.get('/ok')
        async def ok():
            await asyncio.to_thread(logging.getLogger('test_app').info, 'processing')
            return {'ok': True}

        @app.get('/redirect')
        async def redirect(): return RedirectResponse('/ok', status_code=302)

        @app.get('/status/{code}')
        async def status(code: int): raise HTTPException(code, 'test')

        @app.get('/failure')
        async def failure(): raise RuntimeError('test failure')

        self.client = TestClient(AccessLogMiddleware(app), raise_server_exceptions=False)

    def tearDown(self):
        self.client.close()
        for logger in (logging.getLogger(), logging.getLogger('http_access')):
            for h in list(logger.handlers):
                if getattr(h, 'analyst_owned', False) or logger.name == 'http_access':
                    logger.removeHandler(h)
                    h.close()
        logging.getLogger().setLevel(self.root_level)
        self.env.stop()
        self.temp.cleanup()

    def events(self):
        return [json.loads(line) for line in (self.directory / 'access.log').read_text(encoding='utf-8').splitlines()]

    def test_status_codes_and_descriptions(self):
        for path, expected in [('/ok', 200), ('/redirect', 302), ('/status/401', 401), ('/status/429', 429), ('/missing', 404), ('/failure', 500)]:
            response = self.client.get(path, follow_redirects=False)
            event = self.events()[-1]
            self.assertEqual(response.status_code, expected)
            self.assertEqual(event['status'], expected)
            self.assertEqual(event['status_description'], status_description(expected))
            self.assertEqual(event['request_id'], response.headers['x-request-id'])
            self.assertTrue(event['response_complete'])

    def test_privacy_and_thread_correlation(self):
        response = self.client.get('/ok?token=secret-query', headers={'Authorization': 'Bearer secret-header', 'X-Request-ID': 'untrusted-id'})
        access = (self.directory / 'access.log').read_text(encoding='utf-8')
        self.assertNotIn('secret-query', access)
        self.assertNotIn('secret-header', access)
        self.assertNotIn('untrusted-id', access)
        app_log = (self.directory / 'analyst.log').read_text(encoding='utf-8')
        self.assertIn(response.headers['x-request-id'], app_log)
        self.assertNotIn('secret-query', app_log)
        self.assertEqual(request_id.get(), '-')

    def test_rotation_and_unique_ids(self):
        ids = {self.client.get('/ok').headers['x-request-id'] for _ in range(20)}
        self.assertEqual(len(ids), 20)
        self.assertTrue((self.directory / 'access.log.1').exists())
        self.assertFalse((self.directory / 'access.log.3').exists())

    def test_unknown_status(self):
        self.assertEqual(status_description(599), 'Нестандартный HTTP-код')
        self.assertEqual(status_description(206), 'Partial Content')


if __name__ == '__main__': unittest.main()
