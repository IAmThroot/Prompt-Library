"""Run with python -m unittest discover -s tests -v; no ComfyUI install needed."""
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import shutil
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = '_prompt_library_tests'
package = types.ModuleType(PACKAGE)
package.__path__ = [str(ROOT)]
sys.modules[PACKAGE] = package
folders = types.ModuleType('folder_paths')
sys.modules['folder_paths'] = folders


def load(name):
    spec = importlib.util.spec_from_file_location(f'{PACKAGE}.{name}', ROOT / f'{name}.py')
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    setattr(package, name, module)
    return module


extract = load('extract')
db = load('db')
scanner = load('scanner')


def node(cls, **inputs):
    return {'class_type': cls, 'inputs': inputs}


def graph(positive='landscape'):
    return {'1': node('CLIPTextEncode', text=positive),
            '2': node('CLIPTextEncode', text='blur'),
            '3': node('KSampler', positive=['1', 0], negative=['2', 0], seed=42),
            '4': node('VAEDecode', samples=['3', 0]),
            '5': node('SaveImage', images=['4', 0], filename_prefix='sample')}


class ExtractionTests(unittest.TestCase):
    def test_sam3_is_auxiliary(self):
        g = graph()
        g['1'] = node('MiniMaxH3ReferenceToVideo', prompt=['6', 0])
        g['6'] = node('PrimitiveStringMultiline', value='main video prompt')
        g['7'] = node('CLIPTextEncode', text='object label')
        g['8'] = node('SAM3_VideoTrack', conditioning=['7', 0], images=['4', 0])
        self.assertEqual(extract.extract_prompts(g)['positive'], 'main video prompt')
        self.assertIsNone(extract.extract_prompts({'7': g['7'], '8': g['8']}))

    def test_director_through_guide(self):
        g = {'1': node('MiniMaxH3Director', prompt='director prompt'),
             '2': node('MiniMaxH3DirectorGuide', guide=['1', 0]),
             '3': node('BasicGuider', conditioning=['2', 0])}
        self.assertEqual(extract.extract_prompts(g)['positive'], 'director prompt')

    def test_adapters(self):
        for cls, key in extract.PROMPT_FIELDS.items():
            with self.subTest(cls=cls):
                g = graph(); g['1'] = node(cls, **{key[0]: 'main text'})
                self.assertEqual(extract.extract_prompts(g)['positive'], 'main text')

    def test_same_text_stages_keep_separate_settings(self):
        g=graph();g['6']=node('KSampler',positive=['1',0],negative=['2',0],seed=123)
        g['7']=node('SaveImage',images=['6',0])
        r=extract.extract_prompts(g)
        self.assertEqual(len(r['stages']),2)
        self.assertEqual({s['meta']['seed'] for s in r['stages']},{'42','123'})

    def test_custom_sampler_settings_include_noise_and_scheduler(self):
        g = {'1':node('CLIPTextEncode',text='main'),
             '2':node('BasicGuider',conditioning=['1',0]),
             '3':node('RandomNoise',noise_seed=18446744073709551615),
             '4':node('BasicScheduler',steps=30),
             '5':node('SamplerCustomAdvanced',guider=['2',0],noise=['3',0],sigmas=['4',0])}
        r=extract.extract_prompts(g)
        self.assertEqual(r['meta']['seed'],'18446744073709551615')
        self.assertEqual(r['meta']['steps'],30)

    def test_static_switch_selects_one_branch(self):
        g=graph();g['6']=node('CLIPTextEncode',text='other')
        g['7']=node('ComfySwitchNode',switch=False,on_false=['1',0],on_true=['6',0])
        g['3']['inputs']['positive']=['7',0]
        self.assertEqual(extract.extract_prompts(g)['positive'],'landscape')

    def test_zero_conditioning(self):
        g = graph(); g['2'] = node('ConditioningZeroOut', conditioning=['1', 0])
        self.assertEqual(extract.extract_prompts(g)['negative'], '')

    def test_controlnet_slots(self):
        g = graph();g['6'] = node('ControlNetApplyAdvanced', positive=['1',0],negative=['2',0])
        g['3']['inputs'].update(positive=['6',0],negative=['6',1])
        r = extract.extract_prompts(g)
        self.assertEqual((r['positive'], r['negative']), ('landscape','blur'))

    def test_paired_stages_and_settings(self):
        g = graph(); g.update({'10': node('CLIPTextEncode', text='second'),
            '11': node('CLIPTextEncode', text='second negative'),
            '12': node('KSampler', positive=['10',0], negative=['11',0], seed=99),
            '13': node('SaveImage', images=['12',0], filename_prefix='other')})
        r = extract.extract_prompts(g, 'other_00001.png')
        self.assertEqual(r['positive'],'second');self.assertEqual(r['meta']['seed'],'99')
        r = extract.extract_prompts(g)
        self.assertEqual(len(r['stages']),2)
        self.assertEqual(r['stages'][1]['negative'],'second negative')
        self.assertTrue(r['warnings'])

    def test_json_comment_plain_prompt(self):
        r = extract.extract_from_metadata({'comment': json.dumps({'prompt':'plain text','negative_prompt':'bad'})})
        self.assertEqual((r['positive'],r['negative']),('plain text','bad'))

    def test_h3_plan_order_and_prefix(self):
        plan = {'prompt_prefix':['style'],'shots':[{'prompt':['first','shot']},{'prompt':'second'}]}
        r=extract.extract_from_metadata({'h3_plan':json.dumps({'editor_plan':plan})})
        self.assertEqual(r['positive'],'style\nfirst\nshot')
        self.assertEqual(r['extra'][0]['text'],'style\nsecond')

    def test_placeholder_rejected_and_fallback_works(self):
        self.assertIsNone(extract.parse_a1111('positive_prompt_not_found\nNegative prompt: negative_prompt_not_found\nSteps: 0'))
        r=extract.extract_from_metadata({'prompt':json.dumps({'1':node('Unknown')}),'parameters':'real text\nSteps: 20'})
        self.assertEqual(r['positive'],'real text')

    def test_standalone_library(self):
        r=extract.extract_prompts({'1':node('PromptLibrary',positive='yes',negative='no')})
        self.assertEqual((r['positive'],r['negative']),('yes','no'))

    def test_unknown_string_not_guessed(self):
        g=graph();g['1']['inputs']['text']=['6',0];g['6']=node('TransformString',text='input')
        r=extract.extract_prompts(g)
        self.assertEqual(r['positive'],'');self.assertTrue(any('Unresolved' in w for w in r['warnings']))

    def test_cycles_terminate(self):
        g={'1':node('BasicGuider',conditioning=['2',0]),'2':node('Pass',conditioning=['2',0])}
        self.assertIsNone(extract.extract_prompts(g))

    def test_workflow_fallback(self):
        wf={'nodes':[{'id':1,'type':'CLIPTextEncode','widgets_values':['editor text']}],'links':[]}
        r=extract.extract_from_metadata({'workflow':json.dumps(wf)})
        self.assertEqual(r['positive'],'editor text');self.assertTrue(r['warnings'])

    def test_string_slots_and_concat(self):
        nodes={'1':node('PrimitiveStringMultiline',value='a'), '2':node('StringConcatenate',string_a=['1',0],string_b='b',delimiter=':')}
        self.assertEqual(extract.resolve_text(nodes,['2',0]),'a:b')
        self.assertIsNone(extract.resolve_text(nodes,['1',1]))


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.base=Path(self.temp.name)
        self.output=self.base/'output';self.output.mkdir()
        folders.get_output_directory=lambda:str(self.output)
        folders.get_user_directory=lambda:str(self.base/'user')
        db.init()

    def tearDown(self):
        self.temp.cleanup()

    def save(self,text='main',**kwargs):
        return db.save_prompt({'positive':text,**kwargs})[0]

    def file(self,path='a/b/test.png'):
        p=self.output/path;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(b'example')
        os.utime(p,(time.time()-10,)*2)
        return p

    def test_distinct_extra_and_generation_settings(self):
        a=self.save(extra=[{'role':'positive','text':'scene one'}],meta={'seed':1})
        b=self.save(extra=[{'role':'positive','text':'scene two'}],meta={'seed':2})
        self.assertNotEqual(a,b)
        with db.db() as conn:
            for i in [1,2]:db.record_file(conn,f'{i}.png',a,'image',i,10,result={'positive':'main','meta':{'seed':i}})
        self.assertEqual(len(db.generation_history(a)),2)
        self.assertEqual(db.list_prompts(search='scene one')['total'],1)
        self.save('other',extra=[{'role':'positive','text':'नमस्ते'}])
        self.assertEqual(db.list_prompts(search='नमस्ते')['total'],1)

    def test_overwritten_filename_keeps_original_prompt(self):
        with db.db() as conn:
            a,_=db.upsert_prompt(conn,{'positive':'first generation'},False)
            db.record_file(conn,'same.png',a,'image',1,1)
            b,_=db.upsert_prompt(conn,{'positive':'second generation'},False)
            db.record_file(conn,'same.png',b,'image',2,2)
        self.assertEqual(db.list_prompts()['total'],2)
        with db.db() as conn:
            db.record_file(conn,'same.png',None,'image',3,3,state='no_metadata')
        self.assertIsNone(db.known_files()['same.png']['prompt_id'])
        self.assertEqual(db.list_prompts()['total'],2)

    def test_edited_copy_keeps_source(self):
        a=self.save();
        with db.db() as conn:db.record_file(conn,'a.png',a,'image',1,1)
        b=db.update_prompt(a,{'positive':'edited','title':'copy'})
        self.assertNotEqual(a,b);self.assertEqual(db.known_files()['a.png']['prompt_id'],a)
        self.assertEqual(db.list_prompts()['total'],2)

    def test_manual_prompt_edits_in_place(self):
        pid=self.save('original')
        self.assertEqual(db.update_prompt(pid,{'positive':'edited'}),pid)
        self.assertEqual(db.list_prompts()['total'],1)
        self.assertEqual(db.list_prompts()['items'][0]['positive'],'edited')

    def test_duplicate_notes_and_details(self):
        a=self.save(warnings=['review'],stages=[{'positive':'stage'}]);db.save_prompt({'positive':'main'},title='new title',tags='new tag',notes='new note')
        r=db.list_prompts()['items'][0]
        self.assertEqual(r['notes'],'new note');self.assertEqual(r['warnings'],['review'])
        self.assertEqual(r['title'],'new title');self.assertEqual(r['tags'],'new tag')

    def test_real_png_metadata_and_thumbnail(self):
        from PIL import Image, PngImagePlugin
        p=self.output/'sample.png'
        metadata=PngImagePlugin.PngInfo();metadata.add_text('prompt',json.dumps(graph()))
        Image.new('RGB',(8,8)).save(p,pnginfo=metadata)
        os.utime(p,(time.time()-10,)*2)
        scanner._scan(False)
        row=db.list_prompts()['items'][0]
        self.assertEqual(row['positive'],'landscape');self.assertTrue(row['has_thumb'])
        p.unlink();scanner._scan(False)
        self.assertEqual(db.list_prompts()['total'],1)
        self.assertTrue(db.prompt_files(row['id'])[0]['missing'])

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg unavailable')
    def test_video_thumbnail_backfill_and_corrupt_cache_repair(self):
        from PIL import Image
        p=self.output/'video.mp4'
        subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','color=c=blue:s=64x48:r=10',
                        '-t','0.2','-c:v','mpeg4','-metadata',
                        'comment='+json.dumps({'prompt':'video scene'}),str(p)],check=True,capture_output=True)
        os.utime(p,(time.time()-10,)*2)
        scanner._scan(False)
        row=db.list_prompts()['items'][0]
        self.assertTrue(row['has_thumb'])
        thumbnail=Path(db.thumb_path(row['id']))
        with Image.open(thumbnail) as image:
            self.assertLessEqual(max(image.size),256)
            self.assertAlmostEqual(image.width/image.height,64/48,places=1)
        thumbnail.unlink()
        # The second scan skips metadata but still repairs the missing preview.
        with patch.object(extract,'read_media',side_effect=AssertionError('Unchanged metadata reread')):
            scanner._scan(False)
        self.assertTrue(db.valid_thumb(thumbnail))
        previous=db.list_prompts()['items'][0]['thumb_version']
        thumbnail.write_bytes(b'broken cache')
        self.assertFalse(db.list_prompts()['items'][0]['has_thumb'])
        scanner._scan(False)
        repaired=db.list_prompts()['items'][0]
        self.assertTrue(repaired['has_thumb'])
        self.assertNotEqual(previous,repaired['thumb_version'])
        p.unlink()
        self.assertIsNone(db.ensure_thumb(row['id'],str(p),'video'))
        self.assertTrue(db.valid_thumb(thumbnail))

    def test_thumbnail_decode_failure_reported_and_no_partial_file(self):
        self.file('bad.mp4')
        with patch.object(extract,'read_media',return_value=({'parameters':'video scene'},'video',None)), \
             patch.object(db,'_video_frame',side_effect=RuntimeError('Decoder unavailable')):
            scanner._scan(False)
        self.assertEqual(scanner.status['counts']['thumbnail_failed'],1)
        self.assertTrue(any('Decoder unavailable' in x['reason'] for x in scanner.status['issues']))
        row=db.list_prompts()['items'][0]
        self.assertFalse(row['has_thumb'])
        self.assertEqual(list(Path(db.data_dir(),'thumbs').iterdir()),[])

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg unavailable')
    def test_real_audio_comment(self):
        p=self.output/'sound.flac'
        subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','anullsrc=r=8000:cl=mono','-t','0.01',
                        '-metadata','comment='+json.dumps({'prompt':'audio prompt'}),str(p)],check=True,capture_output=True)
        meta,kind,error=extract.read_media(str(p),'audio')
        self.assertIsNone(error);self.assertEqual(kind,'audio')
        self.assertEqual(extract.extract_from_metadata(meta)['positive'],'audio prompt')

    def test_hidden_directory_not_misreported_as_missing(self):
        self.file('.hidden/a.png')
        with patch.object(extract,'read_media',return_value=({'parameters':'prompt'},'image',None)):
            scanner._scan(False,include_hidden=True)
            scanner._scan(False,include_hidden=False)
        row=db.known_files()['.hidden/a.png'];self.assertEqual(row['missing'],0)

    def test_root_collision_and_missing_persistence(self):
        a=self.save()
        with db.db() as conn:
            db.record_file(conn,'same.png',a,'image',1,1,root='root1')
            db.record_file(conn,'same.png',a,'image',2,2,root='root2')
        db.finish_scan([],root='root1')
        files=db.prompt_files(a);self.assertEqual(len(files),2)
        self.assertEqual(sum(f['missing'] for f in files),1)
        self.assertEqual(db.list_prompts()['total'],1)

    def test_migration_backup_and_annotations(self):
        path=db._db_path
        # Create a genuine v1 database in a separate user directory.
        legacy=self.base/'legacy';legacy.mkdir();folders.get_user_directory=lambda:str(legacy)
        directory=legacy/'prompt_library';directory.mkdir();path=directory/'library.db'
        with sqlite3.connect(path) as conn:
            conn.executescript(db.SCHEMA)
            conn.execute("INSERT INTO prompts(hash,positive,title,tags,notes,favorite,deleted,created_at,updated_at,last_seen) VALUES (?,?,?,?,?,1,1,1,1,1)",
                         (extract.prompt_hash('old',''),'old','title','tag','notes'))
            conn.execute("INSERT INTO files VALUES ('a.png',1,'image',1,1,0)")
        conn.close()
        db.init();self.assertTrue(Path(str(path)+'.before-v2.bak').exists())
        with db.db() as conn:
            row=conn.execute('SELECT * FROM prompts').fetchone();self.assertEqual(row['notes'],'notes');self.assertEqual(row['deleted'],1)
        self.assertEqual(db.known_files()['a.png']['extractor'],0)
        db.init()  # idempotent

    def test_recursive_scan_and_retry_after_defer(self):
        p=self.file();os.utime(p,None)
        with patch.object(extract,'read_media',return_value=({'parameters':'prompt'},'image',None)):
            delay=scanner._scan(False);self.assertIsNotNone(delay)
            self.assertEqual(db.known_files()['a/b/test.png']['status'],'deferred')
            os.utime(p,(time.time()-10,)*2);scanner._scan(False)
        self.assertEqual(db.list_prompts()['total'],1)

    def test_failed_read_retries_and_keeps_previous(self):
        self.file()
        with patch.object(extract,'read_media',return_value=({'parameters':'prompt'},'image',None)):scanner._scan(False)
        pid=db.known_files()['a/b/test.png']['prompt_id']
        with patch.object(extract,'read_media',return_value=({},'image','temporary failure')):scanner._scan(True)
        self.assertEqual(db.known_files()['a/b/test.png']['prompt_id'],pid)
        with db.db() as conn:conn.execute('UPDATE files SET retry_at=0')
        with patch.object(extract,'read_media',return_value=({'parameters':'prompt'},'image',None)):scanner._scan(False)
        self.assertEqual(db.known_files()['a/b/test.png']['status'],'duplicate')

    def test_extractor_upgrade_repairs_sam_and_keeps_annotation(self):
        self.file();a=self.save('SAM label');db.update_prompt(a,{'notes':'important'})
        with db.db() as conn:
            db.record_file(conn,'a/b/test.png',a,'image',os.stat(self.output/'a/b/test.png').st_mtime,7)
            conn.execute('UPDATE files SET extractor=0')
        with patch.object(extract,'read_media',return_value=({'parameters':'correct main'},'image',None)):scanner._scan(False)
        self.assertNotEqual(db.known_files()['a/b/test.png']['prompt_id'],a)
        row=next(r for r in db.list_prompts()['items'] if r['positive']=='correct main')
        self.assertEqual(row['notes'],'important')

    def test_removed_source_stays_hidden_after_repair(self):
        self.file();a=self.save('SAM label');db.delete_prompt(a)
        with db.db() as conn:db.record_file(conn,'a/b/test.png',a,'image',(self.output/'a/b/test.png').stat().st_mtime,7)
        with patch.object(extract,'read_media',return_value=({'parameters':'correct main'},'image',None)):scanner._scan(True)
        self.assertEqual(db.list_prompts()['total'],0)
        self.assertEqual(db.known_files()['a/b/test.png']['status'],'hidden')

    def test_reused_deleted_filename_can_import_new_prompt(self):
        self.file();a=self.save('removed prompt');db.delete_prompt(a)
        with db.db() as conn:db.record_file(conn,'a/b/test.png',a,'image',1,7)
        with patch.object(extract,'read_media',return_value=({'parameters':'new generation'},'image',None)):scanner._scan(False)
        self.assertEqual(db.list_prompts()['total'],1)
        self.assertEqual(db.list_prompts()['items'][0]['positive'],'new generation')

    def test_force_request_queued(self):
        with patch.dict(scanner.status,{'running':True}):
            scanner._pending=scanner._pending_force=False
            self.assertFalse(scanner.start_scan(force=True))
            self.assertTrue(scanner._pending_force)
            scanner.start_scan(force=False)
            self.assertTrue(scanner._pending_force)
        scanner._pending=scanner._pending_force=False

    def test_avif_discovered(self):
        self.file('clip.avif')
        with patch.object(extract,'read_media',return_value=({},'video',None)):scanner._scan(False)
        self.assertEqual(db.known_files()['clip.avif']['media_type'],'video')


if __name__ == '__main__':
    unittest.main()
