"""Audit a real outputs folder without changing its files or the live library.

Usage: python tools/audit_outputs.py output --report /tmp/prompt-library-report.json
"""
import argparse
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import types


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    parser.add_argument('--report', type=Path)
    parser.add_argument('--roundtrip', action='store_true', help='Verify export/import and backup/restore in temporary libraries')
    args = parser.parse_args()
    source = Path(__file__).resolve().parents[1]
    output = args.output.resolve()
    if not output.is_dir():
        parser.error('Output folder does not exist')
    with tempfile.TemporaryDirectory(prefix='prompt-library-audit-') as temporary:
        folders = types.ModuleType('folder_paths')
        folders.get_output_directory = lambda: str(output)
        folders.get_user_directory = lambda: temporary
        sys.modules['folder_paths'] = folders
        package = types.ModuleType('_prompt_library_audit')
        package.__path__ = [str(source)]
        sys.modules[package.__name__] = package
        modules = {}
        for name in ('extract', 'db', 'scanner'):
            spec = importlib.util.spec_from_file_location(f'{package.__name__}.{name}', source / f'{name}.py')
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            setattr(package, name, module)
            modules[name] = module
        db, scanner = modules['db'], modules['scanner']
        db.init()
        # Read actual metadata and exercise thumbnail creation in the temporary library.
        scanner._scan(True)
        from importlib import import_module
        import_module(f'{package.__name__}.library')
        with db.db() as conn:
            workflow_count = conn.execute('SELECT COUNT(*) FROM workflows').fetchone()[0]
            asset_count = conn.execute('SELECT COUNT(*) FROM assets').fetchone()[0]
        report = {'workflow_count': workflow_count, 'asset_count': asset_count, 'scan': scanner.get_status(), 'files': db.scan_report(),
                  'prompt_count': db.list_prompts()['total']}
        if args.roundtrip:
            archives = import_module(f'{package.__name__}.archives')
            archive = Path(temporary)/'export.zip'
            archives.export_archive(archive)
            backup = archives.create_backup()
            db.save_prompt({'positive':'temporary round-trip verification marker'})
            archives.restore_backup(backup['id'])
            assert db.list_prompts()['total'] == report['prompt_count'], 'Restore count mismatch'
            folders.get_user_directory = lambda: str(Path(temporary)/'import-target')
            db.init()
            archives.import_archive(archive)
            archives.import_archive(archive)
            assert db.list_prompts()['total'] == report['prompt_count'], 'Repeated import count mismatch'
            with db.db() as conn:
                assert conn.execute('SELECT COUNT(*) FROM workflows').fetchone()[0] == workflow_count
                assert conn.execute('SELECT COUNT(*) FROM assets').fetchone()[0] == asset_count
                assert conn.execute('SELECT COUNT(*) FROM assets WHERE trusted=1').fetchone()[0] == 0
            report['archive_roundtrip'] = 'passed'
        print(json.dumps({'workflow_count': workflow_count, 'asset_count': asset_count, 'roundtrip': report.get('archive_roundtrip'), 'prompt_count': report['prompt_count'], 'counts': report['scan']['counts'],
                          'error': report['scan']['error']}, indent=2))
        if args.report:
            args.report.write_text(json.dumps(report, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
