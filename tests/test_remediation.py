"""Regression coverage for the audit fixes; all storage is disposable."""
import asyncio
import hashlib
import io
import json
import os
from pathlib import Path
import secrets
import sqlite3
import sys
import threading
import time
import types
import unittest
from unittest.mock import patch
import zipfile
from contextlib import closing

import test_features as base
from test_features import db, library, archives, service, scanner, extract
from test_library import graph, folders


class RemediationTests(unittest.TestCase):
    setUp = base.FeatureTests.setUp

    def tearDown(self):
        service.stop()
        scanner._stopping = False
        service._jobs.clear()
        db.invalidate_thumbs()
        base.FeatureTests.tearDown(self)

    def image(self, path, prompt='example'):
        from PIL import Image, PngImagePlugin
        metadata = PngImagePlugin.PngInfo()
        metadata.add_text('parameters', prompt)
        Image.new('RGB', (8, 8)).save(path, pnginfo=metadata)
        os.utime(path, (time.time()-10,)*2)
        return path

    def archive(self):
        pid = db.save_prompt({'positive': 'example', 'warnings': ['review']})[0]
        path = self.base/'portable.zip'
        archives.export_archive(path)
        return pid, path

    def mutate(self, path, transform):
        with zipfile.ZipFile(path) as source:
            files = {name: source.read(name) for name in source.namelist()}
        data = json.loads(files['library.json'])
        transform(data)
        files['library.json'] = json.dumps(data).encode()
        with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as output:
            for name, content in files.items():
                output.writestr(name, content)

    def test_reserved_details_rejected_and_legacy_reads_safe(self):
        pid, path = self.archive()
        self.mutate(path, lambda data: data['details'][0].update(details=json.dumps({'id': 999, 'media': None})))
        with self.assertRaisesRegex(ValueError, 'reserved'):
            archives.import_archive(path)
        with db.db() as conn:
            conn.execute('UPDATE prompt_details SET details=? WHERE prompt_id=?', ('{"id":999,"media":null,"warnings":["ok"]}', pid))
        row = db.list_prompts()['items'][0]
        self.assertEqual(row['id'], pid)
        self.assertEqual(row['media'], [])
        self.assertEqual(row['warnings'], ['ok'])

    def test_nested_record_validation(self):
        for bad in ({'stages': [{'positive': [], 'negative': ''}]}, {'warnings': 'bad'}, {'provenance': []}):
            with self.subTest(bad=bad):
                _, path = self.archive()
                self.mutate(path, lambda data: data['details'][0].update(details=json.dumps(bad)))
                with self.assertRaises(ValueError):
                    archives.validate(path)

    def test_oversized_preview_rejected_before_open(self):
        _, path = self.archive()
        with zipfile.ZipFile(path) as z:
            data = json.loads(z.read('library.json'))
        name = 'thumbs/'+data['prompts'][0]['uuid']+'.jpg'
        data['previews'] = {name: '0'*64}
        with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as z:
            z.writestr('library.json', json.dumps(data))
            z.writestr(name, b'0'*(archives.PREVIEW_LIMIT+1))
        original = zipfile.ZipFile.open
        def guarded(z, member, *args, **kwargs):
            self.assertNotEqual(member, name, 'oversized preview was opened')
            return original(z, member, *args, **kwargs)
        with patch.object(zipfile.ZipFile, 'open', guarded):
            with self.assertRaisesRegex(ValueError, 'limit'):
                archives.validate(path)

    def test_links_require_enabled_and_explicit_roots(self):
        outside = self.base/'external'; outside.mkdir()
        self.image(outside/'private.png')
        (self.output/'file.png').symlink_to(outside/'private.png')
        (self.output/'folder').symlink_to(outside, target_is_directory=True)
        scanner._scan(False)
        self.assertEqual(db.list_prompts()['total'], 0)
        scanner._scan(False, follow_symlinks=True)
        self.assertEqual(db.list_prompts()['total'], 0)
        library.update_settings({'follow_symlinks': True, 'allowed_external_roots': [str(outside)]})
        scanner._scan(False, follow_symlinks=True)
        row = db.list_prompts()['items'][0]
        asset = library.gallery(row['id'])['items'][0]
        self.assertTrue(library.asset_path(asset['id']))
        library.update_settings({'allowed_external_roots': []})
        self.assertIsNone(library.asset_path(asset['id']))
        self.assertEqual(db.list_prompts()['total'], 1)

    def test_internal_file_link_also_obeys_setting(self):
        real = self.image(self.output/'original.png')
        link = self.output/'linked.png'; link.symlink_to(real)
        prefs = library.settings()
        self.assertFalse(library.permitted_path(str(self.output), str(link), prefs))
        prefs['follow_symlinks'] = True
        self.assertTrue(library.permitted_path(str(self.output), str(link), prefs))

    def test_recovery_exceeds_portable_limits_and_is_not_importable(self):
        library.update_settings({'archive_max_mb': 1})
        db.save_prompt({'positive': 'big'}, notes=secrets.token_hex(1024*1024))
        with patch.object(Path, 'read_bytes', side_effect=AssertionError('whole file read')):
            backup = archives.create_backup()
        path = archives.backup_path(backup['id'])
        self.assertGreater(path.stat().st_size, 1024**2)
        # Raising the upload limit exposes the specific recovery format error.
        library.update_settings({'archive_max_mb': 512})
        with self.assertRaisesRegex(ValueError, 'backup interface'):
            archives.preview_import(path)
        library.update_settings({'archive_max_mb': 1})
        db.save_prompt({'positive': 'after'})
        archives.restore_backup(backup['id'])
        self.assertEqual(db.list_prompts()['total'], 1)

    def test_legacy_local_backup_ignores_portable_manifest_limit(self):
        db.save_prompt({'positive': 'legacy'})
        snapshot = self.base/'snapshot.db'; archives.snapshot_database(snapshot)
        with closing(sqlite3.connect(snapshot)) as conn:
            conn.row_factory = sqlite3.Row
            data = archives.payload(conn, complete=True)
        bid = library.uid(); path = archives.directory('backups')/(bid+'.zip')
        archives._write_archive(path, data, snapshot)
        with db.db() as conn:
            conn.execute('INSERT INTO backups VALUES (?,?,?,?,?)', (bid,path.name,time.time(),0,'legacy'))
        db.save_prompt({'positive': 'new'})
        with patch.object(archives, 'MANIFEST_LIMIT', 1):
            archives.restore_backup(bid)
        self.assertEqual(db.list_prompts()['items'][0]['positive'], 'legacy')

    def test_invalid_staged_backup_not_published(self):
        db.save_prompt({'positive': 'test'})
        with patch.object(archives, 'validate_recovery', side_effect=ValueError('broken')):
            with self.assertRaises(ValueError):
                archives.create_backup()
        self.assertEqual(archives.list_backups(), [])
        self.assertEqual(list(archives.directory('backups').glob('*.zip')), [])

    def test_corrupt_recovery_database_refused_without_replacement(self):
        db.save_prompt({'positive': 'before'})
        backup = archives.create_backup(); path = archives.backup_path(backup['id'])
        with zipfile.ZipFile(path) as z:
            members = {n: z.read(n) for n in z.namelist()}
        members['database.sqlite'] = b'bad db'
        with zipfile.ZipFile(path, 'w') as z:
            for n, v in members.items(): z.writestr(n, v)
        with self.assertRaises(ValueError): archives.restore_backup(backup['id'])
        self.assertEqual(db.list_prompts()['items'][0]['positive'], 'before')

    def test_portable_export_limits_clean_partial_files(self):
        db.save_prompt({'positive': 'example'})
        path = self.base/'limited.zip'
        with patch.object(archives, 'MANIFEST_LIMIT', 1):
            with self.assertRaisesRegex(ValueError, 'manifest'):
                archives.export_archive(path)
        self.assertFalse(path.exists())
        self.assertFalse(Path(str(path)+'.tmp').exists())

    def test_bulk_selection_must_be_explicit(self):
        db.save_prompt({'positive': 'keep'})
        for selection in (None, {}, {'ids': [], 'filters': {}}, {'ids': '1'}, {'filters': {'unknown': 'x'}}):
            with self.assertRaises(ValueError): library.bulk(selection, 'remove')
        self.assertEqual(library.bulk({'ids': []}, 'remove')['count'], 0)
        self.assertEqual(db.list_prompts()['total'], 1)
        self.assertEqual(library.bulk({'filters': {}}, 'remove')['count'], 1)
        with self.assertRaises(ValueError): library.bulk({'ids': []}, 'unknown')

    def test_workflow_edit_preserves_source(self):
        saved = library.save_workflow(graph('original'), {'nodes': []})
        copied = db.update_prompt(saved['id'], {'positive': 'edited'})
        self.assertNotEqual(copied, saved['id'])
        self.assertEqual(library.workflows(copied), [])
        self.assertEqual(library.workflow(saved['workflow_id'], 'api')['1']['inputs']['text'], 'original')
        self.assertEqual(db.list_prompts(search='original')['total'], 1)

    def test_representative_snapshot_uses_timestamp_not_scan_order(self):
        pid = db.save_prompt({'positive': 'same'})[0]
        with db.db() as conn:
            for path, ts, seed in [('new.png', 20, 20), ('old.png', 10, 10), ('tie.png', 20, 21)]:
                result = {'positive': 'same', 'meta': {'seed': seed}, 'stages': [
                    {'positive': 'same', 'negative': '', 'meta': {'seed': seed}}]}
                db.upsert_prompt(conn, result, False, ts)
                db.record_file(conn, path, pid, 'image', ts, 1, result=result)
        row = db.list_prompts()['items'][0]
        self.assertEqual(row['meta']['seed'], 21)
        self.assertEqual(row['stages'][0]['meta']['seed'], 21)
        self.assertEqual(len(db.generation_history(pid)), 3)

    def test_comment_graph_workflow_and_case_normalization(self):
        data = {'COMMENT': json.dumps(graph())}
        self.assertIsNotNone(extract.extract_from_metadata(data))
        self.assertEqual(library.metadata_workflows(data)[1], graph())
        data = {'Description': json.dumps({'PROMPT': json.dumps(graph()), 'WORKFLOW': {'nodes': []}})}
        self.assertEqual(library.metadata_workflows(data), ({'nodes': []}, graph()))

    def test_pyav_failure_uses_probe_and_preserves_errors(self):
        av = types.SimpleNamespace(open=lambda path: (_ for _ in ()).throw(ValueError('av failed')))
        with patch.dict(sys.modules, {'av': av}), patch.object(extract, '_probe', return_value=({'comment': 'ok'}, [])):
            self.assertEqual(extract._read_av('video.mp4'), {'comment': 'ok'})
        with patch.dict(sys.modules, {'av': av}), patch.object(extract, '_probe', side_effect=ValueError('probe failed')):
            with self.assertRaisesRegex(RuntimeError, 'av failed.*probe failed'): extract._read_av('video.mp4')

    def test_unchanged_scan_batches_queries_and_reuses_decodes(self):
        from PIL import Image
        for i in range(8): self.image(self.output/f'{i}.png', str(i))
        scanner._scan(False)
        scanner._scan(False)  # populate validation cache for new thumbnails
        with patch.object(db.sqlite3, 'connect', wraps=db.sqlite3.connect) as connects, patch.object(Image, 'open', wraps=Image.open) as opens:
            scanner._scan(False)
        self.assertLessEqual(connects.call_count, 4)
        self.assertEqual(opens.call_count, 0)

    def test_preview_failure_backoff_and_force(self):
        self.image(self.output/'one.png')
        with patch.object(db, 'ensure_thumb', return_value='decode failure') as make:
            scanner._scan(False); first = make.call_count
            scanner._scan(False)
            self.assertEqual(make.call_count, first)
            scanner._scan(True)
            self.assertGreater(make.call_count, first)

    def test_export_reads_only_selected_rows(self):
        one = db.save_prompt({'positive': 'one'})[0]
        db.save_prompt({'positive': 'two'})
        with patch.object(archives, '_rows', side_effect=AssertionError('full table materialization')):
            archives.export_archive(self.base/'one.zip', {'ids': [one]})
        self.assertEqual(len(archives.validate(self.base/'one.zip')['prompts']), 1)

    def test_job_queue_and_storage_admission(self):
        with patch.object(service, 'MAX_ACTIVE', 1):
            jid = service.reserve('export')
            with self.assertRaises(service.JobCapacityError): service.reserve('export')
            service.fail(jid, 'cancelled')
        with patch.object(service, 'MAX_ARTIFACT_BYTES', 1):
            with self.assertRaises(service.JobCapacityError): service.reserve('export')

    def test_job_restart_and_orphan_cleanup(self):
        token = library.uid(); jid = service.reserve('import_preview', upload=token)
        service.job_file(token, '.upload').write_bytes(b'partial')
        orphan = service.job_file(library.uid(), '.zip'); orphan.write_bytes(b'orphan')
        service._jobs.clear()
        service.reconcile()
        self.assertEqual(service.job(jid)['state'], 'failed')
        self.assertFalse(service.job_file(token, '.upload').exists())
        self.assertFalse(orphan.exists())

    def test_report_full_on_disk_bounded_in_response(self):
        report = {'conflicts': [{'field': 'notes', 'local': 'x'*5000} for _ in range(101)]}
        path = self.base/'report.json'
        summary = service.compact_report(report, path)
        self.assertEqual(summary['conflict_count'], 101)
        self.assertEqual(len(summary['conflicts']), 100)
        self.assertLessEqual(len(summary['conflicts'][0]['local']), 4096)
        self.assertTrue(summary['conflicts_truncated'])
        self.assertEqual(len(json.loads(path.read_text())['conflicts']), 101)

    def test_retention_does_not_evict_active_dependency(self):
        jid = service.reserve('import_preview')
        service._jobs[jid].update(state='complete', finished_at=0, reserved_bytes=0)
        token = library.uid(); service._jobs[jid]['upload'] = token
        service.job_file(token, '.upload').write_bytes(b'input')
        commit = service.reserve('import', dependencies=[jid])
        with service._job_lock: service.prune()
        self.assertIn(jid, service._jobs)
        self.assertTrue(service.job_file(token, '.upload').exists())
        service.fail(commit, 'cancelled')
        with service._job_lock: service.prune()
        self.assertNotIn(jid, service._jobs)

    def test_startup_respects_disabled_capture(self):
        library.update_settings({'capture_enabled': False, 'backup_enabled': False})
        with patch.object(scanner, 'start_scan') as start:
            service.start(); time.sleep(.05); service.stop()
            start.assert_not_called()
        source = (Path(__file__).resolve().parents[1]/'__init__.py').read_text()
        self.assertNotIn('scanner.start_scan()', source)

    def test_backup_hashing_releases_live_database_lock(self):
        db.save_prompt({'positive': 'test'})
        original = archives.file_digest
        def checked(path):
            acquired = []
            def probe():
                ok = db._lock.acquire(timeout=.2)
                acquired.append(ok)
                if ok: db._lock.release()
            thread = threading.Thread(target=probe); thread.start(); thread.join(1)
            self.assertEqual(acquired, [True])
            return original(path)
        with patch.object(archives, 'file_digest', checked):
            archives.create_backup()

    def test_commit_validates_once_and_report_failure_rolls_back(self):
        _, path = self.archive()
        with patch.object(archives, 'validate', wraps=archives.validate) as validate:
            archives.import_archive(path)
            self.assertEqual(validate.call_count, 1)
        folders.get_user_directory = lambda: str(self.base/'import-target')
        db.init()
        with patch.object(service, 'compact_report', side_effect=OSError('no disk space')):
            with self.assertRaises(OSError):
                archives.import_archive(path, self.base/'failed-report.json')
        self.assertEqual(db.list_prompts()['total'], 0)

    def test_interrupted_upload_releases_reservation_and_file(self):
        import test_routes as adapters
        class Content:
            async def iter_chunked(self, size):
                yield b'partial upload'
                raise ConnectionResetError('disconnected')
        request = adapters.Request(); request.content = Content()
        with self.assertRaises(ConnectionResetError):
            adapters.RouteTests.run_async(self, adapters.routes.import_preview(request))
        job = list(service._jobs.values())[-1]
        self.assertEqual(job['state'], 'failed')
        self.assertEqual(job['reserved_bytes'], 0)
        self.assertFalse(service.job_file(job['upload'], '.upload').exists())

    def test_queue_completed_jobs_survive_restart_and_prune(self):
        finished = threading.Event()
        jid = service.submit('backup', lambda jid: (finished.set() or {'id': 'test'}))
        self.assertTrue(finished.wait(1))
        service.stop(); scanner._stopping = False
        service._jobs.clear(); service.reconcile()
        self.assertEqual(service.job(jid)['state'], 'complete')
        with service._job_lock:
            service._jobs[jid]['finished_at'] = 0
            service.prune()
        self.assertNotIn(jid, service._jobs)
        self.assertFalse(service.job_file(jid, '.job').exists())

    def test_corrupt_recovery_preview_rejected(self):
        self.image(self.output/'one.png'); scanner._scan(False)
        backup = archives.create_backup(); path = archives.backup_path(backup['id'])
        with zipfile.ZipFile(path) as z: members = {n: z.read(n) for n in z.namelist()}
        name = next(n for n in members if n.startswith('thumbs/'))
        members[name] = b'corrupt'
        with zipfile.ZipFile(path, 'w') as z:
            for n, v in members.items(): z.writestr(n, v)
        with self.assertRaisesRegex(ValueError, 'checksum'):
            archives.restore_backup(backup['id'])
        self.assertEqual(db.list_prompts()['total'], 1)
