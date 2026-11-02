"""Real aiohttp integration coverage, skipped only when aiohttp is unavailable."""
import asyncio
import importlib.util
import threading
import unittest
from unittest.mock import patch

import test_routes as adapters
from test_features import db, service, scanner


@unittest.skipUnless(importlib.util.find_spec('aiohttp'), 'aiohttp is required for HTTP integration tests')
class HTTPTests(unittest.TestCase):
    setUp = adapters.RouteTests.setUp
    tearDown = adapters.RouteTests.tearDown
    run_async = adapters.RouteTests.run_async

    def test_database_wait_does_not_block_other_http_requests(self):
        async def exercise():
            from aiohttp import web
            from aiohttp.test_utils import TestClient, TestServer
            entered, release = threading.Event(), threading.Event()
            def blocker():
                with db._lock:
                    entered.set()
                    release.wait(5)
            worker = threading.Thread(target=blocker)
            worker.start()
            self.assertTrue(entered.wait(1))
            reached = threading.Event()
            original = adapters.routes._fallback_preview
            def fallback(aid):
                reached.set()
                return original(aid)
            app = web.Application()
            app.router.add_get('/preview/{id}', adapters.routes.preview)
            async def health(request):
                return web.Response(text='responsive')
            app.router.add_get('/health', health)
            try:
                with patch.object(adapters.routes, 'web', web), patch.object(adapters.routes, '_fallback_preview', fallback):
                    async with TestClient(TestServer(app)) as client:
                        preview = asyncio.create_task(client.get('/preview/'+'a'*32))
                        for _ in range(100):
                            if reached.is_set(): break
                            await asyncio.sleep(.01)
                        self.assertTrue(reached.is_set())
                        response = await asyncio.wait_for(client.get('/health'), .5)
                        self.assertEqual(await response.text(), 'responsive')
                        self.assertFalse(preview.done())
                        release.set()
                        result = await asyncio.wait_for(preview, 2)
                        self.assertEqual(result.status, 404)
            finally:
                release.set()
                worker.join(2)
        self.run_async(exercise())

    def test_real_bulk_validation_and_queue_response(self):
        async def exercise():
            from aiohttp import web
            from aiohttp.test_utils import TestClient, TestServer
            app = web.Application()
            app.router.add_post('/bulk', adapters.routes.bulk)
            app.router.add_post('/export', adapters.routes.export)
            app.router.add_post('/upload', adapters.routes.import_preview)
            with patch.object(adapters.routes, 'web', web):
                async with TestClient(TestServer(app)) as client:
                    db.save_prompt({'positive': 'keep'})
                    response = await client.post('/bulk', json={'action': 'remove'})
                    self.assertEqual(response.status, 400)
                    self.assertEqual(db.list_prompts()['total'], 1)
                    with patch.object(service, 'MAX_ACTIVE', 0):
                        response = await client.post('/export', json={})
                        self.assertEqual(response.status, 429)
                        self.assertEqual(response.headers['Retry-After'], '30')
                        response = await client.post('/upload', data=b'not accepted')
                        self.assertEqual(response.status, 429)
        self.run_async(exercise())
