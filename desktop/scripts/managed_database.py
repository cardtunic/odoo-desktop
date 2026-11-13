"""Private, app-owned PostgreSQL lifecycle. Never adopts or migrates external DBs.

The desktop owns this supervisor, which owns a foreground PostgreSQL process.
Pausing Odoo for backups leaves PostgreSQL running. Closing/crashing the desktop
stops PostgreSQL; only the persistent cluster and private credentials remain.
"""
from __future__ import annotations

import configparser
import ctypes
import fcntl
from functools import partial
import json
import os
from pathlib import Path
import secrets
import select
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time

import psycopg2
from psycopg2 import sql

POSTGRES_MAJOR = '18'


def private_json(path: Path, value: dict) -> None:
    private_text(path, json.dumps(value, indent=2) + '\n')


def private_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile('w', dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
            temporary.chmod(0o600)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def native_environment(runtime: Path) -> dict:
    env = os.environ.copy()
    native = runtime / 'native'
    env['PATH'] = str(native / 'bin') + os.pathsep + env.get('PATH', '')
    env['ODOO_DESKTOP_NATIVE_BIN'] = str(native / 'bin')
    env['LD_LIBRARY_PATH'] = os.pathsep.join(map(str, [native / 'lib', native / 'postgres/lib'])) + (
        os.pathsep + env['LD_LIBRARY_PATH'] if env.get('LD_LIBRARY_PATH') else '')
    env['MAGIC'] = str(native / 'share/misc/magic.mgc')
    env['PYTHONHOME'] = str(runtime / 'python')
    env['PYTHONPATH'] = str(runtime / 'app')
    env['PYTHONNOUSERSITE'] = '1'
    # Do not let host libpq settings override this app's explicit connections.
    for name in tuple(env):
        if name.startswith('PG'):
            del env[name]
    return env


def parent_death_signal(expected_parent: int) -> None:
    # Linux clears this setting on fork, so set it in each direct child before
    # exec. SIGINT requests PostgreSQL fast shutdown, not an unsafe SIGKILL.
    if sys.platform == 'linux':
        # The supervisor handles SIGINT itself. A forked child must not inherit
        # that Python handler during the fork/exec race and swallow its signal.
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        if ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGINT, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), 'Could not set parent-death signal')
        if os.getppid() != expected_parent:
            os.kill(os.getpid(), signal.SIGINT)


class LocalDatabase:
    def __init__(self, runtime: Path, config: Path, data: Path, stop=None, parent=None):
        self.runtime, self.config, self.data = runtime.resolve(), config.absolute(), data.resolve()
        self.marker = self.config.with_name(self.config.name + '.managed.json')
        self.cluster = self.data / 'postgres/cluster'
        self.stop = stop or threading.Event()
        self.parent = parent
        self.parent_fd = None
        if parent is not None and hasattr(os, 'pidfd_open'):
            try:
                self.parent_fd = os.pidfd_open(parent)
            except OSError:
                pass
        self.process = None
        self.lock = None
        self.state = None
        self.port = None
        self.first_login = None

    def check(self):
        parent_exited = False
        if self.parent_fd is not None:
            parent_exited = bool(select.select([self.parent_fd], [], [], 0)[0])
        elif self.parent is not None:
            try:
                os.kill(self.parent, 0)
            except ProcessLookupError:
                parent_exited = True
        if self.stop.is_set() or parent_exited:
            raise InterruptedError('Desktop startup cancelled')

    def save(self):
        private_json(self.marker, self.state)

    def configuration(self) -> bool:
        if self.config.exists() and not self.marker.exists():
            # Existing external configs may legitimately be symlinks. Never
            # rewrite them or create managed metadata for them.
            config = configparser.ConfigParser(interpolation=None)
            with self.config.open() as stream:
                config.read_file(stream)
            if not config.has_section('options'):
                raise ValueError('Odoo configuration requires an options section.')
            return False
        if self.config.is_symlink() or self.marker.is_symlink():
            raise ValueError('Managed configuration files must not be symlinks.')
        if self.marker.exists():
            self.state = json.loads(self.marker.read_text())
            if (self.state.get('format_version') != 1 or self.state.get('config') != str(self.config)
                    or self.state.get('cluster') != str(self.cluster)):
                raise ValueError('Managed database metadata does not match this installation. No data was changed.')
        if not self.config.exists():
            if not self.state:
                if self.cluster.exists():
                    raise ValueError('An existing local PostgreSQL cluster has no ownership metadata. Restore the configuration; it will not be replaced.')
                self.state = dict(format_version=1, config=str(self.config), cluster=str(self.cluster),
                    dbname='odoo', user='odoo_desktop', password=secrets.token_urlsafe(32),
                    postgres_password=secrets.token_urlsafe(32), master_password=secrets.token_urlsafe(32),
                    login_password=secrets.token_urlsafe(20), initialized=False)
                self.save()
            config = configparser.ConfigParser(interpolation=None)
            config['options'] = dict(db_host='127.0.0.1', db_port='5432', db_user=self.state['user'],
                db_password=self.state['password'], db_name=self.state['dbname'], db_sslmode='disable',
                admin_passwd=self.state['master_password'], list_db='False',
                data_dir=str(self.data / 'odoo'), without_demo='True')
            import io
            stream = io.StringIO()
            config.write(stream)
            private_text(self.config, stream.getvalue())
        config = configparser.ConfigParser(interpolation=None)
        with self.config.open() as stream:
            config.read_file(stream)
        options = config['options']
        # An existing config without our marker, or edited to use another DB,
        # is external. Do not start a server, rewrite it, or run Odoo -i/-u.
        return bool(self.state and options.get('db_host') == '127.0.0.1'
                    and options.get('db_user') == self.state['user']
                    and options.get('db_password') == self.state['password']
                    and options.get('db_name') == self.state['dbname'])

    def run(self, arguments, input_text=None, timeout=300):
        self.check()
        process = subprocess.Popen(list(map(str, arguments)), env=native_environment(self.runtime),
            cwd=self.runtime / 'app', stdin=subprocess.PIPE if input_text else subprocess.DEVNULL,
            preexec_fn=partial(parent_death_signal, os.getpid()))
        try:
            if input_text:
                process.stdin.write(input_text.encode())
                process.stdin.close()
            deadline = time.monotonic() + timeout
            while process.poll() is None:
                self.check()
                if time.monotonic() > deadline:
                    raise TimeoutError('Local database setup timed out. See odoo.log; existing data was preserved.')
                time.sleep(0.1)
            if process.returncode:
                raise RuntimeError(f'Local database setup failed ({Path(arguments[0]).name}, exit {process.returncode}). See odoo.log.')
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()

    def admin(self, dbname='postgres'):
        return psycopg2.connect(host='127.0.0.1', port=self.port, dbname=dbname,
            user='desktop_admin', password=self.state['postgres_password'], sslmode='disable', connect_timeout=2)

    def start(self) -> bool:
        try:
            return self._start()
        except BaseException:
            self.close()
            raise

    def _start(self) -> bool:
        if self.config.exists() and not self.marker.exists():
            return self.configuration()
        if self.cluster.parent.is_symlink() or self.cluster.is_symlink():
            raise ValueError('Managed PostgreSQL storage must not be a symlink.')
        self.data.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.data.chmod(0o700)
        self.cluster.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.cluster.parent.chmod(0o700)
        descriptor = os.open(self.cluster.parent / '.desktop.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        self.lock = os.fdopen(descriptor, 'a')
        os.fchmod(self.lock.fileno(), 0o600)
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock.close()
            self.lock = None
            raise RuntimeError('Another desktop process owns the local PostgreSQL cluster.') from None
        if not self.configuration():
            self.lock.close()
            self.lock = None
            return False
        if not (self.runtime / 'native/bin/postgres').is_file():
            raise RuntimeError('Bundled PostgreSQL is missing. Build the native runtime first with pnpm build.')
        if not self.cluster.exists():
            if self.state['initialized']:
                raise RuntimeError('Managed PostgreSQL data is missing. Restore the cluster from backup; a replacement will not be created.')
            # Interrupted initdb never leaves an apparently valid live cluster.
            with tempfile.TemporaryDirectory(prefix='.initializing-', dir=self.cluster.parent) as directory:
                scratch = Path(directory)
                password = scratch / 'password'
                private_text(password, self.state['postgres_password'] + '\n')
                self.run([self.runtime / 'native/bin/initdb', '-D', scratch / 'cluster',
                    '--username=desktop_admin', f'--pwfile={password}', '--auth-host=scram-sha-256',
                    '--auth-local=reject', '--locale=C.UTF-8', '--encoding=UTF8', '--no-instructions'])
                self.check()
                (scratch / 'cluster').rename(self.cluster)
        version = self.cluster / 'PG_VERSION'
        if not version.is_file() or version.read_text().strip() != POSTGRES_MAJOR:
            raise RuntimeError('The local PostgreSQL cluster requires a different major version. No automatic upgrade or replacement is attempted. Back up and migrate explicitly.')
        with socket.socket() as reservation:
            reservation.bind(('127.0.0.1', 0))
            self.port = reservation.getsockname()[1]
        self.check()
        self.process = subprocess.Popen([str(self.runtime / 'native/bin/postgres'), '-D', str(self.cluster),
            '-p', str(self.port), '-c', 'listen_addresses=127.0.0.1', '-c', 'unix_socket_directories=',
            '-c', 'ssl=off', '-c', 'password_encryption=scram-sha-256'],
            env=native_environment(self.runtime), stdin=subprocess.DEVNULL,
            preexec_fn=partial(parent_death_signal, os.getpid()))
        deadline = time.monotonic() + 60
        while True:
            self.check()
            if self.process.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError('Bundled PostgreSQL did not start. Check odoo.log. No existing server is adopted or stopped.')
            try:
                with self.admin():
                    break
            except psycopg2.OperationalError:
                time.sleep(0.2)
        config = configparser.ConfigParser(interpolation=None)
        config.read(self.config)
        config['options']['db_port'] = str(self.port)
        import io
        stream = io.StringIO()
        config.write(stream)
        private_text(self.config, stream.getvalue())
        if not self.state['initialized']:
            self.initialize()
        elif self.config.with_name('first-login.pending').exists():
            credentials = self.config.with_name('first-login.txt')
            if credentials.is_file():
                self.first_login = credentials.read_text() + f'\nSaved privately at: {credentials}'
        return True

    def initialize(self):
        # Only this installation's newly-created, authenticated cluster is
        # eligible. A nonempty/partially initialized database is never reinitialized.
        admin = self.admin()
        try:
            admin.autocommit = True
            with admin.cursor() as cursor:
                cursor.execute('SELECT 1 FROM pg_roles WHERE rolname=%s', (self.state['user'],))
                if not cursor.fetchone():
                    cursor.execute(sql.SQL('CREATE ROLE {} LOGIN CREATEDB NOSUPERUSER NOREPLICATION PASSWORD %s').format(
                        sql.Identifier(self.state['user'])), (self.state['password'],))
                cursor.execute('SELECT 1 FROM pg_database WHERE datname=%s', (self.state['dbname'],))
                if not cursor.fetchone():
                    cursor.execute(sql.SQL("CREATE DATABASE {} OWNER {} TEMPLATE template0 ENCODING 'UTF8'").format(
                        sql.Identifier(self.state['dbname']), sql.Identifier(self.state['user'])))
        finally:
            admin.close()
        with self.admin(self.state['dbname']) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT to_regclass('public.ir_module_module')")
                if cursor.fetchone()[0]:
                    cursor.execute("SELECT state FROM ir_module_module WHERE name='base'")
                    installed = cursor.fetchone() == ('installed',)
                    if not installed:
                        raise RuntimeError('Local Odoo initialization was interrupted. Data is preserved; restore a backup or repair explicitly.')
                else:
                    cursor.execute("SELECT count(*) FROM pg_tables WHERE schemaname='public'")
                    if cursor.fetchone()[0]:
                        raise RuntimeError('Refusing to initialize a nonempty local database.')
                    installed = False
                cursor.execute('CREATE EXTENSION IF NOT EXISTS pg_trgm')
                cursor.execute('CREATE EXTENSION IF NOT EXISTS unaccent')
        python = self.runtime / 'python/bin/python3'
        if not installed:
            self.run([python, '-m', 'odoo', '-c', self.config, '-i', 'base', '--without-demo=True',
                '--stop-after-init', '--no-http', '--workers=0', '--max-cron-threads=0'], timeout=600)
        # The initial web admin password is random, never the upstream admin/admin.
        # Pass it through stdin, not argv or log output.
        self.run([python, '-c', '''import json, sys, odoo
from odoo.modules.registry import Registry
value = json.load(sys.stdin)
odoo.tools.config.parse_config(['-c', value['config']], setup_logging=True)
with Registry(value['database']).cursor() as cr:
    env = odoo.api.Environment(cr, odoo.api.SUPERUSER_ID, {})
    env.ref('base.user_admin').write({'login': 'admin', 'password': value['password']})
    cr.commit()
'''], json.dumps(dict(config=str(self.config), database=self.state['dbname'], password=self.state['login_password'])))
        credentials = self.config.with_name('first-login.txt')
        private_text(credentials, f'Local Odoo login: admin\nInitial password: {self.state["login_password"]}\n\nChange this password in Odoo, then delete this file.\n')
        private_text(self.config.with_name('first-login.pending'), 'pending\n')
        self.first_login = credentials.read_text() + f'\nSaved privately at: {credentials}'
        self.state['initialized'] = True
        self.state.pop('login_password', None)
        self.save()

    def close(self):
        if self.process is not None and self.process.poll() is None:
            self.process.send_signal(signal.SIGINT)
            try:
                self.process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self.process.send_signal(signal.SIGQUIT)
                try:
                    self.process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait()
        if self.lock is not None:
            self.lock.close()
            self.lock = None
        if self.parent_fd is not None:
            os.close(self.parent_fd)
            self.parent_fd = None


def serve(runtime, config, data, ready, parent):
    os.umask(0o077)
    stop = threading.Event()
    for name in (signal.SIGTERM, signal.SIGINT):
        signal.signal(name, lambda *_: stop.set())
    database = LocalDatabase(Path(runtime), Path(config), Path(data), stop, int(parent))
    try:
        database.start()
        database.check()
        private_json(Path(ready), {'ready': True, 'first_login': database.first_login})
        while not stop.wait(0.2):
            database.check()
            if database.process is not None and database.process.poll() is not None:
                raise RuntimeError('Bundled PostgreSQL exited. Close the app and check odoo.log before restarting.')
    finally:
        database.close()


if __name__ == '__main__':
    try:
        serve(*sys.argv[1:])
    except InterruptedError:
        pass
    except Exception as error:
        # Do not log config contents/DSNs (which may contain passwords).
        detail = str(error) if isinstance(error, (RuntimeError, ValueError, TimeoutError)) else type(error).__name__
        print(f'Local database service: {detail}. Check private configuration and PostgreSQL/Odoo messages above.', file=sys.stderr)
        raise SystemExit(1)
