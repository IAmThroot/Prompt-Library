"""Browser-independent capture, backup scheduling and asynchronous archive jobs."""
import concurrent.futures
import json
import os
import re
from pathlib import Path
import threading
import time

from . import db, library

maintenance = threading.RLock()
_stop = threading.Event()
_thread = None
_executor = None
_jobs = {}
_job_lock = threading.RLock()
_service_state = {'running': False, 'last_error': None, 'next_scan': None, 'next_backup': None}


MAX_ACTIVE = 32
MAX_RECORDS = 1000
MAX_ARTIFACT_BYTES = 8 * 1024**3
RETENTION = 86400
ACTIVE = ('uploading', 'queued', 'running')


class JobCapacityError(ValueError):
    pass


def _persist(item):
    path = job_file(item['id'], '.job')
    temporary = path.with_suffix('.job.tmp')
    try:
        with open(temporary, 'w', encoding='utf-8') as output:
            json.dump(item, output, ensure_ascii=False, allow_nan=False)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _artifacts(item):
    paths = [job_file(item['id'], suffix) for suffix in ('.zip', '.zip.tmp', '.json', '.upload')]
    upload = item.get('upload') or (item.get('result') or {}).get('upload')
    if upload:
        paths.append(job_file(upload, '.upload'))
    return set(paths)


def _bytes(item):
    return sum(path.stat().st_size for path in _artifacts(item) if path.is_file())


def _remove(jid):
    item = _jobs[jid]
    for path in _artifacts(item):
        path.unlink(missing_ok=True)
    job_file(jid, '.job').unlink(missing_ok=True)
    _jobs.pop(jid)


def prune(required=0):
    """Caller holds the job lock. Active jobs and their input previews are pinned."""
    pinned = {dependency for j in _jobs.values() if j['state'] in ACTIVE for dependency in j.get('dependencies', [])}
    terminal = sorted((j for j in _jobs.values() if j['state'] not in ACTIVE and j['id'] not in pinned),
                      key=lambda j: j.get('finished_at', j['created_at']))
    for item in list(terminal):
        if time.time() - item.get('finished_at', item['created_at']) >= RETENTION or len(_jobs) > MAX_RECORDS:
            _remove(item['id'])
            terminal.remove(item)
    def used():
        return sum(max(_bytes(j), j.get('reserved_bytes', 0)) if j['state'] in ACTIVE else _bytes(j)
                   for j in _jobs.values())
    total = used()
    while terminal and total + required > MAX_ARTIFACT_BYTES:
        item = terminal.pop(0)
        total -= _bytes(item)
        _remove(item['id'])
    if total + required > MAX_ARTIFACT_BYTES:
        raise JobCapacityError('Temporary job storage is full; retry after active jobs finish')


def reconcile():
    """Recover terminal jobs, fail interrupted work, remove unowned artifacts."""
    with _job_lock:
        folder = Path(db.data_dir())/'jobs'
        folder.mkdir(exist_ok=True)
        _jobs.clear()
        for path in folder.glob('*.job'):
            try:
                item = json.loads(path.read_text(encoding='utf-8'))
                if item['id'] != path.stem or not re.fullmatch('[0-9a-f]{32}', item['id']):
                    raise ValueError('Invalid job record')
                _jobs[item['id']] = item
                if item['state'] in ACTIVE:
                    item.update(state='failed', error='Interrupted by restart; operation was not replayed',
                                finished_at=time.time(), reserved_bytes=0)
                    for artifact in _artifacts(item):
                        artifact.unlink(missing_ok=True)
                    _persist(item)
            except (ValueError, KeyError, TypeError):
                path.unlink(missing_ok=True)
        owned = {path for item in _jobs.values() for path in _artifacts(item)}
        owned.update(job_file(jid, '.job') for jid in _jobs)
        for path in folder.iterdir():
            if path.is_file() and path not in owned and re.fullmatch(r'[0-9a-f]{32}\.(?:zip|json|upload|job)(?:\.tmp)?', path.name):
                path.unlink()
        prune()


def reserve(kind, upload=None, dependencies=()):
    with _job_lock:
        if sum(j['state'] in ACTIVE for j in _jobs.values()) >= MAX_ACTIVE:
            raise JobCapacityError('Job queue is full; retry after an active job finishes')
        if any(dep not in _jobs for dep in dependencies):
            raise KeyError('Input preview has expired')
        size = library.settings()['archive_max_mb'] * 1024**2 if kind in ('export', 'import_preview') else 0
        if kind in ('import', 'import_preview'):
            size += 256 * 1024**2  # bounded full conflict report
        # Pin dependencies before reclaiming terminal artifacts.
        jid = library.uid()
        item = {'id': jid, 'kind': kind, 'state': 'uploading' if upload else 'queued',
                'created_at': time.time(), 'result': None, 'error': None,
                'reserved_bytes': 0, 'dependencies': list(dependencies)}
        if upload:
            item['upload'] = upload
        _jobs[jid] = item
        try:
            prune(size)
            item['reserved_bytes'] = size
            _persist(item)
        except BaseException:
            _jobs.pop(jid, None)
            raise
        return jid


def fail(jid, error):
    with _job_lock:
        item = _jobs[jid]
        for path in _artifacts(item):
            path.unlink(missing_ok=True)
        item.update(state='failed', error=str(error), finished_at=time.time(), reserved_bytes=0)
        _persist(item)


def compact_report(report, path):
    """Write the full report, return only a bounded summary for memory and HTTP."""
    temporary = str(path) + '.tmp'
    try:
        size = 0
        with open(temporary, 'wb') as output:
            for text in json.JSONEncoder(ensure_ascii=False).iterencode(report):
                chunk = text.encode('utf-8')
                size += len(chunk)
                if size > 256 * 1024**2:
                    raise ValueError('Import conflict report exceeds 256 MiB')
                output.write(chunk)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return report_summary(report)


def report_summary(report):
    conflicts = report.get('conflicts', [])
    # Bound pathological single-field strings as well as the number of conflicts.
    shown = [{key: value[:4096] if isinstance(value, str) else value for key, value in conflict.items()}
             for conflict in conflicts[:100]]
    return dict(report, conflicts=shown, conflict_count=len(conflicts),
                conflicts_truncated=len(conflicts) > 100 or shown != conflicts[:100])


def submit(kind, action, jid=None, dependencies=()):
    global _executor
    jid = jid or reserve(kind, dependencies=dependencies)
    with _job_lock:
        if _executor is None:
            _executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix='prompt-library-jobs')
        _jobs[jid]['state'] = 'queued'
        _persist(_jobs[jid])
        def run():
            try:
                with _job_lock:
                    _jobs[jid]['state'] = 'running'
                    _persist(_jobs[jid])
                result = action(jid)
                with _job_lock:
                    if _bytes(_jobs[jid]) > _jobs[jid]['reserved_bytes'] and kind not in ('backup', 'scheduled_backup', 'restore'):
                        raise JobCapacityError('Job exceeded its reserved temporary storage')
                    _jobs[jid].update(state='complete', result=result, finished_at=time.time(), reserved_bytes=0)
                    _persist(_jobs[jid])
                    prune()
            except Exception as exc:
                fail(jid, exc)
        try:
            _executor.submit(run)
        except Exception as exc:
            fail(jid, exc)
            raise
        return jid


def job(jid):
    with _job_lock:
        if jid not in _jobs:
            raise KeyError(jid)
        return dict(_jobs[jid])


def job_file(jid, suffix):
    if len(jid) != 32 or any(c not in '0123456789abcdef' for c in jid):
        raise ValueError('Invalid job identifier')
    folder = Path(db.data_dir())/'jobs'
    folder.mkdir(exist_ok=True)
    return folder/(jid+suffix)


def status():
    from . import archives, scanner
    with _job_lock:
        jobs = [dict(j) for j in _jobs.values()][-20:]
    return dict(_service_state, scan=scanner.get_status(), jobs=jobs, backups=archives.list_backups())


def tick(now, schedule):
    from . import archives, scanner
    prefs = library.settings()
    if now >= schedule.get('scan',0):
        if prefs['capture_enabled']:
            scanner.start_scan(background=True, include_hidden=prefs['include_hidden'], follow_symlinks=prefs['follow_symlinks'])
        schedule['scan'] = now + prefs['scan_interval']
    if prefs['backup_enabled'] and now >= schedule.get('backup',0):
        backups = [b for b in archives.list_backups() if b['scheduled']]
        due = (backups[0]['created_at'] if backups else 0) + prefs['backup_interval_hours']*3600
        if now >= due:
            with _job_lock:
                active = any(j['kind']=='scheduled_backup' and j['state'] in ('queued','running') for j in _jobs.values())
            if not active:
                submit('scheduled_backup',lambda jid: archives.create_backup(True))
            # One attempt per interval, including unchanged libraries. Retry failures in 60 seconds.
            schedule['backup'] = now + prefs['backup_interval_hours']*3600
        else:
            schedule['backup'] = due
    _service_state.update(next_scan=schedule.get('scan') if prefs['capture_enabled'] else None,
                          next_backup=schedule.get('backup') if prefs['backup_enabled'] else None)


def start():
    global _thread
    if _thread and _thread.is_alive():
        return
    from . import scanner
    reconcile()
    scanner._stopping = False
    _stop.clear()
    def run():
        schedule = {}
        _service_state['running'] = True
        previous_prefs = None
        while not _stop.is_set():
            try:
                prefs = library.settings()
                if prefs != previous_prefs:
                    schedule.clear()
                    previous_prefs = prefs
                tick(time.time(),schedule)
                _service_state['last_error'] = None
                with _job_lock:
                    failed = [j for j in _jobs.values() if j['kind']=='scheduled_backup' and j['state']=='failed' and not j.get('reported')]
                    for j in failed:
                        j['reported'] = True
                        _service_state['last_error'] = j['error']
                        schedule['backup'] = time.time()+60
                    prune()
            except Exception as exc:
                _service_state['last_error'] = str(exc)
            _stop.wait(1)
        _service_state['running'] = False
    _thread = threading.Thread(target=run, name='prompt-library-service',daemon=True)
    _thread.start()


def stop():
    global _executor
    from . import scanner
    _stop.set()
    if _thread:
        _thread.join(timeout=5)
    scanner.stop()
    with _job_lock:
        executor, _executor = _executor, None
    if executor:
        executor.shutdown(wait=True, cancel_futures=True)
    with _job_lock:
        for item in _jobs.values():
            if item['state'] == 'queued':
                fail(item['id'], 'Cancelled during shutdown')
    if _thread:
        _thread.join()


async def startup(app):
    import asyncio
    await asyncio.to_thread(start)


async def cleanup(app):
    import asyncio
    await asyncio.to_thread(stop)
