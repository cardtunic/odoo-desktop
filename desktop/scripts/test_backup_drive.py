import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlparse
import zipfile

import psycopg2
import requests

import backup_drive as backup
import test_transfer_database as transfer_tests


def response(data=None, code=200, headers=None):
    value = Mock()
    value.ok = code < 400
    value.status_code = code
    value.content = b'json'
    value.headers = headers or {}
    value.json.return_value = data or {}
    return value


class ArchiveValidationTests(unittest.TestCase):
    def test_rejects_path_traversal_symlinks_and_aliases(self):
        for filename, symlink in [('../escape', False), ('filestore/../../escape', False), ('filestore//alias', False), ('filestore/link', True)]:
            with self.subTest(filename=filename), tempfile.TemporaryDirectory(dir='/tmp/opencode') as directory:
                root = Path(directory)
                archive = root / 'unsafe.zip'
                with zipfile.ZipFile(archive, 'w') as stream:
                    entry = zipfile.ZipInfo(filename)
                    if symlink:
                        entry.external_attr = (stat.S_IFLNK | 0o777) << 16
                    stream.writestr(entry, 'unsafe')
                with self.assertRaises(ValueError):
                    backup.unpack_archive(archive, root / 'unpacked')
                self.assertFalse((root / 'unpacked').exists())

    def test_checksums_are_required_and_checked(self):
        with tempfile.TemporaryDirectory(dir='/tmp/opencode') as directory:
            root = Path(directory)
            archive = root / 'broken.zip'
            manifest = dict(format='odoo-desktop-backup', format_version=1, odoo_version='20.0',
                filestore_included=True, sha256={'dump.sql':hashlib.sha256(b'original').hexdigest()})
            with zipfile.ZipFile(archive, 'w') as stream:
                stream.writestr('manifest.json', json.dumps(manifest))
                stream.writestr('dump.sql', 'changed')
            with self.assertRaisesRegex(ValueError, 'checksum'):
                backup.unpack_archive(archive, root / 'unpacked')


class GoogleTests(unittest.TestCase):
    def test_browser_does_not_inherit_appimage_library_or_python_settings(self):
        with patch.dict(os.environ, {'PYTHONHOME':'unsafe', 'LD_LIBRARY_PATH':'unsafe', 'APPDIR':'/tmp/test-appimage', 'XAUTHORITY':'/run/user/1000/test-auth', 'DESKTOP_SESSION':'plasma', 'XDG_DATA_DIRS':'/tmp/test-appimage/usr/share:/usr/share'}), \
             patch.object(backup.subprocess, 'run') as run:
            backup.open_browser('https://accounts.google.com/example')
        for key in ('PYTHONHOME', 'LD_LIBRARY_PATH', 'APPDIR'):
            self.assertNotIn(key, run.call_args.kwargs['env'])
        self.assertEqual(run.call_args.kwargs['env']['XAUTHORITY'], '/run/user/1000/test-auth')
        self.assertEqual(run.call_args.kwargs['env']['DESKTOP_SESSION'], 'plasma')
        self.assertEqual(run.call_args.kwargs['env']['XDG_DATA_DIRS'], '/usr/share')

    def test_signin_checks_state_uses_pkce_and_saves_private_tokens(self):
        with tempfile.TemporaryDirectory(dir='/tmp/opencode') as directory:
            root = Path(directory)
            backup.write_private(root / 'google-client.json', {'installed':{'client_id':'test.apps.googleusercontent.com', 'client_secret':'test-secret'}})
            observed = {}
            callbacks = []

            def browser(url):
                parameters = parse_qs(urlparse(url).query)
                observed.update(parameters)
                def callback():
                    redirect = parameters['redirect_uri'][0]
                    bad = requests.get(redirect, params={'state':'wrong', 'code':'bad'}, timeout=10)
                    callbacks.append(bad.status_code)
                    good = requests.get(redirect, params={'state':parameters['state'][0], 'code':'test-code'}, timeout=10)
                    callbacks.append(good.status_code)
                thread = threading.Thread(target=callback)
                thread.start()
                observed['thread'] = thread
                # Also exercise the manual-URL fallback: the browser opener can
                # fail while the user still completes the same callback.
                raise OSError('simulated browser launch failure')

            with patch.object(backup, 'open_browser', side_effect=browser), \
                 patch.object(backup.requests, 'post', return_value=response({'access_token':'test-access', 'refresh_token':'test-refresh', 'expires_in':3600})) as post:
                backup.connect_google(root, root, lambda _: None)
            observed['thread'].join(timeout=10)
            self.assertEqual(callbacks, [400, 200])
            self.assertEqual(observed['scope'], [backup.SCOPE])
            self.assertEqual(observed['code_challenge_method'], ['S256'])
            self.assertIn('code_verifier', post.call_args.kwargs['data'])
            token = root / 'google-token.json'
            self.assertEqual(token.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(token.read_text())['refresh_token'], 'test-refresh')

    def test_uploads_in_chunks_and_requires_google_endpoint(self):
        with tempfile.TemporaryDirectory(dir='/tmp/opencode') as directory:
            archive = Path(directory) / 'backup.zip'
            archive.write_bytes(b'x' * (9 * 1024 * 1024))
            with patch.object(backup, 'access_token', return_value='test-access'), \
                 patch.object(backup.requests, 'get', return_value=response({'files':[{'id':'folder'}]})), \
                 patch.object(backup.requests, 'post', return_value=response(headers={'Location':'https://www.googleapis.com/upload/session'})), \
                 patch.object(backup.requests, 'put', side_effect=[response(code=308, headers={'Range':'bytes=0-8388607'}), response({'id':'uploaded'})]) as put:
                result = backup.upload_archive(Path(directory), archive, lambda _: None)
            self.assertIn('uploaded', result)
            self.assertEqual(put.call_count, 2)
            self.assertEqual(put.call_args.kwargs['headers']['Content-Range'], 'bytes 8388608-9437183/9437184')


@unittest.skipUnless(os.environ.get('ODOO_TRANSFER_INTEGRATION') == '1', 'Requires isolated local PostgreSQL test databases')
class ArchiveIntegrationTests(unittest.TestCase):
    setUp = transfer_tests.TransactionTests.setUp
    tearDown = transfer_tests.TransactionTests.tearDown

    def test_complete_zip_roundtrip_restores_database_and_filestore(self):
        with tempfile.TemporaryDirectory(dir='/tmp/opencode') as directory:
            root = Path(directory)
            source_store = root / 'source-store'
            source_store.mkdir()
            (source_store / 'attachment').write_bytes(b'matching source attachment')
            target_store = root / 'filestore' / self.target['dbname']
            target_store.mkdir(parents=True)
            (target_store / 'old').write_text('old local file')
            source_job = root / 'source-job'
            source_job.mkdir()
            archive, _ = backup.create_archive(self.source, source_store, source_job)
            with zipfile.ZipFile(archive) as stream:
                self.assertEqual(set(stream.namelist()), {'manifest.json', 'dump.sql', 'filestore/attachment'})
                self.assertNotIn('password', stream.read('manifest.json').decode())
            job = root / 'import-job'
            job.mkdir()
            result = backup.import_archive(self.target, target_store, archive, job, lambda _: None)
            self.assertIn('imported', result)
            self.assertEqual((target_store / 'attachment').read_bytes(), b'matching source attachment')
            self.assertFalse((target_store / 'old').exists())
            self.assertTrue((job / 'recovery-before-import.zip').is_file())
            with psycopg2.connect(**self.target) as connection:
                with connection.cursor() as cursor:
                    cursor.execute('SELECT value FROM source_data')
                    self.assertEqual(cursor.fetchone(), ('snapshot',))

    def test_filestore_failure_recovers_original_database_and_files(self):
        with tempfile.TemporaryDirectory(dir='/tmp/opencode') as directory:
            root = Path(directory)
            source_store = root / 'source'
            source_store.mkdir()
            (source_store / 'new').write_text('new')
            target = root / 'stores/local'
            target.mkdir(parents=True)
            (target / 'old').write_text('original')
            archive, _ = backup.create_archive(self.source, source_store, root)
            job = root / 'job'
            job.mkdir()
            original = Path.rename
            def rename(path, destination):
                if path.name.startswith('.odoo-import-'):
                    raise OSError('simulated filestore failure')
                return original(path, destination)
            with patch.object(Path, 'rename', rename), self.assertRaises(OSError):
                backup.import_archive(self.target, target, archive, job, lambda _: None)
            self.assertEqual((target / 'old').read_text(), 'original')
            with psycopg2.connect(**self.target) as connection:
                with connection.cursor() as cursor:
                    cursor.execute('SELECT value FROM stale_data')
                    self.assertEqual(cursor.fetchone(), ('keep on failure',))


if __name__ == '__main__':
    unittest.main()
