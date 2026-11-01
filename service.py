"""Browser-independent capture, backup scheduling and asynchronous archive jobs."""
import concurrent.futures
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


def submit(kind, action):
    global _executor
    with _job_lock:
        if _executor is None:
            _executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix='prompt-library-jobs')
        jid = library.uid()
        _jobs[jid] = {'id': jid, 'kind': kind, 'state': 'queued', 'created_at': time.time(), 'result': None, 'error': None}
        def run():
            with _job_lock:
                _jobs[jid]['state'] = 'running'
            try:
                result = action(jid)
                with _job_lock:
                    _jobs[jid].update(state='complete', result=result, finished_at=time.time())
            except Exception as exc:
                with _job_lock:
                    _jobs[jid].update(state='failed', error=str(exc), finished_at=time.time())
        _executor.submit(run)
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
                    expired = [key for key,j in _jobs.items() if j.get('finished_at', time.time()) < time.time()-86400]
                    for key in expired:
                        for suffix in ('.zip','.json','.upload'):
                            job_file(key,suffix).unlink(missing_ok=True)
                        upload = _jobs[key].get('result',{}).get('upload') if isinstance(_jobs[key].get('result'),dict) else None
                        if upload:
                            job_file(upload,'.upload').unlink(missing_ok=True)
                        _jobs.pop(key,None)
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
                item.update(state='failed',error='Cancelled during shutdown',finished_at=time.time())
    if _thread:
        _thread.join()


async def startup(app):
    start()


async def cleanup(app):
    import asyncio
    await asyncio.to_thread(stop)
