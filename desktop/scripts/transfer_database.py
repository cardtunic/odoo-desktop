"""One-way, transactional Odoo public-schema replacement using PostgreSQL clients.

Invoked only by the desktop's trusted settings window. Never logs passwords.
"""
import configparser
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile

import psycopg2


def status(path, message, done=False, error=False):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(dict(message=message, done=done, error=error)))
    temporary.replace(path)


def connection(settings):
    return psycopg2.connect(**settings, connect_timeout=10, application_name='odoo-desktop-transfer')


def validate(settings, require_odoo=True):
    with connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.ir_module_module')")
            if cur.fetchone()[0]:
                cur.execute("SELECT latest_version FROM public.ir_module_module WHERE name='base' AND state='installed'")
                row = cur.fetchone()
                if not row or not row[0].startswith('20.0.'):
                    raise ValueError('The database must contain an installed Odoo 20 database.')
            elif require_odoo:
                raise ValueError('The source database must contain an installed Odoo 20 database.')
            cur.execute("SELECT nspname FROM pg_namespace WHERE nspname NOT LIKE 'pg_%' AND nspname NOT IN ('public', 'information_schema')")
            if cur.fetchall():
                raise ValueError('Only databases with a single user schema (public) are supported.')
            return conn.server_version // 10000


def client_command(settings, program, *arguments):
    if program not in ('psql', 'pg_dump', 'pg_restore'):
        raise ValueError('Unsupported PostgreSQL client.')
    # Packaged helpers live beside runtime/. Development uses the staged runtime.
    configured = os.environ.get('ODOO_DESKTOP_NATIVE_BIN')
    if configured and not (Path(configured) / program).is_file():
        raise RuntimeError('The bundled PostgreSQL client is missing. Reinstall the AppImage.')
    candidates = ([Path(configured) / program] if configured else []) + [Path(__file__).resolve().parent / 'runtime/native/bin' / program,
                  Path(__file__).resolve().parent.parent / 'src-tauri/runtime/native/bin' / program]
    binary = next((str(path) for path in candidates if path.is_file()), None) or shutil.which(program)
    if not binary:
        raise RuntimeError('PostgreSQL client tools are missing. Build the native runtime first.')
    env = {key: value for key, value in os.environ.items() if not key.startswith('PG')}
    values = dict(PGHOST=settings['host'], PGPORT=str(settings['port']),
                  PGDATABASE=settings['dbname'], PGUSER=settings['user'],
                  PGPASSWORD=settings['password'], PGSSLMODE=settings['sslmode'],
                  PGCONNECT_TIMEOUT='10', PGCHANNELBINDING='prefer')
    env.update(values)
    return [binary, *arguments], env


def run_client(settings, program, *args, stdin=None, stdout=None):
    command, env = client_command(settings, program, *args)
    result = subprocess.run(command, env=env, stdin=stdin, stdout=stdout or subprocess.DEVNULL,
                            stderr=subprocess.PIPE)
    if result.returncode:
        # PostgreSQL errors may contain data: kept in the private job folder.
        raise RuntimeError(result.stderr.decode(errors='replace')[-6000:])


def dump(settings, destination):
    with destination.open('wb') as output:
        # Explicitly selecting public includes CREATE SCHEMA even when it is
        # the unchanged bootstrap schema. Include extension definitions too.
        run_client(settings, 'pg_dump', '--no-owner', '--no-acl', '--schema=public', '--extension=*', stdout=output)


def replace(settings, source, payload):
    with payload.open('wb') as output, source.open('rb') as original:
        output.write(b"SET lock_timeout='15s'; DROP SCHEMA IF EXISTS public CASCADE;\n")
        shutil.copyfileobj(original, output)
    with payload.open('rb') as input_file:
        # The DROP and entire restore commit together, or ALL roll back.
        run_client(settings, 'psql', '-X', '--set=ON_ERROR_STOP=1', '--single-transaction',
                   '--file=-', stdin=input_file)


def main(action, config_path, remote_path, job_path):
    os.umask(0o077)
    job = Path(job_path)
    job.mkdir(parents=True, exist_ok=True)
    progress = job / 'status.json'
    try:
        config = configparser.ConfigParser(interpolation=None)
        config.read(config_path)
        options = config['options']
        local = dict(host=options.get('db_host', '127.0.0.1'), port=options.getint('db_port', 5432),
                     dbname=options['db_name'], user=options['db_user'],
                     password=options.get('db_password', ''), sslmode=options.get('db_sslmode', 'prefer'))
        remote = json.loads(Path(remote_path).read_text())
        if action not in ('test', 'backup', 'import'):
            raise ValueError('Unknown transfer operation.')
        status(progress, 'Checking database connections…')
        remote_version = validate(remote, require_odoo=action == 'import')
        if action == 'test':
            status(progress, f'Connected to Odoo 20 on PostgreSQL {remote_version}.', done=True)
            return
        if local['host'] not in ('127.0.0.1', 'localhost'):
            raise ValueError('Transfers require a local PostgreSQL database.')
        if remote['host'].lower() in ('localhost', '127.0.0.1', '::1'):
            raise ValueError('The remote destination must not point at the local database.')
        local_version = validate(local)
        if action == 'import':
            bundled = Path(__file__).resolve().parent / 'runtime/app/odoo/addons'
            addon_roots = [bundled]
            if not bundled.is_dir():
                project = Path(__file__).resolve().parents[2]
                addon_roots = [project / 'odoo/addons', project / 'addons', project / 'design-themes']
            with connection(remote) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT name FROM ir_module_module WHERE state='installed'")
                    missing = [name for (name,) in cur.fetchall() if not any(
                        (root / name / '__manifest__.py').is_file() for root in addon_roots
                    )]
            if missing:
                raise ValueError('The remote database uses addons not bundled with this app: ' + ', '.join(missing))
        identities = []
        for settings in (local, remote):
            with connection(settings) as conn:
                with conn.cursor() as cur:
                    cur.execute('SELECT current_database(), inet_server_addr()::text, pg_postmaster_start_time()')
                    identities.append(cur.fetchone())
        if identities[0] == identities[1]:
            raise ValueError('Source and destination are the same database. Refusing replacement.')
        source, target = (local, remote) if action == 'backup' else (remote, local)
        source_version, target_version = (local_version, remote_version) if action == 'backup' else (remote_version, local_version)
        if target_version < source_version:
            raise ValueError(f'Cannot safely restore PostgreSQL {source_version} into older PostgreSQL {target_version}. Use a remote server at least as new as the source.')
        command, env = client_command(local, 'pg_dump', '--version')
        version = subprocess.check_output(command, env=env, text=True)
        client_major = int(version.split()[2].split('.')[0])
        if client_major < max(local_version, remote_version):
            raise ValueError('The PostgreSQL client is older than one of the servers.')
        status(progress, 'Saving a recovery dump of the destination before replacement…')
        dump(target, job / 'destination-before.sql')
        filestore = Path(options.get('data_dir', str(Path.home() / '.local/share/Odoo'))) / 'filestore' / local['dbname']
        if filestore.is_dir():
            status(progress, 'Saving the local filestore archive…')
            with tarfile.open(job / 'local-filestore.tar.gz', 'w:gz') as archive:
                archive.add(filestore, arcname=local['dbname'])
        status(progress, 'Dumping the source database…')
        dump(source, job / 'source.sql')
        status(progress, 'Replacing the destination in a single transaction. Keep this window open…')
        replace(target, job / 'source.sql', job / 'restore.sql')
        (job / 'restore.sql').unlink()
        status(progress, f'Database replacement completed. Recovery dump and local filestore archive: {job}. Remote attachments were NOT transferred.', done=True)
    except Exception as error:
        message = str(error)
        try:
            secret = json.loads(Path(remote_path).read_text()).get('password')
            if secret:
                message = message.replace(secret, '[redacted]')
        except Exception:
            pass
        status(progress, f'Transfer failed: {message}\nRecovery files: {job}', done=True, error=True)
        raise SystemExit(1)


if __name__ == '__main__':
    main(*sys.argv[1:])
