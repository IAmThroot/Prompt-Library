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

from . import db, library, validation

FORMAT = 'comfy-prompt-library'
VERSION = 1


def directory(name):
    path = Path(db.data_dir()) / name
    path.mkdir(exist_ok=True)
    return path


def _rows(conn, table):
    return [dict(r) for r in conn.execute('SELECT * FROM ' + table)]


def payload(conn, ids=None, complete=False):
    conn.execute('CREATE TEMP TABLE IF NOT EXISTS export_ids(id INTEGER PRIMARY KEY)')
    conn.execute('DELETE FROM export_ids')
    if ids is not None and not complete:
        conn.executemany('INSERT OR IGNORE INTO export_ids VALUES (?)', ((i,) for i in ids))
    else:
        conn.execute('INSERT INTO export_ids SELECT id FROM prompts' + ('' if complete else ' WHERE deleted=0'))
    def selected(table, column='prompt_id'):
        return [dict(r) for r in conn.execute(f'SELECT * FROM {table} WHERE {column} IN (SELECT id FROM export_ids)')]
    prompts = selected('prompts', 'id')
    pidmap = {r['id']: r['uuid'] for r in prompts}
    generations = selected('generations')
    pws = selected('prompt_workflows')
    cps = selected('collection_prompts')
    details = selected('prompt_details')
    asset_query = 'SELECT asset_id FROM generations WHERE prompt_id IN (SELECT id FROM export_ids)'
    aws = [dict(r) for r in conn.execute('SELECT * FROM asset_workflows WHERE asset_id IN (' + asset_query + ')')]
    assets = [dict(r) for r in conn.execute('SELECT * FROM assets WHERE id IN (' + asset_query + ')')]
    workflow_query = ('SELECT workflow_id FROM prompt_workflows WHERE prompt_id IN (SELECT id FROM export_ids) '
                      'UNION SELECT workflow_id FROM asset_workflows WHERE asset_id IN (' + asset_query + ')')
    workflows = [dict(r) for r in conn.execute('SELECT * FROM workflows WHERE id IN (' + workflow_query + ')')]
    collections = [dict(r) for r in conn.execute('SELECT * FROM collections' + ('' if complete else
        ' WHERE id IN (SELECT collection_id FROM collection_prompts WHERE prompt_id IN (SELECT id FROM export_ids))'))]
    for rows in (generations, pws, cps, details):
        for r in rows:
            r['prompt_id'] = pidmap[r['prompt_id']]
    for r in prompts:
        r.pop('id')
    for r in generations:
        r.pop('id')
    return {'format': FORMAT, 'version': VERSION, 'complete': complete, 'prompts': prompts,
            'generations': generations, 'assets': assets,
            'workflows': workflows,
            'prompt_workflows': pws, 'asset_workflows': aws, 'details': details,
            'collections': collections,
            'memberships': cps, 'settings': _rows(conn, 'preferences') if complete else [],
            'file_index': _rows(conn,'files') if complete else [],
            'aliases': _rows(conn,'archive_aliases') if complete else [], 'previews': {}, '_pidmap': pidmap}


MANIFEST_LIMIT = 64 * 1024**2
PREVIEW_LIMIT = 2 * 1024**2
RECOVERY_FORMAT = 'comfy-prompt-library-recovery'


def file_digest(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def bounded_read(archive, name, limit):
    if archive.getinfo(name).file_size > limit:
        raise ValueError('Archive member exceeds limit: ' + name)
    with archive.open(name) as source:
        content = source.read(limit + 1)
    if len(content) > limit:
        raise ValueError('Archive member exceeds limit: ' + name)
    return content


def snapshot_database(path):
    with db._lock, closing(sqlite3.connect(db._db_path)) as source, closing(sqlite3.connect(path)) as target:
        source.backup(target)


def preview_sources(pidmap, assets):
    members = {}
    for pid, portable in pidmap.items():
        source = db.thumb_path(pid)
        if db.valid_thumb(source):
            members['thumbs/' + portable + '.jpg'] = source
    for asset in assets:
        source = library.asset_thumb(asset['id'])
        if db.valid_thumb(source):
            members['previews/' + asset['id'] + '.jpg'] = source
    return members


class LimitedArchiveFile:
    """Enforce the compressed output limit while ZIP writes, including its directory."""
    def __init__(self, file, limit):
        self.file, self.limit = file, limit

    def write(self, content):
        if self.file.tell() + len(content) > self.limit:
            raise ValueError('Portable archive exceeds upload limit; export a smaller selection')
        return self.file.write(content)

    def __getattr__(self, key):
        return getattr(self.file, key)


def _write_archive(path, data, sqlite_path=None):
    members = preview_sources(data.pop('_pidmap'), data['assets'])
    data['previews'] = {name: file_digest(source) for name, source in members.items()}
    hasher = hashlib.sha256()
    for chunk in json.JSONEncoder(ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(',', ':')).iterencode(data):
        hasher.update(chunk.encode('utf-8'))
    digest = hasher.hexdigest()
    if sqlite_path:
        data['database_sha256'] = file_digest(sqlite_path)
    limits = library.settings()
    tmp = str(path) + '.tmp'
    try:
        with open(tmp, 'w+b') as raw, zipfile.ZipFile(LimitedArchiveFile(raw, limits['archive_max_mb'] * 1024**2), 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            # iterencode avoids a second complete manifest string and byte buffer.
            size = 0
            with archive.open('library.json', 'w', force_zip64=True) as output:
                for chunk in json.JSONEncoder(ensure_ascii=False, allow_nan=False, sort_keys=True).iterencode(data):
                    content = chunk.encode('utf-8')
                    size += len(content)
                    if size > MANIFEST_LIMIT:
                        raise ValueError('Portable manifest exceeds 64 MiB; export a smaller selection')
                    output.write(content)
            expanded = size
            if expanded > limits['archive_expanded_max_mb'] * 1024**2:
                raise ValueError('Expanded archive exceeds limit')
            for name, source in members.items():
                length = os.path.getsize(source)
                if length > PREVIEW_LIMIT:
                    raise ValueError('Preview exceeds 2 MiB')
                expanded += length
                if expanded > limits['archive_expanded_max_mb'] * 1024**2:
                    raise ValueError('Expanded archive exceeds limit')
                archive.write(source, name)
            if sqlite_path:
                archive.write(sqlite_path, 'database.sqlite')
        if os.path.getsize(tmp) > limits['archive_max_mb'] * 1024**2:
            raise ValueError('Portable archive exceeds upload limit; export a smaller selection')
        validate(tmp)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return digest


def export_archive(path, selection=None):
    from . import service
    with service.maintenance, tempfile.TemporaryDirectory(dir=db.data_dir()) as temp:
        snapshot = Path(temp)/'snapshot.db'
        # Resolve selection and snapshot atomically, then serialize without the live DB lock.
        with db._lock:
            ids = library.selected_ids(selection or {})
            snapshot_database(snapshot)
        with closing(sqlite3.connect(snapshot)) as conn:
            conn.row_factory = sqlite3.Row
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
        if 'recovery.json' in archive.namelist():
            raise ValueError('Recovery backups must be restored through the backup interface; use portable exports for importing')
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
        if 'library.json' not in names or archive.getinfo('library.json').file_size > MANIFEST_LIMIT:
            raise ValueError('Missing or oversized library manifest')
        data = json.loads(bounded_read(archive, 'library.json', MANIFEST_LIMIT))
        validation.object_value(data, 'archive manifest')
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
            validation.extras(json.loads(r['extra']))
            validation.metadata(json.loads(r['meta']))
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
            validation.generation(r)
            validation.snapshot(json.loads(r['snapshot']))
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
            validation.details(json.loads(r['details']))
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
            content = bounded_read(archive, name, PREVIEW_LIMIT)
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
    return _preview_data(validate(path))


def _preview_data(data):
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
        report = _preview_data(data)
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
                    stage.write_bytes(bounded_read(archive, name, PREVIEW_LIMIT))
                    os.replace(stage,destination)
                    published.append(destination)
                if report_path:
                    stage = Path(staging)/'report.json'
                    service.compact_report(report, stage)
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


def _recovery_snapshot(sourcepath, archive, data):
    """Validate local recovery independently of portable upload limits."""
    with archive.open('database.sqlite') as source, open(sourcepath, 'wb') as target:
        shutil.copyfileobj(source, target, 1024 * 1024)
    if file_digest(sourcepath) != data.get('database_sha256'):
        raise ValueError('Backup database checksum mismatch')
    with closing(sqlite3.connect(sourcepath)) as conn:
        conn.row_factory = sqlite3.Row
        if conn.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or conn.execute('PRAGMA user_version').fetchone()[0] != 3:
            raise ValueError('Invalid or unsupported backup database')
        required = {'prompts', 'files', 'generations', 'prompt_details', 'workflows', 'assets', 'preferences',
                    'prompt_workflows', 'asset_workflows', 'collections', 'collection_prompts', 'archive_aliases', 'backups'}
        present = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not required <= present:
            raise ValueError('Incomplete backup database')
        # Verify columns consumed by restore/migration before replacing the live database.
        for table, columns in {'prompts': 'id,uuid,hash,positive,negative,extra,meta',
                               'assets': 'id,root,path,mtime,size,trusted',
                               'generations': 'id,uuid,prompt_id,asset_id,snapshot',
                               'preferences': 'key,value', 'backups': 'id,filename,created_at,scheduled,digest'}.items():
            conn.execute(f'SELECT {columns} FROM {table} LIMIT 0')
        prompt_ids = {r[0] for r in conn.execute('SELECT uuid FROM prompts')}
        asset_ids = {r[0] for r in conn.execute('SELECT id FROM assets')}
    previews = data.get('previews')
    if not isinstance(previews, dict):
        raise ValueError('Invalid preview manifest')
    from PIL import Image
    for name, checksum in previews.items():
        parts = PurePosixPath(name).parts
        if len(parts) != 2 or parts[0] not in ('thumbs', 'previews') or not parts[1].endswith('.jpg'):
            raise ValueError('Invalid backup preview path')
        identifier = parts[1][:-4]
        if not _identifier(identifier) or identifier not in (prompt_ids if parts[0] == 'thumbs' else asset_ids):
            raise ValueError('Preview has no owning record')
        content = bounded_read(archive, name, PREVIEW_LIMIT)
        if hashlib.sha256(content).hexdigest() != checksum:
            raise ValueError('Invalid preview checksum')
        with Image.open(io.BytesIO(content)) as image:
            if image.format != 'JPEG' or max(image.size) > 1024:
                raise ValueError('Invalid preview image')
            image.load()


def validate_recovery(path, sourcepath):
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ValueError('Duplicate recovery members')
        modern = 'recovery.json' in names
        manifest = 'recovery.json' if modern else 'library.json'
        # Legacy backups may have manifests larger than the portable import limit.
        with archive.open(manifest) as source:
            data = json.load(source)
        validation.object_value(data, 'recovery manifest')
        expected = (RECOVERY_FORMAT, 2) if modern else (FORMAT, VERSION)
        if (data.get('format'), data.get('version')) != expected or (not modern and not data.get('complete')):
            raise ValueError('Only complete local recovery backups can be restored')
        if set(names) != {manifest, 'database.sqlite'} | set(data.get('previews', {})):
            raise ValueError('Unexpected recovery members')
        _recovery_snapshot(sourcepath, archive, data)
        return data


def create_backup(scheduled=False):
    from . import service
    with service.maintenance, tempfile.TemporaryDirectory(dir=db.data_dir()) as temp:
        snapshot = Path(temp)/'library.db'
        snapshot_database(snapshot)
        with closing(sqlite3.connect(snapshot)) as conn:
            conn.row_factory = sqlite3.Row
            pidmap = {r['id']: r['uuid'] for r in conn.execute('SELECT id,uuid FROM prompts')}
            members = preview_sources(pidmap, conn.execute('SELECT id FROM assets'))
            # Logical content excludes the catalogue, whose changes aren't library changes.
            logical = hashlib.sha256()
            tables = sorted(r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT IN ('backups','sqlite_sequence')"))
            for table in tables:
                logical.update(table.encode())
                for row in conn.execute('SELECT * FROM "' + table.replace('"', '""') + '" ORDER BY rowid'):
                    logical.update(library.canonical(dict(row)).encode())
        previews = {name: file_digest(source) for name, source in members.items()}
        logical.update(library.canonical(previews).encode())
        digest = logical.hexdigest()
        previous = next((r for r in list_backups() if r['scheduled']), None)
        if scheduled and previous and previous['digest'] == digest:
            return {'unchanged': True}
        data = {'format': RECOVERY_FORMAT, 'version': 2, 'database_sha256': file_digest(snapshot), 'previews': previews}
        bid = library.uid()
        path = directory('backups')/(bid+'.zip')
        stage = Path(temp)/'backup.zip'
        with zipfile.ZipFile(stage, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('recovery.json', library.canonical(data))
            archive.write(snapshot, 'database.sqlite')
            for name, source in members.items():
                archive.write(source, name)
        validate_recovery(stage, Path(temp)/'verify.db')
        os.replace(stage, path)
        try:
            with db.db() as conn:
                conn.execute('INSERT INTO backups VALUES (?,?,?,?,?)', (bid,path.name,time.time(),int(scheduled),digest))
        except Exception:
            path.unlink(missing_ok=True)
            raise
        if scheduled:
            keep = library.settings()['backup_keep']
            for row in [r for r in list_backups() if r['scheduled']][keep:]:
                (directory('backups')/os.path.basename(row['filename'])).unlink(missing_ok=True)
                with db.db() as conn:
                    conn.execute('DELETE FROM backups WHERE id=?', (row['id'],))
        return {'id': bid, 'filename': path.name}


def restore_backup(bid):
    from . import service
    path = backup_path(bid)
    with service.maintenance, tempfile.TemporaryDirectory(dir=db.data_dir()) as temp:
        sourcepath = Path(temp)/'restore.db'
        data = validate_recovery(path, sourcepath)
        safety = create_backup(False)
        safety_path = backup_path(safety['id'])
        with zipfile.ZipFile(path) as archive, db._lock:
            catalog = list_backups()
            preview_backup = Path(temp)/'old-previews'
            preview_backup.mkdir()
            for folder in ('thumbs','previews'):
                source = Path(db.data_dir())/folder
                if source.exists():
                    shutil.copytree(source,preview_backup/folder)
            try:
                db.invalidate_thumbs()
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
                    Path(destination).write_bytes(bounded_read(archive, name, PREVIEW_LIMIT))
            except Exception:
                with zipfile.ZipFile(safety_path) as recovery:
                    recovery_db = Path(temp)/'recovery.db'
                    with recovery.open('database.sqlite') as source, open(recovery_db, 'wb') as target:
                        shutil.copyfileobj(source, target, 1024 * 1024)
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
                db.invalidate_thumbs()
                raise
    db.invalidate_thumbs()
    return {'restored': bid, 'safety_backup': safety['id']}
