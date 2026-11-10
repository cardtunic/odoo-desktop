"""Complete ZIP backups, transactional database import, and Google Drive OAuth/ uploads."""
import base64
import configparser
from datetime import datetime, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path, PurePosixPath
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from urllib.parse import parse_qs, urlencode, urlparse
import zipfile

import requests

import transfer_database as pg

SCOPE = 'https://www.googleapis.com/auth/drive.file'
TOKEN_URL = 'https://oauth2.googleapis.com/token'
DRIVE = 'https://www.googleapis.com/drive/v3'


class RecoveryRequired(RuntimeError):
    pass


def write_private(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile('w', dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        json.dump(value, stream)
    temporary.chmod(0o600)
    temporary.replace(path)


def local_settings(config_path):
    config = configparser.ConfigParser(interpolation=None)
    config.read(config_path)
    options = config['options']
    settings = dict(host=options.get('db_host', '127.0.0.1'), port=options.getint('db_port', 5432),
                    dbname=options['db_name'], user=options['db_user'], password=options.get('db_password', ''),
                    sslmode=options.get('db_sslmode', 'prefer'))
    if settings['dbname'] in ('', '.', '..') or '/' in settings['dbname'] or '\\' in settings['dbname']:
        raise ValueError('The configured database name cannot be used as a safe filestore directory.')
    if settings['host'] not in ('localhost', '127.0.0.1') or settings['port'] != 5432:
        raise ValueError('Backups currently require the local odoo-db container on port 5432.')
    store = Path(options.get('data_dir', str(Path.home() / '.local/share/Odoo'))) / 'filestore' / settings['dbname']
    return settings, store


def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def modules(settings):
    with pg.connection(settings) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT name FROM ir_module_module WHERE state='installed' ORDER BY name")
            return [name for (name,) in cursor.fetchall()]


def create_archive(settings, store, directory, name='backup.zip'):
    version = pg.validate(settings)
    with pg.connection(settings) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT to_regclass('public.ir_attachment')")
            if cursor.fetchone()[0]:
                cursor.execute('SELECT store_fname FROM ir_attachment WHERE store_fname IS NOT NULL')
                for (filename,) in cursor.fetchall():
                    path = PurePosixPath(filename)
                    if path.is_absolute() or '..' in path.parts or not (store / filename).is_file():
                        raise ValueError('The current filestore is missing an attachment. Repair it before creating a complete backup.')
    database = directory / (name + '.sql')
    pg.dump(settings, database)
    files = {'dump.sql': database}
    if store.exists():
        if store.is_symlink():
            raise ValueError('Symlinked filestores are not supported.')
        for path in sorted(store.rglob('*')):
            if path.is_symlink():
                raise ValueError('Symlinks are not allowed in the filestore.')
            if path.is_file():
                files['filestore/' + path.relative_to(store).as_posix()] = path
    manifest = dict(format='odoo-desktop-backup', format_version=1, odoo_version='20.0',
                    postgres_major=version, database=settings['dbname'],
                    created_at=datetime.now(timezone.utc).isoformat(), modules=modules(settings),
                    filestore_included=True, sha256={member: sha256(path) for member, path in files.items()})
    output = directory / name
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        archive.writestr('manifest.json', json.dumps(manifest))
        for member, path in files.items():
            archive.write(path, member)
    output.chmod(0o600)
    return output, database


def unpack_archive(source, destination):
    """Validate before touching the live database or filestore. No extractall."""
    with zipfile.ZipFile(source) as archive:
        entries = archive.infolist()
        if len(entries) > 200_000:
            raise ValueError('Backup contains too many files.')
        names = set()
        total = 0
        for entry in entries:
            path = PurePosixPath(entry.filename)
            if (entry.filename in names or path.as_posix() != entry.filename or path.is_absolute() or '..' in path.parts or '\\' in entry.filename
                    or ':' in entry.filename or stat.S_ISLNK(entry.external_attr >> 16) or entry.flag_bits & 1):
                raise ValueError('Backup contains an unsafe, duplicate, or encrypted ZIP entry.')
            if entry.filename not in ('manifest.json', 'dump.sql') and not entry.filename.startswith('filestore/'):
                raise ValueError('Unexpected file in backup.')
            if entry.is_dir():
                raise ValueError('Explicit directory entries are not supported by this backup format.')
            names.add(entry.filename)
            total += entry.file_size
        if not {'manifest.json', 'dump.sql'} <= names:
            raise ValueError('Not an Odoo Desktop backup ZIP.')
        if archive.getinfo('manifest.json').file_size > 4 * 1024 * 1024:
            raise ValueError('Backup manifest is too large.')
        manifest = json.loads(archive.read('manifest.json'))
        if (not isinstance(manifest, dict) or manifest.get('format') != 'odoo-desktop-backup' or manifest.get('format_version') != 1
                or manifest.get('odoo_version') != '20.0' or manifest.get('filestore_included') is not True):
            raise ValueError('Unsupported or incomplete backup format.')
        if not isinstance(manifest.get('sha256'), dict) or set(manifest['sha256']) != names - {'manifest.json'}:
            raise ValueError('Backup file inventory does not match the manifest.')
        if total > 100 * 1024**3 or total * 2 + 100 * 1024**2 > shutil.disk_usage(destination.parent).free:
            raise ValueError('Backup is too large or there is not enough free disk space to restore safely.')
        destination.mkdir()
        for entry in entries:
            target = destination / entry.filename
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(entry) as original, target.open('wb') as output:
                shutil.copyfileobj(original, output)
            if entry.filename != 'manifest.json' and sha256(target) != manifest['sha256'][entry.filename]:
                raise ValueError('Backup checksum failed: ' + entry.filename)
        return manifest


def import_archive(settings, store, archive_path, job, report):
    report('Validating the ZIP, checksums, and version compatibility…')
    extracted = job / 'imported'
    manifest = unpack_archive(archive_path, extracted)
    if pg.validate(settings) < int(manifest['postgres_major']):
        raise ValueError('The local PostgreSQL server is older than this backup. Upgrade it before importing.')
    addons = Path(__file__).resolve().parent / 'runtime/app/odoo/addons'
    addon_roots = [addons]
    if not addons.is_dir():
        project = Path(__file__).resolve().parents[2]
        addon_roots = [project / 'odoo/addons', project / 'addons', project / 'design-themes']
    if not isinstance(manifest.get('modules'), list) or any(
        not isinstance(name, str) or '/' in name or '\\' in name or not any(
            (root / name / '__manifest__.py').is_file() for root in addon_roots
        )
        for name in manifest['modules']
    ):
        raise ValueError('The backup requires addons that are not available in this app.')
    report('Saving a complete recovery ZIP of the current database and attachments…')
    recovery, previous_dump = create_archive(settings, store, job, 'recovery-before-import.zip')
    store.parent.mkdir(parents=True, exist_ok=True)
    staged = Path(tempfile.mkdtemp(prefix='.odoo-import-', dir=store.parent))
    previous_store = store.parent / ('.odoo-before-import-' + secrets.token_hex(8))
    database_changed = False
    old_moved = False
    new_moved = False
    try:
        if (extracted / 'filestore').exists():
            shutil.copytree(extracted / 'filestore', staged, dirs_exist_ok=True)
        report('Restoring the database in one transaction…')
        pg.replace(settings, extracted / 'dump.sql', job / 'restore.sql')
        database_changed = True
        report('Installing the matching filestore…')
        if store.exists():
            store.rename(previous_store)
            old_moved = True
        staged.rename(store)
        new_moved = True
    except Exception:
        if database_changed:
            report('Import failed; restoring the previous database and filestore…')
            try:
                pg.replace(settings, previous_dump, job / 'rollback.sql')
                if new_moved:
                    shutil.rmtree(store)
                if old_moved:
                    previous_store.rename(store)
            except Exception as error:
                raise RecoveryRequired(f'Automatic recovery failed. Keep Odoo closed and restore {recovery}. Original filestore: {previous_store}') from error
        raise
    finally:
        if staged.exists():
            shutil.rmtree(staged)
    # Keep the original store alongside the recovery ZIP rather than deleting
    # the user's old files. It is outside the active database directory.
    return f'Database and attachments imported. Recovery ZIP: {recovery}. Previous filestore: {previous_store if old_moved else "none"}.'


def google_client(directory):
    value = json.loads((directory / 'google-client.json').read_text()).get('installed', {})
    if not value.get('client_id') or not value.get('client_secret'):
        raise ValueError('Choose a Google OAuth Desktop app client JSON file first.')
    return value


def safe_google_response(response, context):
    if not response.ok:
        raise RuntimeError(f'{context} failed (HTTP {response.status_code}). Check Google Drive API access and reconnect if necessary.')
    return response.json() if response.content else {}


def open_browser(url):
    # Do not leak AppImage's Python/GTK/library environment into the browser.
    # Preserve XAUTHORITY and the rest of the desktop session: a small allowlist
    # can make KDE report success while the actual browser cannot open a window.
    environment = os.environ.copy()
    appdir = environment.get('APPDIR', '')
    for key in ('PYTHONHOME', 'PYTHONPATH', 'LD_LIBRARY_PATH', 'LD_PRELOAD', 'APPDIR', 'APPIMAGE', 'ARGV0',
                'GDK_PIXBUF_MODULE_FILE', 'GDK_PIXBUF_MODULEDIR', 'GIO_MODULE_DIR', 'GI_TYPELIB_PATH',
                'GTK_PATH', 'GTK_EXE_PREFIX', 'GTK_DATA_PREFIX', 'GST_PLUGIN_PATH', 'GST_PLUGIN_SYSTEM_PATH',
                'GST_PLUGIN_SCANNER', 'WEBKIT_EXEC_PATH', 'WEBKIT_INJECTED_BUNDLE_PATH'):
        environment.pop(key, None)
    if appdir:
        for key in ('PATH', 'XDG_DATA_DIRS', 'XDG_CONFIG_DIRS'):
            if key in environment:
                environment[key] = os.pathsep.join(value for value in environment[key].split(os.pathsep)
                    if value and value != appdir and not value.startswith(appdir + '/'))
    subprocess.run(['xdg-open', url], env=environment, check=True, timeout=30,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def connect_google(directory, job, report):
    client = google_client(directory)
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
    result = {}

    class Callback(BaseHTTPRequestHandler):
        def setup(self):
            self.request.settimeout(5)
            super().setup()

        def log_message(self, *_):
            pass

        def do_GET(self):
            parsed = urlparse(self.path)
            parameters = parse_qs(parsed.query)
            valid = parsed.path == '/oauth/callback' and secrets.compare_digest(parameters.get('state', [''])[0], state)
            self.send_response(200 if valid else 400)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(b'Return to Odoo Desktop. You can close this tab.' if valid else b'Invalid callback.')
            if valid:
                result.update(code=parameters.get('code', [''])[0], error=parameters.get('error', [''])[0])

    with HTTPServer(('127.0.0.1', 0), Callback) as server:
        server.timeout = .5
        redirect = f'http://127.0.0.1:{server.server_port}/oauth/callback'
        query = urlencode(dict(client_id=client['client_id'], redirect_uri=redirect, response_type='code', scope=SCOPE,
                               state=state, code_challenge=challenge, code_challenge_method='S256',
                               access_type='offline', prompt='consent'))
        auth_url = 'https://accounts.google.com/o/oauth2/v2/auth?' + query
        def browser_status(message):
            write_private(job / 'status.json', dict(message=message, done=False, error=False, auth_url=auth_url))
        browser_status('Complete Google sign-in in your browser. If no tab opens, copy the sign-in URL below into your browser. Keep this window open.')
        try:
            open_browser(auth_url)
        except (OSError, subprocess.SubprocessError):
            browser_status('The browser could not be opened automatically. Copy the sign-in URL below into your browser to continue.')
        deadline = time.monotonic() + 180
        while not result and time.monotonic() < deadline:
            if (job / 'cancel').exists():
                raise ValueError('Google sign-in cancelled.')
            server.handle_request()
        if not result.get('code') or result.get('error'):
            raise ValueError('Google sign-in was denied or timed out. Try connecting again.')
    response = requests.post(TOKEN_URL, data=dict(code=result['code'], client_id=client['client_id'],
        client_secret=client['client_secret'], redirect_uri=redirect, grant_type='authorization_code', code_verifier=verifier), timeout=30)
    token = safe_google_response(response, 'Google sign-in')
    if not token.get('access_token') or not token.get('refresh_token'):
        raise ValueError('Google did not issue persistent access. Reconnect and grant access again.')
    token['expires_at'] = time.time() + token.get('expires_in', 3600)
    write_private(directory / 'google-token.json', token)
    return 'Connected to Google Drive. Access is limited to files created by this app.'


def access_token(directory):
    path = directory / 'google-token.json'
    if not path.exists():
        raise ValueError('Connect Google Drive first.')
    token = json.loads(path.read_text())
    if token.get('expires_at', 0) > time.time() + 120:
        return token['access_token']
    client = google_client(directory)
    response = requests.post(TOKEN_URL, data=dict(client_id=client['client_id'], client_secret=client['client_secret'],
        refresh_token=token['refresh_token'], grant_type='refresh_token'), timeout=30)
    token.update(safe_google_response(response, 'Refreshing Google access'))
    token['expires_at'] = time.time() + token.get('expires_in', 3600)
    write_private(path, token)
    return token['access_token']


def disconnect_google(directory):
    path = directory / 'google-token.json'
    if path.exists():
        token = json.loads(path.read_text())
        response = requests.post('https://oauth2.googleapis.com/revoke', data={'token':token.get('refresh_token', token.get('access_token', ''))}, timeout=30)
        if response.status_code not in (200, 400):
            raise RuntimeError('Could not revoke Google access. Try again when online.')
        path.unlink()
    return 'Google Drive disconnected. Existing Drive backups were not deleted.'


def upload_archive(directory, archive, report):
    headers = {'Authorization':'Bearer ' + access_token(directory)}
    query = "trashed=false and mimeType='application/vnd.google-apps.folder' and appProperties has { key='odooDesktopBackups' and value='1' }"
    folders = safe_google_response(requests.get(DRIVE + '/files', headers=headers,
        params={'q':query, 'fields':'files(id)', 'pageSize':1}, timeout=30), 'Finding backup folder').get('files', [])
    folder = folders[0]['id'] if folders else safe_google_response(requests.post(DRIVE + '/files', headers=headers,
        json={'name':'Odoo Desktop backups', 'mimeType':'application/vnd.google-apps.folder', 'appProperties':{'odooDesktopBackups':'1'}}, timeout=30), 'Creating backup folder')['id']
    response = requests.post('https://www.googleapis.com/upload/drive/v3/files', params={'uploadType':'resumable', 'fields':'id'},
        headers={**headers, 'X-Upload-Content-Type':'application/zip', 'X-Upload-Content-Length':str(archive.stat().st_size)},
        json={'name':archive.name, 'parents':[folder]}, timeout=30)
    safe_google_response(response, 'Starting upload')
    location = response.headers.get('Location', '')
    parsed = urlparse(location)
    if parsed.scheme != 'https' or parsed.hostname not in ('www.googleapis.com', 'content.googleapis.com'):
        raise ValueError('Google returned an unexpected upload endpoint.')
    total = archive.stat().st_size
    offset = 0
    with archive.open('rb') as stream:
        while offset < total:
            stream.seek(offset)
            chunk = stream.read(8 * 1024 * 1024)
            report(f'Uploading ZIP to Google Drive… {offset * 100 // total}%')
            response = requests.put(location, headers={**headers, 'Content-Type':'application/zip',
                'Content-Range':f'bytes {offset}-{offset + len(chunk) - 1}/{total}'}, data=chunk, timeout=120, allow_redirects=False)
            if response.status_code == 308:
                acknowledged = response.headers.get('Range', '')
                if not acknowledged.startswith('bytes=0-'):
                    raise ValueError('Google did not acknowledge the upload chunk. The local ZIP is retained.')
                next_offset = int(acknowledged.rsplit('-', 1)[1]) + 1
                if next_offset <= offset or next_offset > offset + len(chunk):
                    raise ValueError('Google returned an unexpected upload offset. The local ZIP is retained.')
                offset = next_offset
            else:
                result = safe_google_response(response, 'Uploading backup')
                if not result.get('id'):
                    raise ValueError('Google did not confirm the completed upload.')
                return f'Backup uploaded to Google Drive / Odoo Desktop backups / {archive.name}. Local copy: {archive}'
    raise RuntimeError('Google did not confirm the completed upload. The local ZIP is retained.')


def main(action, config_path, directory, job_path, archive_path=''):
    os.umask(0o077)
    directory, job = Path(directory), Path(job_path)
    job.mkdir(parents=True, exist_ok=True)
    report = lambda message: pg.status(job / 'status.json', message)
    try:
        if action == 'connect-drive':
            message = connect_google(directory, job, report)
        elif action == 'disconnect-drive':
            message = disconnect_google(directory)
        elif action == 'drive-backup':
            access_token(directory)  # Fail before doing an expensive dump if disconnected.
            settings, store = local_settings(config_path)
            report('Creating a consistent database and attachments ZIP…')
            name = 'odoo-backup-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '.zip'
            archive, _ = create_archive(settings, store, job, name)
            message = upload_archive(directory, archive, report)
        elif action == 'zip-import':
            settings, store = local_settings(config_path)
            message = import_archive(settings, store, Path(archive_path), job, report)
        else:
            raise ValueError('Unknown backup action.')
        pg.status(job / 'status.json', message, done=True)
    except Exception as error:
        # Never include HTTP payloads, OAuth URLs, or token contents in errors.
        message = str(error) if isinstance(error, (ValueError, RuntimeError)) else type(error).__name__ + ': operation failed. Check connectivity and configuration.'
        write_private(job / 'status.json', dict(message=message + f'\nRecovery/local backup files: {job}',
            done=True, error=True, recovery_required=isinstance(error, RecoveryRequired)))
        raise SystemExit(1)


if __name__ == '__main__':
    main(*sys.argv[1:])
