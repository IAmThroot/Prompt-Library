"""SQLite storage. Prompts live here independently of the files they came from,
so deleting or moving an image/audio/video never removes its prompt."""
import io
import json
import os
import sqlite3
import shutil
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager, closing
from collections import OrderedDict
from . import validation

import folder_paths

from .extract import norm, prompt_hash, EXTRACTOR_VERSION

SCHEMA = """
CREATE TABLE IF NOT EXISTS prompts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    hash       TEXT UNIQUE NOT NULL,
    title      TEXT NOT NULL DEFAULT '',
    positive   TEXT NOT NULL DEFAULT '',
    negative   TEXT NOT NULL DEFAULT '',
    extra      TEXT NOT NULL DEFAULT '[]',
    meta       TEXT NOT NULL DEFAULT '{}',
    tags       TEXT NOT NULL DEFAULT '',
    notes      TEXT NOT NULL DEFAULT '',
    favorite   INTEGER NOT NULL DEFAULT 0,
    deleted    INTEGER NOT NULL DEFAULT 0,
    origin     TEXT NOT NULL DEFAULT 'scan',      -- 'scan' (found in outputs) or 'saved' (saved by hand)
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    last_seen  REAL NOT NULL                      -- newest file time, or save time
);
CREATE TABLE IF NOT EXISTS files (
    path       TEXT PRIMARY KEY,                  -- relative to the outputs folder
    prompt_id  INTEGER,
    media_type TEXT NOT NULL,
    mtime      REAL NOT NULL,
    size       INTEGER NOT NULL,
    missing    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_files_prompt ON files(prompt_id);
"""

_lock = threading.RLock()
_db_path = None


def data_dir():
    try:
        base = folder_paths.get_user_directory()
    except AttributeError:  # older ComfyUI
        base = os.path.join(folder_paths.base_path, "user")
    path = os.path.join(base, "prompt_library")
    os.makedirs(os.path.join(path, "thumbs"), exist_ok=True)
    return path


def output_root():
    return os.path.realpath(folder_paths.get_output_directory())


def init():
    global _db_path
    _db_path = os.path.join(data_dir(), "library.db")
    # SQLite's backup API includes committed WAL data. Back up before schema changes.
    with _lock:
        conn = sqlite3.connect(_db_path)
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        exists = conn.execute("SELECT 1 FROM sqlite_master WHERE name='prompts'").fetchone()
        if exists and version < 3:
            backup = _db_path + (".before-v2.bak" if version < 2 else ".before-v3.bak")
            if not os.path.exists(backup):
                with closing(sqlite3.connect(backup + ".tmp")) as destination:
                    conn.backup(destination)
                os.replace(backup + ".tmp", backup)
        conn.close()
    with db() as conn:
        conn.executescript(SCHEMA)
        conn.execute("BEGIN IMMEDIATE")
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(files)")}
        if "root" not in columns:
            conn.execute("ALTER TABLE files RENAME TO legacy_files")
            conn.execute("DROP INDEX IF EXISTS idx_files_prompt")
            conn.execute("""CREATE TABLE files (
                root TEXT NOT NULL, path TEXT NOT NULL, prompt_id INTEGER,
                media_type TEXT NOT NULL, mtime REAL NOT NULL, size INTEGER NOT NULL,
                missing INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'pending',
                error TEXT, extractor INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
                retry_at REAL NOT NULL DEFAULT 0, PRIMARY KEY(root,path))""")
            conn.execute("""INSERT INTO files(root,path,prompt_id,media_type,mtime,size,missing)
                SELECT ?,path,prompt_id,media_type,mtime,size,missing FROM legacy_files""", (output_root(),))
            conn.execute("DROP TABLE legacy_files")
            conn.execute("CREATE INDEX idx_files_prompt ON files(prompt_id)")
        conn.execute("""CREATE TABLE IF NOT EXISTS generations (
            id INTEGER PRIMARY KEY, prompt_id INTEGER NOT NULL, root TEXT NOT NULL,
            path TEXT NOT NULL, mtime REAL NOT NULL, size INTEGER NOT NULL,
            extractor INTEGER NOT NULL, snapshot TEXT NOT NULL, created_at REAL NOT NULL,
            UNIQUE(root,path,mtime,size,extractor))""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_generations_prompt ON generations(prompt_id)")
        conn.execute("""CREATE TABLE IF NOT EXISTS prompt_details (
            prompt_id INTEGER PRIMARY KEY, details TEXT NOT NULL)""")
        from . import library
        library.migrate(conn)


@contextmanager
def db():
    with _lock:
        conn = sqlite3.connect(_db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


# --------------------------------------------------------------------------- #
# Writing
# --------------------------------------------------------------------------- #

def upsert_prompt(conn, data, explicit, seen_ts=None, title="", tags="", notes=""):
    """Insert a prompt or find the existing one with the same text.

    explicit=True means the user saved it on purpose: it un-hides a removed prompt
    and marks it as 'saved'. Returns (id, created).
    """
    pos, neg = norm(data.get("positive")), norm(data.get("negative"))
    digest = prompt_hash(pos, neg, data.get("extra"))
    now = time.time()
    seen_ts = seen_ts or now
    row = conn.execute("SELECT * FROM prompts WHERE hash=?", (digest,)).fetchone()
    if not row and not explicit:
        # A legacy removal must remain hidden even when extraction gains stage support.
        row = conn.execute("SELECT * FROM prompts WHERE hash=? AND deleted=1",
                           (prompt_hash(pos, neg),)).fetchone()
    if row:
        sets = {"last_seen": max(row["last_seen"] or 0, seen_ts)}
        created = False
        if explicit:
            created = bool(row["deleted"])
            sets.update(deleted=0, origin="saved", updated_at=now)
            if title:
                sets["title"] = title
            if tags:
                sets["tags"] = ", ".join(dict.fromkeys(t.strip() for t in (row["tags"] + "," + tags).split(",") if t.strip()))
            if notes and notes not in row["notes"].split("\n\n"):
                sets["notes"] = "\n\n".join(x for x in (row["notes"], notes) if x)
        conn.execute(f"UPDATE prompts SET {','.join(k + '=?' for k in sets)} WHERE id=?", (*sets.values(), row["id"]))
        if any(data.get(k) for k in ("warnings", "provenance", "stages")):
            conn.execute("UPDATE prompts SET meta=? WHERE id=?", (json.dumps(data.get("meta") or {}), row["id"]))
            conn.execute("INSERT OR REPLACE INTO prompt_details VALUES (?,?)",
                         (row["id"], json.dumps({k: data.get(k) for k in ("warnings", "provenance", "stages")})))
        return row["id"], created
    cur = conn.execute(
        "INSERT INTO prompts(hash,title,positive,negative,extra,meta,tags,notes,origin,created_at,updated_at,last_seen)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (digest, title, pos, neg, json.dumps(data.get("extra") or [], ensure_ascii=False), json.dumps(data.get("meta") or {}),
         tags, notes, "saved" if explicit else "scan", now, now, seen_ts))
    conn.execute("INSERT INTO prompt_details VALUES (?,?)",
                 (cur.lastrowid, json.dumps({k: data.get(k) for k in ("warnings", "provenance", "stages")})))
    return cur.lastrowid, True


def save_prompt(data, title="", tags="", notes=""):
    with db() as conn:
        return upsert_prompt(conn, data, explicit=True, title=title.strip(), tags=tags.strip(), notes=notes.strip())


def update_prompt(pid, fields):
    with db() as conn:
        row = conn.execute("SELECT * FROM prompts WHERE id=?", (pid,)).fetchone()
        if not row:
            raise KeyError(pid)
        vals = {}
        for key in ("title", "tags", "notes"):
            if key in fields:
                vals[key] = str(fields[key]).strip()
        if "favorite" in fields:
            vals["favorite"] = 1 if fields["favorite"] else 0
        if "positive" in fields or "negative" in fields:
            pos = norm(fields.get("positive", row["positive"]))
            neg = norm(fields.get("negative", row["negative"]))
            if not (pos or neg):
                raise ValueError("A prompt needs a positive or a negative text.")
            if pos != row["positive"] or neg != row["negative"]:
                imported = conn.execute("SELECT 1 FROM files WHERE prompt_id=? UNION SELECT 1 FROM generations WHERE prompt_id=? UNION SELECT 1 FROM prompt_workflows WHERE prompt_id=?",
                                        (pid, pid, pid)).fetchone()
                if imported or row["origin"] == "scan":
                    # Editing imported text makes a personal copy; provenance stays immutable.
                    copied = {"positive": pos, "negative": neg}
                    copy_id, _ = upsert_prompt(conn, copied, True,
                        title=vals.get("title", row["title"]), tags=vals.get("tags", row["tags"]),
                        notes=vals.get("notes", row["notes"]))
                    conn.execute("UPDATE prompts SET favorite=? WHERE id=?",
                                 (vals.get("favorite", row["favorite"]), copy_id))
                    return copy_id
                vals.update(positive=pos, negative=neg, hash=prompt_hash(pos, neg, json.loads(row["extra"])))

        vals["updated_at"] = time.time()
        try:
            conn.execute(f"UPDATE prompts SET {','.join(k + '=?' for k in vals)} WHERE id=?", (*vals.values(), pid))
        except sqlite3.IntegrityError:
            raise ValueError("Another prompt in the library already has this exact text.")
        return pid


def delete_prompt(pid):
    """Soft delete: keeps the row so a rescan doesn't bring the prompt back."""
    with db() as conn:
        conn.execute("UPDATE prompts SET deleted=1, updated_at=? WHERE id=?", (time.time(), pid))


# --------------------------------------------------------------------------- #
# Scanner helpers
# --------------------------------------------------------------------------- #

def known_files(root=None):
    with db() as conn:
        return {r["path"]: dict(r) for r in conn.execute("SELECT * FROM files WHERE root=?", (root or output_root(),))}


def record_file(conn, path, prompt_id, mtype, mtime, size, root=None,
                state="imported", error=None, result=None, workflow_id=None):
    root = root or output_root()
    old = conn.execute("SELECT * FROM files WHERE root=? AND path=?", (root, path)).fetchone()
    # A transient failure must not detach a previously imported prompt.
    if prompt_id is None and old and state in ("failed", "deferred"):
        prompt_id = old["prompt_id"]
    attempts = 0
    if state == "failed":
        attempts = (old["attempts"] if old and old["status"] == "failed" and old["mtime"] == mtime and old["size"] == size else 0) + 1
    retry_at = time.time() + min(3600, 5 * 2 ** min(attempts - 1, 10)) if state == "failed" else 0
    conn.execute("""INSERT OR REPLACE INTO files
        (root,path,prompt_id,media_type,mtime,size,status,error,extractor,attempts,retry_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (root,path,prompt_id,mtype,mtime,size,state,error,EXTRACTOR_VERSION,attempts,retry_at))
    if prompt_id and (result or workflow_id):
        from . import library
        aid = library.asset(conn, root, path, mtime, size, mtype, True)
        conn.execute("""INSERT OR IGNORE INTO generations
            (prompt_id,root,path,mtime,size,extractor,snapshot,created_at,asset_id) VALUES (?,?,?,?,?,?,?,?,?)""",
            (prompt_id,root,path,mtime,size,EXTRACTOR_VERSION,json.dumps(result or {}),time.time(),aid))
        library.link_workflow(conn, prompt_id, workflow_id, aid)
    if old and old["prompt_id"] and prompt_id != old["prompt_id"]:
        previous = conn.execute("SELECT * FROM prompts WHERE id=?", (old["prompt_id"],)).fetchone()
        if previous:
            # Keep annotations on the corrected entry as well as on the archived source record.
            for key in ("title", "tags", "notes"):
                if previous[key]:
                    conn.execute(f"UPDATE prompts SET {key}=? WHERE id=? AND {key}=''", (previous[key],prompt_id))
            if previous["favorite"]:
                conn.execute("UPDATE prompts SET favorite=1 WHERE id=?", (prompt_id,))
            remaining = conn.execute("SELECT 1 FROM files WHERE prompt_id=?", (previous["id"],)).fetchone()
            repair = (old["mtime"], old["size"]) == (mtime, size) and old["extractor"] < EXTRACTOR_VERSION
            if repair and not remaining and previous["origin"] == "scan" and not any(previous[k] for k in ("title","tags","notes","favorite")):
                conn.execute("UPDATE prompts SET deleted=1 WHERE id=?", (previous["id"],))


def finish_scan(seen_paths, root=None, complete=True):
    """An inaccessible subtree must not make its files appear deleted."""
    if not complete:
        return
    with db() as conn:
        conn.execute("CREATE TEMP TABLE seen(path TEXT PRIMARY KEY)")
        conn.executemany("INSERT OR IGNORE INTO seen VALUES (?)", ((p,) for p in seen_paths))
        conn.execute("UPDATE files SET missing=CASE WHEN path IN (SELECT path FROM seen) THEN 0 ELSE 1 END WHERE root=?",
                     (root or output_root(),))


def generation_history(pid):
    with db() as conn:
        return [dict(r, snapshot=json.loads(r["snapshot"])) for r in conn.execute(
            "SELECT * FROM generations WHERE prompt_id=? ORDER BY created_at DESC", (pid,))]


def scan_report(root=None):
    with db() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT path,media_type,status,error,missing FROM files WHERE root=? ORDER BY path",
            (root or output_root(),))]


def thumb_path(pid):
    return os.path.join(data_dir(), "thumbs", f"{int(pid)}.jpg")


_thumb_cache = OrderedDict()
_thumb_lock = threading.Lock()


def invalidate_thumbs(path=None):
    with _thumb_lock:
        if path is None:
            _thumb_cache.clear()
        else:
            _thumb_cache.pop(str(path), None)


def valid_thumb(path):
    path = str(path)
    try:
        st = os.stat(path)
        fingerprint = (st.st_size, st.st_mtime_ns)
        with _thumb_lock:
            cached = _thumb_cache.get(path)
            if cached and cached[0] == fingerprint:
                _thumb_cache.move_to_end(path)
                return cached[1]
        from PIL import Image
        try:
            with Image.open(path) as image:
                valid = image.format == 'JPEG'
                if valid:
                    image.load()
        except Exception:
            valid = False
        with _thumb_lock:
            _thumb_cache[path] = (fingerprint, valid)
            _thumb_cache.move_to_end(path)
            while len(_thumb_cache) > 4096:
                _thumb_cache.popitem(last=False)
        return valid
    except OSError:
        invalidate_thumbs(path)
        return False


def _video_frame(src):
    """Decode one frame, trying PyAV first and FFmpeg when unavailable/unsupported."""
    errors = []
    try:
        import av
        with av.open(src) as container:
            for frame in container.decode(video=0):
                scale = min(256 / frame.width, 256 / frame.height, 1)
                return frame.reformat(width=max(1, round(frame.width * scale)),
                                      height=max(1, round(frame.height * scale)), format="rgb24").to_image()
        errors.append("No video frames found")
    except Exception as exc:
        errors.append(str(exc))
    if shutil.which("ffmpeg"):
        try:
            from PIL import Image
            result = subprocess.run([
                "ffmpeg", "-nostdin", "-v", "error", "-threads", "1", "-i", os.path.abspath(src),
                "-map", "0:v:0", "-frames:v", "1", "-vf",
                "scale=256:256:force_original_aspect_ratio=decrease", "-threads", "1",
                "-f", "image2pipe", "-c:v", "mjpeg", "pipe:1"],
                capture_output=True, timeout=30)
            if result.returncode or not result.stdout:
                raise RuntimeError(result.stderr.decode("utf-8", "replace")[:1000] or "No video frame decoded")
            with Image.open(io.BytesIO(result.stdout)) as image:
                return image.copy()
        except Exception as exc:
            errors.append(str(exc))
    else:
        errors.append("Video previews require PyAV or FFmpeg (ffmpeg); ffprobe alone cannot decode a preview")
    raise RuntimeError("; ".join(errors))


def ensure_thumb(pid, src, mtype="image", destination=None):
    """Retain a preview independently of the source; return a diagnostic on failure."""
    dst = destination or thumb_path(pid)
    if valid_thumb(dst):
        return None
    temporary = None
    try:
        from PIL import Image, ImageOps
        if mtype == "video":
            image = _video_frame(src)
        else:
            with Image.open(src) as source:
                image = ImageOps.exif_transpose(source).copy()
        try:
            image.thumbnail((256, 256))
            with tempfile.NamedTemporaryFile(dir=os.path.dirname(dst), suffix=".jpg", delete=False) as output:
                temporary = output.name
            image.convert("RGB").save(temporary, "JPEG", quality=80)
            # Only publish complete images, so concurrent requests cannot see a partial JPEG.
            os.replace(temporary, dst)
            invalidate_thumbs(dst)
        finally:
            image.close()
        return None
    except Exception as exc:
        return f"Thumbnail unavailable: {type(exc).__name__}: {exc}"
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #

def _like(term):
    return "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _row(r):
    d = dict(r)
    d["extra"] = json.loads(d.get("extra") or "[]")
    d["meta"] = json.loads(d.get("meta") or "{}")
    d["media"] = [m for m in (d.get("media") or "").split(",") if m]
    path = thumb_path(d["id"])
    d["has_thumb"] = valid_thumb(path)
    try:
        d["thumb_version"] = str(os.stat(path).st_mtime_ns) if d["has_thumb"] else "0"
    except OSError:
        d["has_thumb"], d["thumb_version"] = False, "0"
    d.update(validation.safe_details(json.loads(d.pop("details", None) or "{}")))
    snapshot = json.loads(d.pop('representative', None) or '{}')
    if snapshot:
        d['meta'] = snapshot.get('meta') or {}
        d.update(validation.safe_details({k: snapshot.get(k) for k in ('warnings', 'provenance', 'stages')}))
    d.pop("hash", None)
    return d


def prompt_filter(search="", flt="all", collection=None):
    where, args = ["p.deleted=0"], []
    for term in search.split():
        where.append("(" + " OR ".join(f"p.{c} LIKE ? ESCAPE '\\'" for c in
                                       ("positive", "negative", "title", "tags", "notes", "extra")) + ")")
        args += [_like(term)] * 6
    if flt == "favorites":
        where.append("p.favorite=1")
    elif flt == "saved":
        where.append("p.origin='saved'")
    elif flt in ("image", "video", "audio"):
        where.append("EXISTS (SELECT 1 FROM files f WHERE f.prompt_id=p.id AND f.media_type=?)")
        args.append(flt)
    if collection:
        where.append("EXISTS (SELECT 1 FROM collection_prompts cp WHERE cp.prompt_id=p.id AND cp.collection_id=?)")
        args.append(collection)
    return " AND ".join(where), args


def matching_ids(search="", flt="all", collection=None):
    clause, args = prompt_filter(search, flt, collection)
    with db() as conn:
        return [r[0] for r in conn.execute(f"SELECT p.id FROM prompts p WHERE {clause} ORDER BY p.id", args)]


def list_prompts(search="", flt="all", sort="latest", offset=0, limit=50, collection=None):
    clause, args = prompt_filter(search, flt, collection)
    order = {"oldest": "p.last_seen ASC", "added": "p.created_at DESC"}.get(sort, "p.last_seen DESC")
    with db() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM prompts p WHERE {clause}", args).fetchone()[0]
        rows = conn.execute(
            f"""SELECT p.*,
                   (SELECT details FROM prompt_details WHERE prompt_id=p.id) AS details,
                   (SELECT snapshot FROM generations WHERE prompt_id=p.id ORDER BY mtime DESC,id DESC LIMIT 1) AS representative,
                   (SELECT COUNT(*) FROM files f WHERE f.prompt_id=p.id) AS file_count,
                   (SELECT COUNT(*) FROM files f WHERE f.prompt_id=p.id AND f.missing=0) AS available_count,
                   (SELECT group_concat(DISTINCT f.media_type) FROM files f WHERE f.prompt_id=p.id) AS media
                FROM prompts p WHERE {clause} ORDER BY {order}, p.id DESC LIMIT ? OFFSET ?""",
            (*args, limit, offset)).fetchall()
    return {"total": total, "items": [_row(r) for r in rows]}


def prompt_files(pid):
    with db() as conn:
        rows = conn.execute("SELECT root, path, media_type, mtime, missing, status, error FROM files WHERE prompt_id=? "
                            "ORDER BY missing ASC, mtime DESC", (pid,)).fetchall()
    return [dict(r, current_root=r["root"] == output_root()) for r in rows]
