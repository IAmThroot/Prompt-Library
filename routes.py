"""REST endpoints under /prompt_library/* (also reachable as /api/prompt_library/*)."""
import asyncio
import functools
import sqlite3

from aiohttp import web
from server import PromptServer

from . import db, extract, scanner, library, archives, service

routes = PromptServer.instance.routes


def _int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


async def _body(request):
    try:
        data = await request.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _error(message, status=400):
    return web.json_response({"error": message}, status=status)


@routes.get("/prompt_library/list")
async def list_prompts(request):
    q = request.rel_url.query
    return web.json_response(await asyncio.to_thread(db.list_prompts,
        search=q.get("search", ""), flt=q.get("filter", "all"), sort=q.get("sort", "latest"),
        offset=max(_int(q.get("offset"), 0), 0), limit=min(max(_int(q.get("limit"), 50), 1), 200), collection=q.get("collection") or None))


@routes.get("/prompt_library/files/{id}")
async def prompt_files(request):
    return web.json_response({"files": await asyncio.to_thread(db.prompt_files,_int(request.match_info["id"], 0))})


@routes.get("/prompt_library/thumb/{id}")
async def thumb(request):
    path = await asyncio.to_thread(db.thumb_path, _int(request.match_info["id"], 0))
    if not await asyncio.to_thread(db.valid_thumb,path):
        return web.Response(status=404)
    return web.FileResponse(path, headers={"Cache-Control": "max-age=86400"})


@routes.post("/prompt_library/scan")
async def scan(request):
    body = await _body(request)
    preferences = await asyncio.to_thread(library.settings)
    return web.json_response({"started": scanner.start_scan(force=bool(body.get("force")),
        include_hidden=preferences['include_hidden'], follow_symlinks=preferences['follow_symlinks'])})


@routes.get("/prompt_library/status")
async def scan_status(request):
    return web.json_response(scanner.get_status())


@routes.post("/prompt_library/save")
async def save(request):
    body = await _body(request)
    data = {"positive": str(body.get("positive") or ""), "negative": str(body.get("negative") or "")}
    if not (extract.norm(data["positive"]) or extract.norm(data["negative"])):
        return _error("Nothing to save: both prompts are empty.")
    pid, created = await asyncio.to_thread(db.save_prompt,data, title=str(body.get("title") or ""), tags=str(body.get("tags") or ""),
                                  notes=str(body.get("notes") or ""))
    return web.json_response({"id": pid, "created": created})


@routes.post("/prompt_library/save_workflow")
async def save_workflow(request):
    """Body: {"prompt": <API-format graph from app.graphToPrompt()>}"""
    body = await _body(request)
    try:
        result = await asyncio.to_thread(library.save_workflow, body.get('prompt'), body.get('workflow'))
        return web.json_response(result)
    except ValueError as exc:
        return _error(str(exc))


@routes.post("/prompt_library/update/{id}")
async def update(request):
    try:
        pid = await asyncio.to_thread(db.update_prompt,_int(request.match_info["id"], 0), await _body(request))
    except KeyError:
        return _error("Prompt not found.", 404)
    except ValueError as e:
        return _error(str(e), 409)
    return web.json_response({"ok": True, "id": pid})


@routes.post("/prompt_library/delete/{id}")
async def delete(request):
    await asyncio.to_thread(db.delete_prompt,_int(request.match_info["id"], 0))
    return web.json_response({"ok": True})


@routes.get("/prompt_library/report")
async def report(request):
    return web.json_response({"scan": scanner.get_status(), "files": await asyncio.to_thread(db.scan_report)})


@routes.get("/prompt_library/history/{id}")
async def history(request):
    return web.json_response({"generations": await asyncio.to_thread(db.generation_history,_int(request.match_info["id"], 0))})


def guarded(fn):
    @functools.wraps(fn)
    async def wrapped(request):
        try:
            return await fn(request)
        except service.JobCapacityError as exc:
            return web.json_response({'error': str(exc)}, status=429, headers={'Retry-After': '30'})
        except KeyError:
            return _error('Not found',404)
        except (ValueError, TypeError, sqlite3.IntegrityError) as exc:
            return _error(str(exc),400)
    return wrapped


@routes.get('/prompt_library/settings')
@guarded
async def preferences(request):
    return web.json_response(await asyncio.to_thread(library.settings))


@routes.post('/prompt_library/settings')
@guarded
async def preferences_update(request):
    return web.json_response(await asyncio.to_thread(library.update_settings,await _body(request)))


@routes.get('/prompt_library/service')
@guarded
async def service_status(request):
    return web.json_response(await asyncio.to_thread(service.status))


@routes.get('/prompt_library/collections')
@guarded
async def collections(request):
    return web.json_response({'items':await asyncio.to_thread(library.collections)})


@routes.post('/prompt_library/collections')
@guarded
async def collection_save(request):
    body = await _body(request)
    return web.json_response({'id':await asyncio.to_thread(library.collection_save,body.get('name',''),body.get('id'))})


@routes.post('/prompt_library/collections/{id}/delete')
@guarded
async def collection_delete(request):
    await asyncio.to_thread(library.collection_delete,request.match_info['id'])
    return web.json_response({'ok':True})


@routes.post('/prompt_library/selection')
@guarded
async def selection(request):
    ids = await asyncio.to_thread(library.selected_ids,await _body(request))
    return web.json_response({'ids':ids,'count':len(ids)})


@routes.post('/prompt_library/bulk')
@guarded
async def bulk(request):
    body = await _body(request)
    return web.json_response(await asyncio.to_thread(library.bulk,body.get('selection'),body.get('action'),body.get('value')))


@routes.get('/prompt_library/workflows/{id}')
@guarded
async def workflows(request):
    return web.json_response({'items':await asyncio.to_thread(library.workflows,_int(request.match_info['id'],0))})


@routes.get('/prompt_library/workflow/{id}/{representation}')
@guarded
async def workflow(request):
    result = await asyncio.to_thread(library.workflow,request.match_info['id'],request.match_info['representation'])
    headers = {'Content-Disposition': 'attachment; filename="workflow.json"'} if request.query.get('download') else {}
    return web.json_response(result,headers=headers)


@routes.get('/prompt_library/gallery/{id}')
@guarded
async def gallery(request):
    result = await asyncio.to_thread(library.gallery,_int(request.match_info['id'],0),
                                    max(0,_int(request.query.get('offset'),0)),min(100,max(1,_int(request.query.get('limit'),24))))
    return web.json_response(result)


@routes.get('/prompt_library/media/{id}')
@guarded
async def media(request):
    path = await asyncio.to_thread(library.asset_path,request.match_info['id'])
    if not path:
        return _error('Original media is missing or has changed',404)
    return web.FileResponse(path,headers={'Cache-Control':'no-store'})


def _fallback_preview(aid):
    with db.db() as conn:
        row = conn.execute('SELECT prompt_id FROM generations WHERE asset_id=? ORDER BY id DESC LIMIT 1', (aid,)).fetchone()
    return db.thumb_path(row[0]) if row else ''


@routes.get('/prompt_library/preview/{id}')
@guarded
async def preview(request):
    aid = request.match_info['id']
    path = await asyncio.to_thread(library.asset_thumb, aid)
    if not await asyncio.to_thread(db.valid_thumb,path):
        path = await asyncio.to_thread(_fallback_preview, aid)
    if not await asyncio.to_thread(db.valid_thumb,path):
        return web.Response(status=404)
    return web.FileResponse(path)


@routes.post('/prompt_library/export')
@guarded
async def export(request):
    selection = await _body(request)
    jid = await asyncio.to_thread(service.submit, 'export',lambda jid: archives.export_archive(service.job_file(jid,'.zip'),selection))
    return web.json_response({'job':jid})


@routes.post('/prompt_library/import/preview')
@guarded
async def import_preview(request):
    token = library.uid()
    jid = await asyncio.to_thread(service.reserve, 'import_preview', upload=token)
    path = await asyncio.to_thread(service.job_file, token, '.upload')
    limit = (await asyncio.to_thread(library.settings))['archive_max_mb']*1024**2
    size = 0
    output = None
    try:
        output = await asyncio.to_thread(open, path, 'wb')
        async for chunk in request.content.iter_chunked(1024*1024):
            size += len(chunk)
            if size > limit:
                raise ValueError('Archive exceeds upload limit')
            await asyncio.to_thread(output.write, chunk)
        await asyncio.to_thread(output.close)
        output = None
        def inspect(jid):
            report = archives.preview_import(path)
            report['upload'] = token
            return service.compact_report(report, service.job_file(jid, '.json'))
        await asyncio.to_thread(service.submit, 'import_preview', inspect, jid=jid)
        return web.json_response({'job':jid})
    except BaseException:
        if output:
            await asyncio.to_thread(output.close)
        await asyncio.to_thread(service.fail, jid, 'Upload interrupted or rejected')
        raise


@routes.post('/prompt_library/import/commit')
@guarded
async def import_commit(request):
    body = await _body(request)
    preview = await asyncio.to_thread(service.job, body.get('preview_job'))
    if preview['kind']!='import_preview' or preview['state']!='complete':
        raise ValueError('Preview this archive before importing it')
    path = await asyncio.to_thread(service.job_file, preview['result']['upload'], '.upload')
    def commit(jid):
        report = archives.import_archive(path, service.job_file(jid, '.json'))
        return service.report_summary(report)
    return web.json_response({'job':await asyncio.to_thread(service.submit, 'import', commit, dependencies=[preview['id']])})


@routes.get('/prompt_library/jobs/{id}')
@guarded
async def job(request):
    return web.json_response(await asyncio.to_thread(service.job, request.match_info['id']))


@routes.get('/prompt_library/jobs/{id}/download')
@guarded
async def job_download(request):
    jid = request.match_info['id']
    job = await asyncio.to_thread(service.job, jid)
    if job['state']!='complete':
        raise ValueError('Job is not complete')
    suffix = '.zip' if job['kind']=='export' else '.json'
    path = await asyncio.to_thread(service.job_file, jid, suffix)
    if not path.is_file():
        raise KeyError(jid)
    return web.FileResponse(path,headers={'Content-Disposition':'attachment; filename="prompt-library'+suffix+'"'})


@routes.get('/prompt_library/backups')
@guarded
async def backups(request):
    return web.json_response({'items':await asyncio.to_thread(archives.list_backups)})


@routes.post('/prompt_library/backups')
@guarded
async def backup(request):
    return web.json_response({'job':await asyncio.to_thread(service.submit, 'backup', lambda jid:archives.create_backup())})


@routes.get('/prompt_library/backups/{id}/download')
@guarded
async def backup_download(request):
    path = await asyncio.to_thread(archives.backup_path,request.match_info['id'])
    return web.FileResponse(path,headers={'Content-Disposition':'attachment; filename="prompt-library-backup.zip"'})


@routes.post('/prompt_library/backups/{id}/restore')
@guarded
async def restore(request):
    if (await _body(request)).get('confirm') is not True:
        raise ValueError('Confirm replacement of the library before restoring')
    bid = request.match_info['id']
    return web.json_response({'job':await asyncio.to_thread(service.submit, 'restore', lambda jid:archives.restore_backup(bid))})
