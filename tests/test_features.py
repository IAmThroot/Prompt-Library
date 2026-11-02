import json
import os
from pathlib import Path
import sqlite3
import time
import unittest
from unittest.mock import patch
import zipfile

import test_library as base
from test_library import db, extract, scanner, folders, graph, load

library = load('library')
archives = load('archives')
service = load('service')
# scanner imports the package modules during initial loading; retain shared synchronization state.
scanner.library = library
scanner.service = service


class FeatureTests(unittest.TestCase):
    setUp = base.StorageTests.setUp
    tearDown = base.StorageTests.tearDown
    save = base.StorageTests.save
    file = base.StorageTests.file
    def test_workflow_only_and_distinct_workflows(self):
        first = library.save_workflow(graph(), {'nodes': [], 'version':0.4})
        second = library.save_workflow(graph(), {'nodes': [], 'version':0.4, 'extra':{'label':'second'}})
        self.assertEqual(first['id'],second['id'])
        self.assertEqual(len(library.workflows(first['id'])),2)
        empty = library.save_workflow(None,{'nodes':[]})
        self.assertTrue(empty['workflow_only'])
        self.assertEqual(library.save_workflow(None,{'nodes':[]})['id'],empty['id'])
        self.assertEqual(library.workflow(empty['workflow_id']),{'nodes':[]})

    def test_collections_bulk_all_matching(self):
        a=self.save('one',extra=[{'role':'other','text':'match'}]);b=self.save('two',extra=[{'role':'other','text':'match'}]);self.save('third')
        c=library.collection_save('Project')
        self.assertEqual(library.bulk({'filters':{'search':'match'}},'collection_add',c)['count'],2)
        self.assertEqual(db.list_prompts(collection=c)['total'],2)
        library.bulk({'ids':[a,b]},'tag_add','red, red, blue')
        library.bulk({'ids':[a]},'tag_remove','red')
        self.assertEqual(db.list_prompts(search='red')['total'],1)
        library.bulk({'filters':{'collection':c}},'favorite',True)
        self.assertEqual(db.list_prompts(flt='favorites')['total'],2)
        library.collection_delete(c)
        self.assertEqual(db.list_prompts()['total'],3)

    def test_gallery_revisions_trust_and_overwrite(self):
        p=self.file('image.png');stat=p.stat();pid=self.save()
        with db.db() as conn:
            aid=library.asset(conn,str(self.output),p.name,stat.st_mtime,stat.st_size,'image',True)
            db.record_file(conn,p.name,pid,'image',stat.st_mtime,stat.st_size,result={'positive':'main'})
            conn.execute('INSERT INTO generations(prompt_id,root,path,mtime,size,extractor,snapshot,created_at,asset_id) VALUES (?,?,?,?,?,?,?,?,?)',
                         (pid,str(self.output),p.name,stat.st_mtime,stat.st_size,0,'{}',time.time(),aid))
        gallery=library.gallery(pid)
        self.assertEqual(gallery['total'],1);self.assertTrue(gallery['items'][0]['available'])
        p.write_bytes(b'different file')
        self.assertFalse(library.gallery(pid)['items'][0]['available'])

    def test_archive_roundtrip_and_repeated_import_conflict(self):
        result=library.save_workflow(graph(),{'nodes':[]});pid=result['id']
        db.update_prompt(pid,{'notes':'original','title':'original title','tags':'tag'})
        cid=library.collection_save('Project');library.bulk({'ids':[pid]},'collection_add',cid)
        archive=self.base/'export.zip';archives.export_archive(archive)
        self.assertEqual(archives.preview_import(archive)['duplicates'],1)
        db.update_prompt(pid,{'notes':'local edit','title':'local title','favorite':True})
        report=archives.import_archive(archive)
        self.assertTrue(report['conflicts']);self.assertEqual(db.list_prompts()['items'][0]['notes'],'local edit')
        self.assertTrue(db.list_prompts()['items'][0]['favorite'])
        archives.import_archive(archive)
        self.assertEqual(db.list_prompts()['total'],1)
        self.assertEqual(len(library.workflows(pid)),1)
        # Import into a different library, with numeric IDs already occupied.
        folders.get_user_directory=lambda:str(self.base/'other-user');db.init();self.save('unrelated')
        archives.import_archive(archive);archives.import_archive(archive)
        self.assertEqual(db.list_prompts()['total'],2)
        row=next(r for r in db.list_prompts()['items'] if r['positive']=='landscape')
        self.assertEqual(row['notes'],'original')
        self.assertEqual(len(library.workflows(row['id'])),1)
        self.assertEqual(library.collections()[0]['count'],1)

    def test_imported_media_untrusted_until_local_scan(self):
        p=self.file('x.png');pid=self.save();stat=p.stat()
        with db.db() as conn:db.record_file(conn,p.name,pid,'image',stat.st_mtime,stat.st_size,result={'positive':'main'})
        archive=self.base/'export.zip';archives.export_archive(archive)
        folders.get_user_directory=lambda:str(self.base/'other-user');db.init();archives.import_archive(archive)
        row=db.list_prompts()['items'][0]
        self.assertFalse(library.gallery(row['id'])['items'][0]['available'])
        with patch.object(extract,'read_media',return_value=({'parameters':'main'},'image',None)):scanner._scan(False)
        self.assertTrue(library.gallery(row['id'])['items'][0]['available'])

    def test_backup_restore_preserves_removed_settings_and_collections(self):
        a=self.save('visible');b=self.save('removed');db.delete_prompt(b)
        cid=library.collection_save('Keep');library.bulk({'ids':[a]},'collection_add',cid)
        library.update_settings({'capture_enabled':False})
        backup=archives.create_backup()
        self.save('later');library.collection_delete(cid);library.update_settings({'capture_enabled':True})
        result=archives.restore_backup(backup['id'])
        self.assertIn('safety_backup',result)
        self.assertEqual(db.list_prompts()['total'],1)
        self.assertEqual(library.collections()[0]['name'],'Keep')
        self.assertFalse(library.settings()['capture_enabled'])
        with db.db() as conn:self.assertEqual(conn.execute('SELECT deleted FROM prompts WHERE id=?',(b,)).fetchone()[0],1)
        self.assertEqual(len(archives.list_backups()),2)

    def test_schedule_retention_and_unchanged(self):
        library.update_settings({'backup_keep':2})
        self.save('a');manual=archives.create_backup()
        first=archives.create_backup(True);self.assertTrue(archives.create_backup(True)['unchanged'])
        self.save('b');archives.create_backup(True);self.save('c');archives.create_backup(True)
        self.assertEqual(len([r for r in archives.list_backups() if r['scheduled']]),2)
        self.assertTrue(archives.backup_path(manual['id']).exists())
        self.assertFalse((archives.directory('backups')/(first['id']+'.zip')).exists())

    def test_scheduler_without_browser_and_disabled_capture(self):
        library.update_settings({'backup_enabled':False})
        with patch.object(scanner,'start_scan') as start:
            state={};service.tick(100,state);service.tick(110,state);service.tick(130,state)
            self.assertEqual(start.call_count,2)
            self.assertTrue(start.call_args.kwargs['background'])
            library.update_settings({'capture_enabled':False});service.tick(160,state)
            self.assertEqual(start.call_count,2)

    def test_background_requests_do_not_queue(self):
        with patch.dict(scanner.status,{'running':True}):
            scanner._pending=False
            self.assertFalse(scanner.start_scan(background=True));self.assertFalse(scanner._pending)

    def test_malformed_archive_and_rollback(self):
        self.save();archive=self.base/'export.zip';archives.export_archive(archive)
        bad=self.base/'bad.zip'
        with zipfile.ZipFile(archive) as source,zipfile.ZipFile(bad,'w') as target:
            for name in source.namelist():target.writestr(name,source.read(name))
            target.writestr('../escape','bad')
        with self.assertRaises(ValueError):archives.validate(bad)
        result=library.save_workflow(graph(),{'nodes':[]});archives.export_archive(archive)
        with db.db() as conn:conn.execute('DELETE FROM prompt_workflows');conn.execute('DELETE FROM workflows')
        count=db.list_prompts()['total']
        with patch.object(library,'store_workflow',side_effect=RuntimeError('interrupted')):
            with self.assertRaises(RuntimeError):archives.import_archive(archive)
        self.assertEqual(db.list_prompts()['total'],count)

    def test_actual_service_capture_and_shutdown(self):
        from PIL import Image,PngImagePlugin
        import os
        library.update_settings({'backup_enabled':False})
        p=self.output/'background.png';meta=PngImagePlugin.PngInfo();meta.add_text('prompt',json.dumps(graph()))
        Image.new('RGB',(4,4)).save(p,pnginfo=meta);os.utime(p,(time.time()-10,)*2)
        try:
            service.start();thread=service._thread;service.start();self.assertIs(service._thread,thread)
            for _ in range(100):
                if db.list_prompts()['total']:break
                time.sleep(.02)
            self.assertEqual(db.list_prompts()['total'],1)
        finally:
            service.stop();scanner._stopping=False
        self.assertFalse(thread.is_alive())
        self.assertFalse(scanner.status['running'])

    def test_restore_failure_rolls_back_database_and_previews(self):
        from PIL import Image
        pid=self.save('before')
        Image.new('RGB',(8,8),'red').save(db.thumb_path(pid),'JPEG')
        backup=archives.create_backup()
        self.save('after')
        original=Path.write_bytes
        failed=[]
        def fail_once(path,content):
            if path.suffix=='.jpg' and not failed:
                failed.append(True);raise OSError('simulated full disk')
            return original(path,content)
        with patch.object(Path,'write_bytes',fail_once):
            with self.assertRaises(OSError):archives.restore_backup(backup['id'])
        self.assertEqual(db.list_prompts()['total'],2)
        self.assertTrue(db.valid_thumb(db.thumb_path(pid)))

    def test_backup_scheduler_catches_up_once(self):
        library.update_settings({'capture_enabled':False})
        with patch.object(service,'submit',return_value='job') as submit:
            state={};service.tick(time.time(),state);service.tick(time.time()+1,state)
            self.assertEqual(submit.call_count,1)
            self.assertEqual(submit.call_args.args[0],'scheduled_backup')

    def test_workflow_backfill_from_existing_metadata(self):
        from PIL import Image,PngImagePlugin
        p=self.output/'snapshot.png';meta=PngImagePlugin.PngInfo()
        meta.add_text('prompt',json.dumps(graph()));meta.add_text('workflow',json.dumps({'nodes':[]}))
        Image.new('RGB',(8,8)).save(p,pnginfo=meta);stat=p.stat();pid=self.save('landscape',negative='blur')
        with db.db() as conn:
            db.record_file(conn,p.name,pid,'image',stat.st_mtime,stat.st_size,result={'positive':'landscape'})
            conn.execute('UPDATE files SET extractor=2')
        import os
        os.utime(p,(time.time()-10,)*2)
        scanner._scan(False)
        self.assertEqual(len(library.workflows(pid)),1)
        p.unlink();self.assertEqual(library.workflow(library.workflows(pid)[0]['id']),{'nodes':[]})

    def test_migration_v1_and_v2_comprehensive(self):
        from PIL import Image
        legacy = self.base / 'legacy_v1_v2'
        legacy.mkdir()
        folders.get_user_directory = lambda: str(legacy)
        directory = legacy / 'prompt_library'
        directory.mkdir()
        thumbs_dir = directory / 'thumbs'
        thumbs_dir.mkdir()
        db_file = directory / 'library.db'

        # 1. Test v1 schema migration with annotations, deleted entries, missing files, legacy thumbs
        with sqlite3.connect(db_file) as conn:
            conn.executescript(db.SCHEMA)
            # prompt 1: annotated, active
            conn.execute('''INSERT INTO prompts(hash,positive,negative,title,tags,notes,favorite,deleted,created_at,updated_at,last_seen)
                            VALUES (?,?,?,?,?,?,1,0,1,1,1)''',
                         (extract.prompt_hash('annotated prompt',''), 'annotated prompt', '', 'My Title', 'tag1, tag2', 'Special notes'))
            # prompt 2: removed entry
            conn.execute('''INSERT INTO prompts(hash,positive,negative,title,tags,notes,favorite,deleted,created_at,updated_at,last_seen)
                            VALUES (?,?,?,?,?,?,0,1,1,1,1)''',
                         (extract.prompt_hash('deleted prompt',''), 'deleted prompt', '', '', '', ''))
            # files: one present, one missing
            conn.execute("INSERT INTO files VALUES ('present.png', 1, 'image', 100, 200, 0)")
            conn.execute("INSERT INTO files VALUES ('gone.png', 2, 'image', 100, 200, 1)")
        conn.close()

        Image.new('RGB', (32, 32), color='blue').save(thumbs_dir / '1.jpg', 'JPEG')

        db.init()
        self.assertTrue((directory / 'library.db.before-v2.bak').exists())
        with db.db() as conn:
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], 3)
            p1 = conn.execute('SELECT * FROM prompts WHERE id=1').fetchone()
            self.assertEqual(p1['title'], 'My Title')
            self.assertEqual(p1['tags'], 'tag1, tag2')
            self.assertEqual(p1['notes'], 'Special notes')
            self.assertEqual(p1['favorite'], 1)
            self.assertEqual(p1['deleted'], 0)
            self.assertTrue(p1['uuid'] and len(p1['uuid']) == 32)

            p2 = conn.execute('SELECT * FROM prompts WHERE id=2').fetchone()
            self.assertEqual(p2['deleted'], 1)

            f1 = conn.execute("SELECT * FROM files WHERE path='present.png'").fetchone()
            self.assertEqual(f1['missing'], 0)
            f2 = conn.execute("SELECT * FROM files WHERE path='gone.png'").fetchone()
            self.assertEqual(f2['missing'], 1)

        self.assertTrue(db.valid_thumb(str(thumbs_dir / '1.jpg')))
        prompts = db.list_prompts()['items']
        self.assertEqual(len(prompts), 1)
        self.assertTrue(prompts[0]['has_thumb'])

        # 2. Upgrade a self-contained v2 database, independent of the installed ComfyUI library.
        v2_dir = self.base / 'legacy_v2'
        v2_pl = v2_dir / 'prompt_library'
        v2_pl.mkdir(parents=True)
        v2_file = v2_pl / 'library.db'
        root = str(self.output)
        with sqlite3.connect(v2_file) as conn:
            conn.executescript(db.SCHEMA)
            conn.execute('DROP TABLE files')
            conn.execute('''CREATE TABLE files (
                root TEXT NOT NULL, path TEXT NOT NULL, prompt_id INTEGER,
                media_type TEXT NOT NULL, mtime REAL NOT NULL, size INTEGER NOT NULL,
                missing INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'pending',
                error TEXT, extractor INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
                retry_at REAL NOT NULL DEFAULT 0, PRIMARY KEY(root,path))''')
            conn.execute('''CREATE TABLE generations (
                id INTEGER PRIMARY KEY, prompt_id INTEGER NOT NULL, root TEXT NOT NULL,
                path TEXT NOT NULL, mtime REAL NOT NULL, size INTEGER NOT NULL,
                extractor INTEGER NOT NULL, snapshot TEXT NOT NULL, created_at REAL NOT NULL,
                UNIQUE(root,path,mtime,size,extractor))''')
            conn.execute('CREATE TABLE prompt_details (prompt_id INTEGER PRIMARY KEY, details TEXT NOT NULL)')
            conn.execute('''INSERT INTO prompts(hash,positive,title,notes,deleted,created_at,updated_at,last_seen)
                VALUES (?,?,?,?,0,1,1,1)''',
                (extract.prompt_hash('v2 prompt', ''), 'v2 prompt', 'V2 title', 'V2 notes'))
            conn.execute('''INSERT INTO prompts(hash,positive,deleted,created_at,updated_at,last_seen)
                VALUES (?,?,1,1,1,1)''', (extract.prompt_hash('v2 removed', ''), 'v2 removed'))
            conn.execute('''INSERT INTO files(root,path,prompt_id,media_type,mtime,size,missing,status,extractor)
                VALUES (?,?,?,?,?,?,0,'imported',2)''', (root, 'present.png', 1, 'image', 100, 200))
            conn.execute('''INSERT INTO files(root,path,prompt_id,media_type,mtime,size,missing,status,extractor)
                VALUES (?,?,?,?,?,?,1,'missing',2)''', (root, 'gone.png', 2, 'image', 100, 200))
            conn.execute('''INSERT INTO generations(prompt_id,root,path,mtime,size,extractor,snapshot,created_at)
                VALUES (?,?,?,?,?,?,?,?)''', (1, root, 'present.png', 100, 200, 2, '{}', 100))
            conn.execute('PRAGMA user_version=2')
        conn.close()
        folders.get_user_directory = lambda: str(v2_dir)
        db.init()
        backup = v2_pl / 'library.db.before-v3.bak'
        self.assertTrue(backup.exists())
        with sqlite3.connect(backup) as conn:
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], 2)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='assets'").fetchone()[0], 0)
        conn.close()
        with db.db() as conn:
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], 3)
            self.assertEqual(conn.execute('SELECT title FROM prompts WHERE id=1').fetchone()[0], 'V2 title')
            self.assertEqual(conn.execute('SELECT notes FROM prompts WHERE id=1').fetchone()[0], 'V2 notes')
            self.assertEqual(conn.execute('SELECT deleted FROM prompts WHERE id=2').fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT missing FROM files WHERE path='gone.png'").fetchone()[0], 1)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM assets').fetchone()[0], 1)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM generations WHERE asset_id IS NULL').fetchone()[0], 0)
        db.init()
        with db.db() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM assets').fetchone()[0], 1)

    def test_collection_merging_case_insensitive(self):
        # Create an archive with collection 'nature' (lowercase)
        other_user = self.base / 'other_user_col'
        folders.get_user_directory = lambda: str(other_user)
        db.init()
        pid = db.save_prompt({'positive': 'green forest'})[0]
        cid_remote = library.collection_save('nature')
        library.bulk({'ids': [pid]}, 'collection_add', cid_remote)
        arc_path = self.base / 'col_merge.zip'
        archives.export_archive(arc_path)

        # Switch back to main user directory and create collection 'Nature' (uppercase)
        folders.get_user_directory = lambda: str(self.base / 'user')
        db.init()
        cid_local = library.collection_save('Nature')

        archives.import_archive(arc_path)
        cols = library.collections()
        self.assertEqual(len(cols), 1)
        self.assertEqual(cols[0]['name'], 'Nature')
        self.assertEqual(cols[0]['count'], 1)

    def test_malformed_archive_extended_cases(self):
        from PIL import Image
        pid = self.save('valid for export')
        Image.new('RGB', (16, 16), color='green').save(db.thumb_path(pid), 'JPEG')
        arc_valid = self.base / 'valid_template.zip'
        archives.export_archive(arc_valid)

        # Helper to create mutated zip
        def mutate_archive(name, transform):
            dest = self.base / name
            with zipfile.ZipFile(arc_valid) as src, zipfile.ZipFile(dest, 'w') as dst:
                transform(src, dst)
            return dest

        # Absolute path in zip
        bad_abs = mutate_archive('bad_abs.zip', lambda s, d: [
            d.writestr(n, s.read(n)) for n in s.namelist()] + [d.writestr('/etc/passwd', b'root:x:0:0')])
        with self.assertRaises(ValueError):
            archives.validate(bad_abs)

        # Backslash path in zip
        bad_bs = mutate_archive('bad_bs.zip', lambda s, d: [
            d.writestr(n, s.read(n)) for n in s.namelist()] + [d.writestr('thumbs\\bad.jpg', b'data')])
        with self.assertRaises(ValueError):
            archives.validate(bad_bs)

        # Missing library.json
        bad_no_manifest = mutate_archive('bad_no_manifest.zip', lambda s, d: [
            d.writestr(n, s.read(n)) for n in s.namelist() if n != 'library.json'])
        with self.assertRaises(ValueError):
            archives.validate(bad_no_manifest)

        # Malformed JSON in library.json
        bad_json = mutate_archive('bad_json.zip', lambda s, d: [
            d.writestr(n, s.read(n) if n != 'library.json' else b'{not valid json') for n in s.namelist()])
        with self.assertRaises(Exception):
            archives.validate(bad_json)

        # Tampered prompt hash
        def tamper_hash(s, d):
            for n in s.namelist():
                if n == 'library.json':
                    data = json.loads(s.read(n))
                    data['prompts'][0]['hash'] = 'tampered_hash_value'
                    d.writestr(n, json.dumps(data))
                else:
                    d.writestr(n, s.read(n))
        bad_hash = mutate_archive('bad_hash.zip', tamper_hash)
        with self.assertRaises(ValueError):
            archives.validate(bad_hash)

        # Tampered preview (corrupted image bytes)
        def tamper_preview(s, d):
            for n in s.namelist():
                if n.startswith('thumbs/'):
                    d.writestr(n, b'not a jpeg image')
                    # Keep sha256 in manifest or mismatch
                else:
                    d.writestr(n, s.read(n))
        bad_preview = mutate_archive('bad_preview.zip', tamper_preview)
        with self.assertRaises(ValueError):
            archives.validate(bad_preview)

        # Broken generation reference
        def tamper_generation(s, d):
            for n in s.namelist():
                if n == 'library.json':
                    data = json.loads(s.read(n))
                    data['generations'].append({
                        'prompt_id': '00000000000000000000000000000000',
                        'asset_id': '00000000000000000000000000000000',
                        'root': 'root', 'path': 'path', 'mtime': 1.0, 'size': 10,
                        'extractor': 1, 'snapshot': '{}', 'created_at': 1.0, 'uuid': '11111111111111111111111111111111'
                    })
                    d.writestr(n, json.dumps(data))
                else:
                    d.writestr(n, s.read(n))
        bad_gen = mutate_archive('bad_gen.zip', tamper_generation)
        with self.assertRaises(ValueError):
            archives.validate(bad_gen)

    def test_scheduled_backup_timing_and_failure_reporting(self):
        library.update_settings({'backup_enabled': True, 'backup_interval_hours': 24, 'capture_enabled': False})
        pid = self.save('prompt for backup timing')
        schedule = {}
        now = time.time()
        service.tick(now, schedule)
        self.assertEqual(schedule['backup'], now + 24 * 3600)

        # Test failure reporting
        with patch.object(archives, 'create_backup', side_effect=RuntimeError('simulated disk failure')):
            schedule = {'backup': 0}
            service.tick(now + 10, schedule)
            time.sleep(0.05)
            with service._job_lock:
                failed = [j for j in service._jobs.values() if j['kind'] == 'scheduled_backup' and j['state'] == 'failed']
                self.assertGreater(len(failed), 0)
                for j in failed:
                    j['reported'] = True
                    service._service_state['last_error'] = j['error']
                    schedule['backup'] = time.time() + 60
            self.assertIn('simulated disk failure', service._service_state['last_error'])
            self.assertGreater(schedule['backup'], time.time())

    def test_gallery_media_types_and_unavailable_sources(self):
        from PIL import Image
        pid = self.save('gallery test')
        # Create image, video, audio mock assets
        p_img = self.file('gallery/test.png')
        p_vid = self.file('gallery/test.mp4')
        p_aud = self.file('gallery/test.flac')

        stat_img = p_img.stat()
        stat_vid = p_vid.stat()
        stat_aud = p_aud.stat()

        with db.db() as conn:
            aid_img = library.asset(conn, str(self.output), 'gallery/test.png', stat_img.st_mtime, stat_img.st_size, 'image', True)
            aid_vid = library.asset(conn, str(self.output), 'gallery/test.mp4', stat_vid.st_mtime, stat_vid.st_size, 'video', True)
            aid_aud = library.asset(conn, str(self.output), 'gallery/test.flac', stat_aud.st_mtime, stat_aud.st_size, 'audio', True)

            for aid, path, st, mtype in [(aid_img, 'gallery/test.png', stat_img, 'image'),
                                         (aid_vid, 'gallery/test.mp4', stat_vid, 'video'),
                                         (aid_aud, 'gallery/test.flac', stat_aud, 'audio')]:
                db.record_file(conn, path, pid, mtype, st.st_mtime, st.st_size, result={'positive': 'gallery test'})
                conn.execute('''INSERT INTO generations(prompt_id,root,path,mtime,size,extractor,snapshot,created_at,asset_id)
                                VALUES (?,?,?,?,?,?,?,?,?)''',
                             (pid, str(self.output), path, st.st_mtime, st.st_size, 1, json.dumps({'positive': f'{mtype} snapshot'}), time.time(), aid))

        # Create previews for each asset
        for aid in (aid_img, aid_vid, aid_aud):
            thumb_file = library.asset_thumb(aid)
            Image.new('RGB', (16, 16), color='red').save(thumb_file, 'JPEG')

        g = library.gallery(pid)
        self.assertEqual(g['total'], 3)
        for item in g['items']:
            self.assertTrue(item['available'])
            self.assertTrue(item['has_preview'])
            self.assertTrue(item['own_preview'])
            self.assertIn(item['media_type'], ('image', 'video', 'audio'))

        # Now remove the source files from disk
        p_img.unlink()
        p_vid.unlink()
        p_aud.unlink()

        g_after = library.gallery(pid)
        self.assertEqual(g_after['total'], 3)
        for item in g_after['items']:
            self.assertFalse(item['available'])
            # Previews must remain retained!
            self.assertTrue(item['has_preview'])
            self.assertTrue(item['own_preview'])

        # Verified asset_path returns None for missing sources
        self.assertIsNone(library.asset_path(aid_img))

    def test_partially_written_files_and_repeated_scan_requests(self):
        # 1. Partially written file (mtime < 2s) is deferred
        p = self.file('partial.png')
        os.utime(p, None)  # set mtime to current time
        with patch.object(extract, 'read_media', return_value=({'parameters': 'partial prompt'}, 'image', None)):
            delay = scanner._scan(False)
            self.assertIsNotNone(delay)
            self.assertEqual(db.known_files()['partial.png']['status'], 'deferred')
            self.assertIn('Waiting for file to finish writing', db.known_files()['partial.png']['error'])

            # Now simulate file write completion
            os.utime(p, (time.time() - 10,)*2)
            scanner._scan(False)
            self.assertEqual(db.known_files()['partial.png']['status'], 'imported')

        # 2. Repeated scan requests with force preservation
        with patch.dict(scanner.status, {'running': True}):
            scanner._pending = scanner._pending_force = False
            # First request with force=True
            self.assertFalse(scanner.start_scan(force=True))
            self.assertTrue(scanner._pending_force)
            # Second non-forced request must NOT downgrade _pending_force
            self.assertFalse(scanner.start_scan(force=False))
            self.assertTrue(scanner._pending_force)
            # Background request must not queue
            self.assertFalse(scanner.start_scan(background=True))
        scanner._pending = scanner._pending_force = False

    def test_package_version_and_metadata(self):
        import importlib
        init_spec = importlib.util.spec_from_file_location(f'{base.PACKAGE}.__init__', Path(__file__).resolve().parent.parent / '__init__.py')
        init_mod = importlib.util.module_from_spec(init_spec)
        init_mod.__package__ = base.PACKAGE
        init_spec.loader.exec_module(init_mod)
        self.assertEqual(init_mod.__version__, "1.1.0")
        self.assertIn("__version__", init_mod.__all__)

if __name__=='__main__':unittest.main()
