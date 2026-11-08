"""Exercise real route handlers with lightweight request/response adapters.

No live ComfyUI or third-party HTTP test dependency is required.
"""
import asyncio
import json
from pathlib import Path
import sys
import time
import types
import unittest

import test_library as base
from test_features import library, archives, service


class Response:
    def __init__(self, data=None, status=200, headers=None):
        self.data, self.status, self.headers = data, status, headers or {}


class Routes:
    def __init__(self): self.entries={}
    def get(self,path): return self.register('GET',path)
    def post(self,path): return self.register('POST',path)
    def register(self,method,path):
        def attach(fn): self.entries[(method,path)]=fn; return fn
        return attach


registry=Routes()
web=types.SimpleNamespace(json_response=lambda data,**kw:Response(data,**kw),Response=Response,
                          FileResponse=lambda path,**kw:Response({'file':str(path)},**kw))
server=types.ModuleType('server');server.PromptServer=types.SimpleNamespace(instance=types.SimpleNamespace(routes=registry))
aio=types.ModuleType('aiohttp');aio.web=web
# Route imports use the actual storage implementation and only adapt HTTP objects.
previous={name:sys.modules.get(name) for name in ('aiohttp','server')}
sys.modules['aiohttp']=aio;sys.modules['server']=server
routes=base.load('routes')
for name,value in previous.items():
    if value is None:sys.modules.pop(name,None)
    else:sys.modules[name]=value


class Request:
    def __init__(self,body=None,query=None,**params):
        self.body=body or {};self.query=query or {};self.rel_url=types.SimpleNamespace(query=self.query)
        self.match_info=params
    async def json(self):return self.body


class RouteTests(unittest.TestCase):
    def test_selection_requires_explicit_valid_input(self):
        pid, _ = base.db.save_prompt({'positive': 'selection example'})
        for body in ({}, {'ids': 'bad'}, {'ids': [pid], 'filters': {}}, {'filters': {'unknown': 'x'}}):
            self.assertEqual(self.run_async(routes.selection(Request(body))).status, 400)
        result = self.run_async(routes.selection(Request({'ids': [pid]})))
        self.assertEqual(result.data['ids'], [pid])
        result = self.run_async(routes.selection(Request({'filters': {}})))
        self.assertEqual(result.data['ids'], [pid])
    setUp=base.StorageTests.setUp
    def tearDown(self):
        service.stop();base.scanner._stopping=False
        service._jobs.clear()
        base.StorageTests.tearDown(self)
    def run_async(self,coro):
        loop=asyncio.new_event_loop()
        # This host suppresses cross-thread socket wakeups; a timer keeps the test loop responsive.
        def pulse():
            loop.call_later(.01,pulse)
        pulse()
        try:
            return loop.run_until_complete(coro)
        finally:
            # Avoid Python 3.14 runner shutdown's thread wakeup issue in this test host.
            if loop._default_executor:
                loop._default_executor.shutdown(wait=True)
            loop.close()

    def test_save_workflow_collection_bulk_and_download(self):
        result=self.run_async(routes.save_workflow(Request({'workflow':{'nodes':[]}})))
        self.assertEqual(result.status,200);pid=result.data['id'];wid=result.data['workflow_id']
        result=self.run_async(routes.collection_save(Request({'name':'Examples'})))
        cid=result.data['id']
        response=self.run_async(routes.bulk(Request({'selection':{'ids':[pid]},'action':'collection_add','value':cid})))
        self.assertEqual(response.data['count'],1)
        response=self.run_async(routes.list_prompts(Request(query={'collection':cid})))
        self.assertEqual(response.data['total'],1)
        response=self.run_async(routes.workflow(Request(id=wid,representation='editor',query={'download':'1'})))
        self.assertIn('attachment',response.headers['Content-Disposition'])
        self.assertEqual(response.data,{'nodes':[]})
        self.assertEqual(self.run_async(routes.workflow(Request(id=wid,representation='api'))).status,404)

    def test_job_progress_export_and_restore_confirmation(self):
        base.db.save_prompt({'positive':'example'})
        result=self.run_async(routes.export(Request()))
        jid=result.data['job']
        for _ in range(100):
            result=self.run_async(routes.job(Request(id=jid)))
            if result.data['state'] not in ('queued','running'):break
            time.sleep(.01)
        self.assertEqual(result.data['state'],'complete',result.data)
        response=self.run_async(routes.job_download(Request(id=jid)))
        self.assertTrue(Path(response.data['file']).is_file())
        self.assertEqual(self.run_async(routes.restore(Request(id='unused'))).status,400)

    def test_invalid_settings_and_media(self):
        self.assertEqual(self.run_async(routes.preferences_update(Request({'scan_interval':0}))).status,400)
        self.assertEqual(self.run_async(routes.media(Request(id='missing'))).status,404)
        self.assertEqual(self.run_async(routes.preview(Request(id='../bad'))).status,400)
        self.assertEqual(self.run_async(routes.import_commit(Request({'preview_job':'missing'}))).status,404)
