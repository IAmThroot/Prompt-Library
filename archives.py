"""Versioned portable archives and consistent, restorable SQLite backups."""
import hashlib
import io
import json
import math
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import tempfile
import time
import zipfile
from contextlib import closing

from . import db, library

FORMAT = 'comfy-prompt-library'
VERSION = 1


def directory(name):
    path = Path(db.data_dir()) / name
    path.mkdir(exist_ok=True)
    return path


def _rows(conn, table):
    return [dict(r) for r in conn.execute('SELECT * FROM ' + table)]


def payload(conn, ids=None, complete=False):
    selected = set(ids) if ids is not None else {r[0] for r in conn.execute('SELECT id FROM prompts WHERE deleted=0')}
    if complete:
        selected = {r[0] for r in conn.execute('SELECT id FROM prompts')}
    prompts = [r for r in _rows(conn, 'prompts') if r['id'] in selected]
    pidmap = {r['id']: r['uuid'] for r in prompts}
    generations = [r for r in _rows(conn, 'generations') if r['prompt_id'] in selected]
    aids = {r['asset_id'] for r in generations if r['asset_id']}
    pws = [r for r in _rows(conn, 'prompt_workflows') if r['prompt_id'] in selected]
    aws = [r for r in _rows(conn, 'asset_workflows') if r['asset_id'] in aids]
    wids = {r['workflow_id'] for r in pws + aws}
    cps = [r for r in _rows(conn, 'collection_prompts') if r['prompt_id'] in selected]
    cids = {r['collection_id'] for r in cps}
    details = [r for r in _rows(conn, 'prompt_details') if r['prompt_id'] in selected]
    for rows in (generations, pws, cps, details):
        for r in rows:
            r['prompt_id'] = pidmap[r['prompt_id']]
    for r in prompts:
        r.pop('id')
    for r in generations:
        r.pop('id')
    return {'format': FORMAT, 'version': VERSION, 'complete': complete, 'prompts': prompts,
            'generations': generations, 'assets': [r for r in _rows(conn, 'assets') if r['id'] in aids],
            'workflows': [r for r in _rows(conn, 'workflows') if r['id'] in wids],
            'prompt_workflows': pws, 'asset_workflows': aws, 'details': details,
            'collections': [r for r in _rows(conn, 'collections') if complete or r['id'] in cids],
            'memberships': cps, 'settings': _rows(conn, 'preferences') if complete else [],
            'file_index': _rows(conn,'files') if complete else [],
            'aliases': _rows(conn,'archive_aliases') if complete else [], 'previews': {}, '_pidmap': pidmap}


def _write_archive(path, data, sqlite_path=None):
    pidmap = data.pop('_pidmap')
    members = {}
    for pid, portable in pidmap.items():
        source = db.thumb_path(pid)
        if db.valid_thumb(source):
            members['thumbs/' + portable + '.jpg'] = Path(source).read_bytes()
    for asset in data['assets']:
        source = library.asset_thumb(asset['id'])
        if db.valid_thumb(source):
            members['previews/' + asset['id'] + '.jpg'] = Path(source).read_bytes()
    data['previews'] = {name: hashlib.sha256(content).hexdigest() for name, content in members.items()}
    digest = hashlib.sha256(library.canonical(data).encode()).hexdigest()
    if sqlite_path:
        data['database_sha256'] = hashlib.sha256(Path(sqlite_path).read_bytes()).hexdigest()
    tmp = str(path) + '.tmp'
    try:
        with zipfile.ZipFile(tmp, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('library.json', library.canonical(data))
            for name, content in members.items():
                archive.writestr(name, content)
            if sqlite_path:
                archive.write(sqlite_path, 'database.sqlite')
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return digest


def export_archive(path, selection=None):
    from . import service
    with service.maintenance, db._lock:
        ids = library.selected_ids(selection or {})
        with db.db() as conn:
            data = payload(conn, ids)
        _write_archive(path, data)
    return {'path': str(path), 'count': len(ids)}


def _identifier(value):
    return isinstance(value, str) and len(value) == 32 and all(c in '0123456789abcdef' for c in value)


def validate(path):
    limits = library.settings()
    if os.path.getsize(path) > limits['archive_max_mb'] * 1024 ** 2:
        raise ValueError('Archive exceeds upload limit')
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        names = [i.filename for i in infos]
        if len(names) != len(set(names)) or len(names) > 100000:
            raise ValueError('Duplicate or excessive archive members')
        if sum(i.file_size for i in infos) > limits['archive_expanded_max_mb'] * 1024 ** 2:
            raise ValueError('Expanded archive exceeds limit')
        for entry in infos:
            name = entry.filename
            parts = PurePosixPath(name).parts
            if name.startswith('/') or '\\' in name or '..' in parts or (entry.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError('Unsafe archive path')
            allowed = name in ('library.json', 'database.sqlite') or (len(parts) == 2 and parts[0] in ('thumbs', 'previews') and parts[1].endswith('.jpg') and _identifier(parts[1][:-4]))
            if not allowed:
                raise ValueError('Unexpected archive member: ' + name)
        if 'library.json' not in names or archive.getinfo('library.json').file_size > 64 * 1024 ** 2:
            raise ValueError('Missing or oversized library manifest')
        data = json.loads(archive.read('library.json'))
        if data.get('format') != FORMAT or data.get('version') != VERSION:
            raise ValueError('Unsupported library archive version')
        tables = ('prompts','generations','assets','workflows','prompt_workflows','asset_workflows','details','collections','memberships','settings')
        for table in tables:
            if not isinstance(data.get(table), list) or not all(isinstance(r, dict) for r in data[table]):
                raise ValueError('Invalid archive records: ' + table)
        sets = {}
        for table, key in (('prompts','uuid'),('generations','uuid'),('assets','id'),('workflows','id'),('collections','id')):
            identifiers = [r.get(key) for r in data[table]]
            if any(not _identifier(i) for i in identifiers) or len(set(identifiers)) != len(identifiers):
                raise ValueError('Invalid or duplicate identifiers: ' + table)
            sets[table] = set(identifiers)
        for r in data['prompts']:
            for key in ('positive','negative','title','tags','notes','hash','extra','meta','origin','kind'):
                if not isinstance(r.get(key), str):
                    raise ValueError('Invalid prompt field: ' + key)
            if r['kind'] not in ('prompt','workflow') or any(type(r.get(key)) is not int or r[key] not in (0,1) for key in ('favorite','deleted')):
                raise ValueError('Invalid prompt kind or flags')
            if any(type(r.get(key)) not in (int,float) or not math.isfinite(r[key]) for key in ('created_at','updated_at','last_seen')):
                raise ValueError('Invalid prompt timestamp')
            if not isinstance(json.loads(r['extra']), list) or not isinstance(json.loads(r['meta']), dict):
                raise ValueError('Invalid prompt metadata')
            from .extract import prompt_hash
            if r['kind']=='prompt' and r['hash'] not in (prompt_hash(r['positive'],r['negative']),prompt_hash(r['positive'],r['negative'],json.loads(r['extra']))):
                raise ValueError('Prompt digest mismatch')
        for r in data['assets']:
            if not isinstance(r.get('root'), str) or not isinstance(r.get('path'), str):
                raise ValueError('Invalid source reference')
            if r.get('media_type') not in ('image','video','audio'):
                raise ValueError('Invalid media kind')
            if type(r.get('mtime')) not in (int,float) or not math.isfinite(r['mtime']) or type(r.get('size')) is not int or r['size'] < 0:
                raise ValueError('Invalid source fingerprint')
        for r in data['generations']:
            if r.get('prompt_id') not in sets['prompts'] or r.get('asset_id') not in sets['assets']:
                raise ValueError('Broken generation reference')
            if not isinstance(json.loads(r['snapshot']), dict):
                raise ValueError('Invalid generation snapshot')
        for table, left, target, right in (('prompt_workflows','prompt_id','prompts','workflow_id'),('asset_workflows','asset_id','assets','workflow_id'),('memberships','prompt_id','prompts','collection_id')):
            rightset = sets['collections'] if table == 'memberships' else sets['workflows']
            for r in data[table]:
                if r.get(left) not in sets[target] or r.get(right) not in rightset:
                    raise ValueError('Broken archive reference')
        for r in data['collections']:
            if not isinstance(r.get('name'),str) or not 1 <= len(r['name'].strip()) <= 120:
                raise ValueError('Invalid collection name')
        for r in data['details']:
            if r.get('prompt_id') not in sets['prompts'] or not isinstance(json.loads(r['details']), dict):
                raise ValueError('Invalid prompt details')
        for r in data['workflows']:
            editor = json.loads(r['editor']) if r.get('editor') else None
            api = json.loads(r['api']) if r.get('api') else None
            if editor is not None and (not isinstance(editor, dict) or not isinstance(editor.get('nodes'), list)):
                raise ValueError('Invalid editor workflow')
            if api is not None and not isinstance(api, dict):
                raise ValueError('Invalid API graph')
            if editor is None and api is None:
                raise ValueError('Empty workflow snapshot')
            if hashlib.sha256(library.canonical([editor,api]).encode()).hexdigest() != r.get('digest'):
                raise ValueError('Workflow digest mismatch')
        previews = data.get('previews')
        if not isinstance(previews, dict):
            raise ValueError('Invalid preview manifest')
        if set(names) - {'library.json','database.sqlite'} != set(previews):
            raise ValueError('Preview manifest mismatch')
        from PIL import Image
        for name, digest in previews.items():
            kind, filename = name.split('/')
            if filename[:-4] not in sets['prompts' if kind == 'thumbs' else 'assets']:
                raise ValueError('Preview has no owning record')
            content = archive.read(name)
            if len(content) > 2 * 1024 ** 2 or hashlib.sha256(content).hexdigest() != digest:
                raise ValueError('Invalid preview checksum or size')
            with Image.open(io.BytesIO(content)) as image:
                if image.format != 'JPEG' or max(image.size) > 1024:
                    raise ValueError('Invalid preview image')
                image.load()
    return data


def _prompt_match(conn, item):
    row = conn.execute('SELECT * FROM prompts WHERE uuid=?', (item['uuid'],)).fetchone()
    if not row:
        alias = conn.execute("SELECT local_id FROM archive_aliases WHERE kind='prompt' AND external_id=?", (item['uuid'],)).fetchone()
        row = conn.execute('SELECT * FROM prompts WHERE id=?', (alias[0],)).fetchone() if alias else None
    if not row:
        row = conn.execute('SELECT * FROM prompts WHERE hash=?', (item['hash'],)).fetchone()
    return row


def preview_import(path):
    data = validate(path)
    report = {'new': 0, 'duplicates': 0, 'conflicts': [], 'workflows': len(data['workflows']), 'generations': len(data['generations'])}
    with db.db() as conn:
        for item in data['prompts']:
            row = _prompt_match(conn,item)
            report['duplicates' if row else 'new'] += 1
            if row:
                for key in ('title','notes','favorite','deleted','positive','negative'):
                    if row[key] != item[key] and (row[key] or key in ('favorite','deleted')):
                        report['conflicts'].append({'prompt': item['uuid'], 'field': key, 'local': row[key], 'imported': item[key]})
    return report


def import_archive(path, report_path=None):
    from . import service
    data = validate(path)
    with service.maintenance, db._lock, tempfile.TemporaryDirectory(dir=db.data_dir()) as staging:
        report = preview_import(path)
        published = []
        try:
            with db.db() as conn, zipfile.ZipFile(path) as archive:
                pm, am, wm, cm = {}, {}, {}, {}
                for r in data['prompts']:
                    row = _prompt_match(conn,r)
                    if row:
                        pid = row['id']
                        tags = ', '.join(dict.fromkeys(t.strip() for t in (row['tags']+','+r['tags']).split(',') if t.strip()))
                        conn.execute('UPDATE prompts SET title=?,notes=?,tags=? WHERE id=?',
                                     (row['title'] or r['title'],row['notes'] or r['notes'],tags,pid))
                    else:
                        keys = ('hash','title','positive','negative','extra','meta','tags','notes','favorite','deleted','origin','created_at','updated_at','last_seen','uuid','kind')
                        pid = conn.execute('INSERT INTO prompts('+','.join(keys)+') VALUES ('+','.join('?' for k in keys)+')', tuple(r[k] for k in keys)).lastrowid
                    pm[r['uuid']] = pid
                    conn.execute("INSERT OR REPLACE INTO archive_aliases VALUES ('prompt',?,?)", (r['uuid'],str(pid)))
                for r in data['workflows']:
                    wm[r['id']] = library.store_workflow(conn, json.loads(r['editor']) if r['editor'] else None, json.loads(r['api']) if r['api'] else None)
                for r in data['assets']:
                    am[r['id']] = library.asset(conn,r['root'],r['path'],r['mtime'],r['size'],r['media_type'],False)
                for r in data['collections']:
                    row = conn.execute('SELECT id FROM collections WHERE id=? OR name=? COLLATE NOCASE', (r['id'],r['name'])).fetchone()
                    cid = row['id'] if row else r['id']
                    if not row:
                        conn.execute('INSERT INTO collections VALUES (?,?)', (cid,r['name']))
                    cm[r['id']] = cid
                for r in data['generations']:
                    # Numeric IDs are local; portable UUIDs and source fingerprint identify imported history.
                    conn.execute('''INSERT OR IGNORE INTO generations(prompt_id,root,path,mtime,size,extractor,snapshot,created_at,asset_id,uuid)
                        VALUES (?,?,?,?,?,?,?,?,?,?)''', (pm[r['prompt_id']],r['root'],r['path'],r['mtime'],r['size'],r['extractor'],r['snapshot'],r['created_at'],am[r['asset_id']],r['uuid']))
                for r in data['prompt_workflows']:
                    library.link_workflow(conn,pm[r['prompt_id']],wm[r['workflow_id']])
                for r in data['asset_workflows']:
                    conn.execute('INSERT OR IGNORE INTO asset_workflows VALUES (?,?)',(am[r['asset_id']],wm[r['workflow_id']]))
                for r in data['memberships']:
                    conn.execute('INSERT OR IGNORE INTO collection_prompts VALUES (?,?)',(cm[r['collection_id']],pm[r['prompt_id']]))
                for r in data['details']:
                    conn.execute('INSERT OR IGNORE INTO prompt_details VALUES (?,?)',(pm[r['prompt_id']],r['details']))
                for index, name in enumerate(data['previews']):
                    portable = PurePosixPath(name).stem
                    destination = db.thumb_path(pm[portable]) if name.startswith('thumbs/') else library.asset_thumb(am[portable])
                    if os.path.exists(destination):
                        continue
                    stage = Path(staging)/str(index)
                    stage.write_bytes(archive.read(name))
                    os.replace(stage,destination)
                    published.append(destination)
                if report_path:
                    stage = Path(staging)/'report.json'
                    stage.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
                    os.replace(stage,report_path)
                    published.append(report_path)
            return report
        except Exception:
            for name in published:
                if os.path.exists(name):
                    os.unlink(name)
            raise


def list_backups():
    with db.db() as conn:
        return [dict(r) for r in conn.execute('SELECT * FROM backups ORDER BY created_at DESC')]


def backup_path(bid):
    with db.db() as conn:
        row = conn.execute('SELECT filename FROM backups WHERE id=?', (bid,)).fetchone()
    if not row:
        raise KeyError(bid)
    path = directory('backups') / os.path.basename(row['filename'])
    if not path.is_file():
        raise KeyError(bid)
    return path


def create_backup(scheduled=False):
    from . import service
    with service.maintenance, db._lock, tempfile.TemporaryDirectory(dir=db.data_dir()) as temp:
        snapshot = Path(temp)/'library.db'
        with closing(sqlite3.connect(db._db_path)) as source, closing(sqlite3.connect(snapshot)) as target:
            source.backup(target)
        with closing(sqlite3.connect(snapshot)) as conn:
            conn.row_factory = sqlite3.Row
            data = payload(conn,complete=True)
        bid = library.uid()
        path = directory('backups') / (bid+'.zip')
        digest = _write_archive(path,data,snapshot)
        previous = next((r for r in list_backups() if r['scheduled']),None)
        if scheduled and previous and previous['digest'] == digest:
            path.unlink()
            return {'unchanged': True}
        with db.db() as conn:
            conn.execute('INSERT INTO backups VALUES (?,?,?,?,?)',(bid,path.name,time.time(),int(scheduled),digest))
        if scheduled:
            keep = library.settings()['backup_keep']
            stale = [r for r in list_backups() if r['scheduled']][keep:]
            for row in stale:
                file = directory('backups') / os.path.basename(row['filename'])
                file.unlink(missing_ok=True)
                with db.db() as conn:
                    conn.execute('DELETE FROM backups WHERE id=?',(row['id'],))
        return {'id': bid, 'filename': path.name}


def restore_backup(bid):
    from . import service
    path = backup_path(bid)
    data = validate(path)
    if not data.get('complete'):
        raise ValueError('Only complete local backups can be restored')
    with service.maintenance, db._lock, tempfile.TemporaryDirectory(dir=db.data_dir()) as temp:
        with zipfile.ZipFile(path) as archive:
            sourcepath = Path(temp)/'restore.db'
            sourcepath.write_bytes(archive.read('database.sqlite'))
            with closing(sqlite3.connect(sourcepath)) as source:
                if source.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or source.execute('PRAGMA user_version').fetchone()[0] != 3:
                    raise ValueError('Invalid or unsupported backup database')
                if source.execute("SELECT COUNT(*) FROM sqlite_master WHERE name IN ('prompts','workflows','assets','preferences')").fetchone()[0] != 4:
                    raise ValueError('Incomplete backup database')
            if hashlib.sha256(sourcepath.read_bytes()).hexdigest() != data.get('database_sha256'):
                raise ValueError('Backup database checksum mismatch')
            safety = create_backup(False)
            safety_path = backup_path(safety['id'])
            catalog = list_backups()
            preview_backup = Path(temp)/'old-previews'
            preview_backup.mkdir()
            for folder in ('thumbs','previews'):
                source = Path(db.data_dir())/folder
                if source.exists():
                    shutil.copytree(source,preview_backup/folder)
            try:
                with closing(sqlite3.connect(sourcepath)) as source, closing(sqlite3.connect(db._db_path)) as target:
                    source.backup(target)
                with db.db() as conn:
                    pm = {r['uuid']:r['id'] for r in conn.execute('SELECT id,uuid FROM prompts')}
                    # Preserve the local backup catalogue, including the recovery point just made.
                    conn.execute('DELETE FROM backups')
                    conn.executemany('INSERT INTO backups VALUES (?,?,?,?,?)',[(r['id'],r['filename'],r['created_at'],r['scheduled'],r['digest']) for r in catalog])
                for folder in ('thumbs','previews'):
                    destination = Path(db.data_dir())/folder
                    destination.mkdir(exist_ok=True)
                    for old in destination.glob('*.jpg'):
                        old.unlink()
                for name in data['previews']:
                    identifier = PurePosixPath(name).stem
                    destination = db.thumb_path(pm[identifier]) if name.startswith('thumbs/') else library.asset_thumb(identifier)
                    Path(destination).write_bytes(archive.read(name))
            except Exception:
                with zipfile.ZipFile(safety_path) as recovery:
                    recovery_db = Path(temp)/'recovery.db'
                    recovery_db.write_bytes(recovery.read('database.sqlite'))
                with closing(sqlite3.connect(recovery_db)) as source, closing(sqlite3.connect(db._db_path)) as target:
                    source.backup(target)
                with db.db() as conn:
                    conn.execute('DELETE FROM backups')
                    conn.executemany('INSERT INTO backups VALUES (?,?,?,?,?)',[(r['id'],r['filename'],r['created_at'],r['scheduled'],r['digest']) for r in catalog])
                for folder in ('thumbs','previews'):
                    destination = Path(db.data_dir())/folder
                    if destination.exists():
                        shutil.rmtree(destination)
                    if (preview_backup/folder).exists():
                        shutil.copytree(preview_backup/folder,destination)
                raise
    return {'restored': bid, 'safety_backup': safety['id']}
