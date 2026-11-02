"""Workflow snapshots, collections, gallery assets and persistent library preferences."""
import hashlib
import json
import os
import time
import uuid

from . import db

DEFAULTS = {'capture_enabled': True, 'scan_interval': 30, 'include_hidden': False,
            'follow_symlinks': False, 'allowed_external_roots': [], 'backup_enabled': True, 'backup_interval_hours': 24,
            'backup_keep': 7, 'archive_max_mb': 512, 'archive_expanded_max_mb': 2048}


def uid():
    return uuid.uuid4().hex


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def migrate(conn):
    columns = {r['name'] for r in conn.execute('PRAGMA table_info(prompts)')}
    if 'uuid' not in columns:
        conn.execute('ALTER TABLE prompts ADD COLUMN uuid TEXT')
        conn.execute("ALTER TABLE prompts ADD COLUMN kind TEXT NOT NULL DEFAULT 'prompt'")
        conn.execute('UPDATE prompts SET uuid=lower(hex(randomblob(16)))')
    conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_prompt_uuid ON prompts(uuid)')
    conn.execute('''CREATE TRIGGER IF NOT EXISTS prompt_uuid AFTER INSERT ON prompts WHEN NEW.uuid IS NULL
        BEGIN UPDATE prompts SET uuid=lower(hex(randomblob(16))) WHERE id=NEW.id; END''')
    statements = [
        'CREATE TABLE IF NOT EXISTS preferences(key TEXT PRIMARY KEY,value TEXT NOT NULL)',
        'CREATE TABLE IF NOT EXISTS workflows(id TEXT PRIMARY KEY,digest TEXT UNIQUE NOT NULL,editor TEXT,api TEXT,created_at REAL NOT NULL)',
        'CREATE TABLE IF NOT EXISTS prompt_workflows(prompt_id INTEGER NOT NULL,workflow_id TEXT NOT NULL,PRIMARY KEY(prompt_id,workflow_id))',
        '''CREATE TABLE IF NOT EXISTS assets(id TEXT PRIMARY KEY,root TEXT NOT NULL,path TEXT NOT NULL,
            mtime REAL NOT NULL,size INTEGER NOT NULL,media_type TEXT NOT NULL,trusted INTEGER NOT NULL DEFAULT 0,
            UNIQUE(root,path,mtime,size))''',
        'CREATE TABLE IF NOT EXISTS asset_workflows(asset_id TEXT NOT NULL,workflow_id TEXT NOT NULL,PRIMARY KEY(asset_id,workflow_id))',
        'CREATE TABLE IF NOT EXISTS collections(id TEXT PRIMARY KEY,name TEXT NOT NULL UNIQUE COLLATE NOCASE)',
        'CREATE TABLE IF NOT EXISTS collection_prompts(collection_id TEXT NOT NULL,prompt_id INTEGER NOT NULL,PRIMARY KEY(collection_id,prompt_id))',
        'CREATE TABLE IF NOT EXISTS archive_aliases(kind TEXT NOT NULL,external_id TEXT NOT NULL,local_id TEXT NOT NULL,PRIMARY KEY(kind,external_id))',
        '''CREATE TABLE IF NOT EXISTS backups(id TEXT PRIMARY KEY,filename TEXT NOT NULL,created_at REAL NOT NULL,
            scheduled INTEGER NOT NULL,digest TEXT NOT NULL)''',
    ]
    for statement in statements:
        conn.execute(statement)
    if 'asset_id' not in {r['name'] for r in conn.execute('PRAGMA table_info(generations)')}:
        conn.execute('ALTER TABLE generations ADD COLUMN asset_id TEXT')
    if 'uuid' not in {r['name'] for r in conn.execute('PRAGMA table_info(generations)')}:
        conn.execute('ALTER TABLE generations ADD COLUMN uuid TEXT')
        conn.execute('UPDATE generations SET uuid=lower(hex(randomblob(16)))')
    conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_generation_uuid ON generations(uuid)')
    conn.execute('''CREATE TRIGGER IF NOT EXISTS generation_uuid AFTER INSERT ON generations WHEN NEW.uuid IS NULL
        BEGIN UPDATE generations SET uuid=lower(hex(randomblob(16))) WHERE id=NEW.id; END''')
    for row in conn.execute('SELECT * FROM generations WHERE asset_id IS NULL').fetchall():
        file = conn.execute('SELECT media_type FROM files WHERE root=? AND path=?', (row['root'], row['path'])).fetchone()
        aid = asset(conn, row['root'], row['path'], row['mtime'], row['size'], file['media_type'] if file else 'image', True)
        conn.execute('UPDATE generations SET asset_id=? WHERE id=?', (aid, row['id']))
    for key, value in DEFAULTS.items():
        conn.execute('INSERT OR IGNORE INTO preferences VALUES (?,?)', (key, json.dumps(value)))
    conn.execute('PRAGMA user_version=3')


def settings():
    with db.db() as conn:
        return {**DEFAULTS, **{r['key']: json.loads(r['value']) for r in conn.execute('SELECT * FROM preferences') if r['key'] in DEFAULTS}}


def update_settings(values):
    limits = {'scan_interval': (5, 3600), 'backup_interval_hours': (1, 720), 'backup_keep': (1, 100),
              'archive_max_mb': (1, 4096), 'archive_expanded_max_mb': (1, 8192)}
    clean = {}
    for key, value in values.items():
        if key not in DEFAULTS:
            raise ValueError('Unknown setting: ' + key)
        if key == 'allowed_external_roots':
            if not isinstance(value, list) or not all(isinstance(v, str) and os.path.isabs(v) for v in value):
                raise ValueError('Allowed folders must be a list of absolute directory paths')
            value = list(dict.fromkeys(os.path.realpath(v) for v in value))
            if any(not os.path.isdir(v) for v in value):
                raise ValueError('Allowed folders must exist and be directories')
        elif isinstance(DEFAULTS[key], bool):
            if not isinstance(value, bool):
                raise ValueError(key + ' must be a boolean')
        elif type(value) is not int or not limits[key][0] <= value <= limits[key][1]:
            raise ValueError(key + ' is outside its allowed range')
        clean[key] = value
    with db.db() as conn:
        for key, value in clean.items():
            conn.execute('INSERT OR REPLACE INTO preferences VALUES (?,?)', (key, json.dumps(value)))
    return settings()


def metadata_workflows(metadata):
    from .extract import discover_workflows
    editor, api = discover_workflows(metadata)
    # Some custom nodes serialize NaN cache markers. JSON round-tripping must remain portable.
    def sanitize(value):
        import math
        if isinstance(value, float) and not math.isfinite(value):
            return None
        if isinstance(value, list):
            return [sanitize(v) for v in value]
        if isinstance(value, dict):
            return {k: sanitize(v) for k, v in value.items()}
        return value
    return sanitize(editor), sanitize(api)


def store_workflow(conn, editor=None, api=None):
    if editor is not None and (not isinstance(editor, dict) or not isinstance(editor.get('nodes'), list)):
        raise ValueError('Editor workflow must contain a nodes array')
    if api is not None and not isinstance(api, dict):
        raise ValueError('API graph must be an object')
    if editor is None and api is None:
        return None
    digest = hashlib.sha256(canonical([editor, api]).encode()).hexdigest()
    row = conn.execute('SELECT id FROM workflows WHERE digest=?', (digest,)).fetchone()
    if row:
        return row['id']
    wid = uid()
    conn.execute('INSERT INTO workflows VALUES (?,?,?,?,?)',
                 (wid, digest, canonical(editor) if editor is not None else None,
                  canonical(api) if api is not None else None, time.time()))
    return wid


def workflow_entry(conn, wid, explicit=False):
    digest = 'workflow:' + conn.execute('SELECT digest FROM workflows WHERE id=?', (wid,)).fetchone()[0]
    row = conn.execute('SELECT id FROM prompts WHERE hash=?', (digest,)).fetchone()
    if row:
        if explicit:
            conn.execute("UPDATE prompts SET deleted=0,origin='saved' WHERE id=?", (row['id'],))
        return row['id'], False
    now = time.time()
    cur = conn.execute('''INSERT INTO prompts(hash,title,origin,created_at,updated_at,last_seen,kind)
        VALUES (?,?,?,?,?,?,'workflow')''', (digest, 'Workflow snapshot', 'saved' if explicit else 'scan', now, now, now))
    return cur.lastrowid, True


def link_workflow(conn, pid, wid, aid=None):
    if wid:
        conn.execute('INSERT OR IGNORE INTO prompt_workflows VALUES (?,?)', (pid, wid))
        if aid:
            conn.execute('INSERT OR IGNORE INTO asset_workflows VALUES (?,?)', (aid, wid))


def save_workflow(api=None, editor=None):
    from . import extract
    # Apply the same normalisation used for embedded metadata.
    editor, api = metadata_workflows({'workflow': editor, 'prompt': api})
    with db.db() as conn:
        wid = store_workflow(conn, editor, api)
        if not wid:
            raise ValueError('No valid workflow supplied')
        result = extract.extract_prompts(api) if api else None
        pid, created = db.upsert_prompt(conn, result, True) if result else workflow_entry(conn, wid, True)
        link_workflow(conn, pid, wid)
    return {'saved': True, 'id': pid, 'created': created, 'workflow_id': wid, 'workflow_only': not bool(result)}


def workflows(pid):
    with db.db() as conn:
        return [dict(r) for r in conn.execute('''SELECT w.id,w.created_at,w.editor IS NOT NULL AS has_editor,
            w.api IS NOT NULL AS has_api FROM workflows w JOIN prompt_workflows p ON p.workflow_id=w.id
            WHERE p.prompt_id=? ORDER BY w.created_at DESC''', (pid,))]


def workflow(wid, representation='editor'):
    if representation not in ('editor', 'api'):
        raise ValueError('Unknown workflow representation')
    with db.db() as conn:
        row = conn.execute('SELECT * FROM workflows WHERE id=?', (wid,)).fetchone()
    if not row or row[representation] is None:
        raise KeyError(wid)
    return json.loads(row[representation])


def asset(conn, root, path, mtime, size, media_type, trusted=False):
    row = conn.execute('SELECT id FROM assets WHERE root=? AND path=? AND mtime=? AND size=?', (root, path, mtime, size)).fetchone()
    if row:
        if trusted:
            conn.execute('UPDATE assets SET trusted=1,media_type=? WHERE id=?', (media_type, row['id']))
        return row['id']
    aid = uid()
    conn.execute('INSERT INTO assets VALUES (?,?,?,?,?,?,?)', (aid, root, path, mtime, size, media_type, int(trusted)))
    return aid


def permitted_path(root, path, preferences):
    """Resolve links against explicit local permissions, including file links."""
    root, path = os.path.abspath(root), os.path.abspath(path)
    try:
        if os.path.commonpath([root, path]) != root:
            return False
        resolved = os.path.realpath(path)
        if not preferences['follow_symlinks']:
            # Checking components also catches links that resolve inside the output root.
            relative = os.path.relpath(path, root)
            current = root
            for part in relative.split(os.sep):
                current = os.path.join(current, part)
                if os.path.islink(current):
                    return False
        roots = [os.path.realpath(root)]
        if preferences['follow_symlinks']:
            roots += preferences.get('allowed_external_roots', [])
        return any(os.path.commonpath([resolved, allowed]) == allowed for allowed in roots)
    except (OSError, ValueError):
        return False


def asset_path(aid, verify=True):
    with db.db() as conn:
        row = conn.execute('SELECT * FROM assets WHERE id=?', (aid,)).fetchone()
    if not row or not row['trusted']:
        return None
    path = os.path.abspath(os.path.join(row['root'], row['path']))
    # Historical foreign paths are never trusted merely because an archive contains them.
    if os.path.commonpath([path, os.path.abspath(row['root'])]) != os.path.abspath(row['root']):
        return None
    if not permitted_path(row['root'], path, settings()):
        return None
    try:
        stat = os.stat(path)
        if verify and (stat.st_mtime != row['mtime'] or stat.st_size != row['size']):
            return None
    except OSError:
        return None
    return path


def asset_thumb(aid):
    if len(aid) != 32 or any(c not in '0123456789abcdef' for c in aid):
        raise ValueError('Invalid generation identifier')
    directory = os.path.join(db.data_dir(), 'previews')
    os.makedirs(directory, exist_ok=True)
    return os.path.join(directory, aid + '.jpg')


def gallery(pid, offset=0, limit=24):
    with db.db() as conn:
        total = conn.execute('SELECT COUNT(DISTINCT asset_id) FROM generations WHERE prompt_id=?', (pid,)).fetchone()[0]
        rows = conn.execute('''SELECT a.*,g.snapshot FROM assets a JOIN generations g ON g.asset_id=a.id
            WHERE g.id=(SELECT g2.id FROM generations g2 WHERE g2.asset_id=a.id AND g2.prompt_id=?
                        ORDER BY g2.extractor DESC,g2.id DESC LIMIT 1)
            ORDER BY a.mtime DESC,a.id LIMIT ? OFFSET ?''', (pid, limit, offset)).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item['snapshot'] = json.loads(item['snapshot'])
            item['workflows'] = [dict(w) for w in conn.execute('''SELECT w.id,w.editor IS NOT NULL AS has_editor,
                w.api IS NOT NULL AS has_api FROM workflows w JOIN asset_workflows aw ON aw.workflow_id=w.id
                WHERE aw.asset_id=?''', (row['id'],))]
            items.append(item)
    for item in items:
        item['available'] = bool(asset_path(item['id']))
        preview = asset_thumb(item['id'])
        item['own_preview'] = db.valid_thumb(preview)
        item['has_preview'] = item['own_preview'] or db.valid_thumb(db.thumb_path(pid))
        item['preview_version'] = str(os.stat(preview).st_mtime_ns) if item['own_preview'] else 'fallback'
        item.pop('trusted')
    return {'total': total, 'items': items}


def collections():
    with db.db() as conn:
        return [dict(r) for r in conn.execute('''SELECT c.*,COUNT(p.id) AS count FROM collections c
            LEFT JOIN collection_prompts cp ON cp.collection_id=c.id
            LEFT JOIN prompts p ON p.id=cp.prompt_id AND p.deleted=0 GROUP BY c.id ORDER BY c.name COLLATE NOCASE''')]


def collection_save(name, cid=None):
    name = str(name).strip()
    if not name or len(name) > 120:
        raise ValueError('Collection names must contain 1–120 characters')
    with db.db() as conn:
        if cid:
            if not conn.execute('SELECT 1 FROM collections WHERE id=?', (cid,)).fetchone():
                raise KeyError(cid)
            conn.execute('UPDATE collections SET name=? WHERE id=?', (name, cid))
        else:
            cid = uid()
            conn.execute('INSERT INTO collections VALUES (?,?)', (cid, name))
    return cid


def collection_delete(cid):
    with db.db() as conn:
        conn.execute('DELETE FROM collection_prompts WHERE collection_id=?', (cid,))
        conn.execute('DELETE FROM collections WHERE id=?', (cid,))


def validate_selection(selection):
    if not isinstance(selection, dict) or set(selection) not in ({'ids'}, {'filters'}):
        raise ValueError('Provide exactly one explicit selection: ids or filters')
    if 'ids' in selection:
        if not isinstance(selection['ids'], list) or any(type(i) is not int or i <= 0 for i in selection['ids']):
            raise ValueError('Selection ids must be positive integers')
    else:
        filters = selection['filters']
        if not isinstance(filters, dict) or set(filters) - {'search', 'flt', 'collection'}:
            raise ValueError('Invalid selection filters')
        if any(not isinstance(v, str) and not (k == 'collection' and v is None) for k, v in filters.items()):
            raise ValueError('Invalid selection filter value')


def selected_ids(selection):
    if 'ids' in selection:
        ids = list(dict.fromkeys(int(i) for i in selection['ids']))
        with db.db() as conn:
            return [i for i in ids if conn.execute('SELECT 1 FROM prompts WHERE id=? AND deleted=0', (i,)).fetchone()]
    filters = selection.get('filters', {})
    allowed = {k: v for k, v in filters.items() if k in ('search', 'flt', 'collection')}
    return db.matching_ids(**allowed)


def bulk(selection, action, value=None):
    if action not in ('collection_add', 'collection_remove', 'tag_add', 'tag_remove', 'favorite', 'remove'):
        raise ValueError('Unknown bulk action')
    validate_selection(selection)
    ids = selected_ids(selection)
    with db.db() as conn:
        if action in ('collection_add', 'collection_remove') and not conn.execute('SELECT 1 FROM collections WHERE id=?', (value,)).fetchone():
            raise ValueError('Collection not found')
        for pid in ids:
            if action == 'collection_add':
                conn.execute('INSERT OR IGNORE INTO collection_prompts VALUES (?,?)', (value, pid))
            elif action == 'collection_remove':
                conn.execute('DELETE FROM collection_prompts WHERE collection_id=? AND prompt_id=?', (value, pid))
            elif action in ('tag_add', 'tag_remove'):
                tags = [t.strip() for t in str(value or '').split(',') if t.strip()]
                current = [t.strip() for t in conn.execute('SELECT tags FROM prompts WHERE id=?', (pid,)).fetchone()[0].split(',') if t.strip()]
                result = list(dict.fromkeys(current + tags)) if action == 'tag_add' else [t for t in current if t not in tags]
                conn.execute('UPDATE prompts SET tags=?,updated_at=? WHERE id=?', (', '.join(result), time.time(), pid))
            elif action == 'favorite':
                conn.execute('UPDATE prompts SET favorite=?,updated_at=? WHERE id=?', (int(bool(value)), time.time(), pid))
            elif action == 'remove':
                conn.execute('UPDATE prompts SET deleted=1,updated_at=? WHERE id=?', (time.time(), pid))
            else:
                raise ValueError('Unknown bulk action')
    return {'count': len(ids)}
