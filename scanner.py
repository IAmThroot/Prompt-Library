"""Recursive, versioned imports with diagnostics and bounded automatic retries."""
import os
import threading
import time

import folder_paths

from . import db, extract, library, service

_state = threading.RLock()
_pending = False
_pending_force = False
_options = {"include_hidden": False, "follow_symlinks": False}
_retry_timer = None
_worker = None
_stopping = False
status = {"running": False, "done": 0, "total": 0, "added": 0, "error": None,
          "finished": None, "queued": False, "counts": {}, "issues": []}


def get_status():
    with _state:
        return dict(status, counts=dict(status.get("counts", {})), issues=list(status.get("issues", [])))


def start_scan(force=False, include_hidden=None, follow_symlinks=None, background=False):
    """Coalesce concurrent requests; a pending forced scan is never downgraded."""
    global _pending, _pending_force, _retry_timer, _worker
    with _state:
        if _stopping:
            return False
        for key, value in (("include_hidden", include_hidden), ("follow_symlinks", follow_symlinks)):
            if value is not None:
                _options[key] = bool(value)
        if status["running"]:
            if background:
                return False
            _pending = True
            _pending_force |= force
            status["queued"] = True
            return False
        if _retry_timer:
            _retry_timer.cancel()
            _retry_timer = None
        status.update(running=True, done=0, total=0, added=0, error=None, queued=False)
        _worker = threading.Thread(target=_run, args=(force,), name="prompt-library-scan", daemon=True)
        _worker.start()
        return True


def _run(force):
    global _pending, _pending_force, _retry_timer
    while True:
        delay = None
        try:
            with service.maintenance:
                if _stopping:
                    break
                delay = _scan(force, **dict(_options))
        except Exception as exc:
            status["error"] = str(exc)
            print(f"[Prompt Library] scan failed: {exc}")
        with _state:
            if _pending and not _stopping:
                force = _pending_force
                _pending = _pending_force = False
                status["queued"] = False
                continue
            status.update(running=False, finished=time.time())
            if delay is not None and not _stopping:
                _retry_timer = threading.Timer(max(1, delay), start_scan)
                _retry_timer.daemon = True
                _retry_timer.start()
            return


def _scan(force, include_hidden=False, follow_symlinks=False):
    root = os.path.realpath(folder_paths.get_output_directory())
    if not os.path.isdir(root):
        raise RuntimeError(f"Output folder not found: {root}")
    known = db.known_files(root)
    seen, todo, issues, counts = [], [], [], {}
    deferred = []
    inaccessible = []
    visited = set()
    excluded_prefixes = []
    def count(key):
        counts[key] = counts.get(key, 0) + 1
    def issue(path, reason):
        # Full per-file diagnostics are persisted in SQLite; bound status response size.
        if len(issues) < 200:
            issues.append({"path": path, "reason": reason})
    def preview(pid, full, rel, mtype):
        if pid and mtype in ("image", "video"):
            diagnostic = db.ensure_thumb(pid, full, mtype)
            with db.db() as conn:
                rows = conn.execute("SELECT DISTINCT asset_id FROM generations WHERE prompt_id=? AND root=? AND path=?", (pid, root, rel)).fetchall()
            for row in rows:
                if row['asset_id'] and library.asset_path(row['asset_id']):
                    asset_error = db.ensure_thumb(pid, full, mtype, library.asset_thumb(row['asset_id']))
                    diagnostic = diagnostic or asset_error
            if diagnostic:
                count("thumbnail_failed")
                issue(rel, diagnostic)
    def walk_error(error):
        inaccessible.append(str(error))
        issue(str(error.filename), str(error))
    status.update(root=root, done=0, total=0, added=0, error=None, counts=counts, issues=issues)
    for dirpath, dirnames, filenames in os.walk(root, followlinks=follow_symlinks, onerror=walk_error):
        if _stopping:
            return None
        try:
            stat = os.stat(dirpath)
            identity = (stat.st_dev, stat.st_ino)
            if identity in visited:
                excluded_prefixes.append(os.path.relpath(dirpath, root).replace(os.sep, "/") + "/")
                dirnames[:] = []
                continue
            visited.add(identity)
        except OSError as exc:
            walk_error(exc)
            continue
        count("folders")
        kept = []
        for name in dirnames:
            path = os.path.join(dirpath, name)
            if name == "__pycache__" or (name.startswith(".") and not include_hidden):
                count("excluded_folders")
                excluded_prefixes.append(os.path.relpath(path, root).replace(os.sep, "/") + "/")
            elif os.path.islink(path) and not follow_symlinks:
                count("excluded_symlinks")
                excluded_prefixes.append(os.path.relpath(path, root).replace(os.sep, "/") + "/")
            else:
                kept.append(name)
        dirnames[:] = kept
        for name in filenames:
            mtype = extract.media_type(name)
            if not mtype:
                count("unsupported")
                continue
            count("discovered")
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            seen.append(rel)
            old = known.get(rel)
            try:
                st = os.stat(full)
            except OSError as exc:
                issue(rel, str(exc)); count("failed")
                with db.db() as conn:
                    db.record_file(conn, rel, None, mtype, 0, 0, root, state="failed", error=str(exc))
                if not old or old["attempts"] < 3:
                    deferred.append(5)
                continue
            old = known.get(rel)
            same = old and old["mtime"] == st.st_mtime and old["size"] == st.st_size
            if not force and same and old["extractor"] == extract.EXTRACTOR_VERSION:
                if old["status"] not in ("failed", "deferred"):
                    count("unchanged")
                    # Thumbnail availability is independent of the metadata fingerprint.
                    preview(old["prompt_id"], full, rel, old["media_type"])
                    continue
                if old["retry_at"] > time.time():
                    if old["attempts"] < 4:
                        deferred.append(old["retry_at"] - time.time())
                    count("retry_wait")
                    continue
            if time.time() - st.st_mtime < 2:
                deferred.append(2.5)
                count("deferred")
                with db.db() as conn:
                    db.record_file(conn, rel, None, mtype, st.st_mtime, st.st_size,
                                   root, state="deferred", error="Waiting for file to finish writing")
                continue
            todo.append((rel, full, mtype, st.st_mtime, st.st_size))
    status["total"] = len(todo)
    for rel, full, mtype, mtime, size in sorted(todo, key=lambda t: t[3]):
        if _stopping:
            return None
        meta, mtype, error = extract.read_media(full, mtype)
        result = None
        try:
            if not error:
                result = extract.extract_from_metadata(meta, rel)
            after = os.stat(full)
            if (after.st_mtime, after.st_size) != (mtime, size):
                error = "File changed during metadata read; retrying"
                result = None
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        generation_metadata = any(k.lower() in {"prompt", "workflow", "parameters", "comment", "description", "usercomment", "h3_plan", "h3_manifest"} for k in meta)
        state = "failed" if error else "imported" if result else "unsupported_metadata" if generation_metadata else "no_metadata"
        pid = None
        old = known.get(rel)
        with db.db() as conn:
            editor, api_graph = library.metadata_workflows(meta) if not error else (None,None)
            wid = library.store_workflow(conn,editor,api_graph)
            previous = conn.execute("SELECT deleted FROM prompts WHERE id=?", (old["prompt_id"],)).fetchone() if old else None
            if result and previous and previous["deleted"] and (old["mtime"], old["size"]) == (mtime, size):
                # Respect a source-associated removal even if the new extractor changes its text.
                pid = old["prompt_id"]
                state = "hidden"
            elif result:
                pid, created = db.upsert_prompt(conn, result, explicit=False, seen_ts=mtime)
                status["added"] += int(created)
                hidden = conn.execute("SELECT deleted FROM prompts WHERE id=?", (pid,)).fetchone()[0]
                if hidden:
                    state = "hidden"
                elif not created:
                    state = "duplicate"
                if result.get("warnings"):
                    issue(rel, "; ".join(result["warnings"]))
            if not pid and wid:
                pid, created = library.workflow_entry(conn,wid)
                status["added"] += int(created)
                state = "workflow_only"
            diagnostic = error or ("Metadata present but no supported prompt was resolved" if state == "unsupported_metadata" else
                                   "No generation metadata found" if state == "no_metadata" else None)
            db.record_file(conn, rel, pid, mtype, mtime, size, root, state=state, error=diagnostic, result=result, workflow_id=wid)
        preview(pid, full, rel, mtype)
        count(state)
        if diagnostic:
            issue(rel, diagnostic)
        if state == "failed" and (not old or old["attempts"] < 3):
            deferred.append(5 * 2 ** min(old["attempts"] if old else 0, 3))
        status["done"] += 1
    # Excluded directories are unobserved, not deleted.
    seen.extend(path for path in known if any(path.startswith(prefix) for prefix in excluded_prefixes))
    db.finish_scan(seen, root, complete=not inaccessible)
    if inaccessible:
        status["error"] = "Some folders could not be read; see scan report"
    return min(deferred) if deferred else None


def stop():
    global _stopping, _pending, _pending_force
    with _state:
        _stopping = True
        _pending = _pending_force = False
        if _retry_timer:
            _retry_timer.cancel()
    if _worker and _worker is not threading.current_thread():
        _worker.join()
    status['running'] = False
