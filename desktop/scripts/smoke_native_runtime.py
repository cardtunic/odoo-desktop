"""Exercise the extracted native bundle using disposable, isolated user data.

No Docker socket or existing database/config is used. This deliberately runs
real initdb, Odoo initialization, SQL restore/rollback, and PDF/image rendering.
"""
import configparser
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

import psycopg2

def check(runtime):
    runtime = Path(runtime).resolve()
    # Prefer the helpers actually shipped next to runtime/, not the source
    # checkout's copies. This also tests packaged addon discovery and PG tools.
    if (runtime.parent / 'backup_drive.py').is_file():
        sys.path.insert(0, str(runtime.parent))
    import backup_drive as backup
    from managed_database import LocalDatabase, native_environment
    import transfer_database as pg
    # Do not accidentally rely on the build runner's system timezone database.
    os.environ['PYTHONTZPATH'] = ''
    os.environ['ODOO_DESKTOP_NATIVE_BIN'] = str(runtime / 'native/bin')
    with tempfile.TemporaryDirectory(prefix='odoo-native-smoke-') as directory:
        root = Path(directory)
        # Keep fonts/browser caches and Odoo data out of the real user's home.
        os.environ['HOME'] = str(root / 'home')
        os.environ['XDG_CACHE_HOME'] = str(root / 'cache')
        config = root / 'config/odoo.conf'
        data = root / 'data'
        database = LocalDatabase(runtime, config, data)
        try:
            assert database.start()
            assert database.first_login and 'Initial password:' in database.first_login
            # Simulate acknowledging the desktop's first-login dialog.
            config.with_name('first-login.pending').unlink()
            assert database.cluster.joinpath('PG_VERSION').read_text().strip() == '18'
            for path in (config, database.marker, config.with_name('first-login.txt')):
                assert path.stat().st_mode & 0o777 == 0o600
            assert database.cluster.stat().st_mode & 0o777 == 0o700
            options = configparser.ConfigParser(interpolation=None)
            options.read(config)
            settings = dict(host='127.0.0.1', port=options.getint('options', 'db_port'),
                dbname=options['options']['db_name'], user=options['options']['db_user'],
                password=options['options']['db_password'], sslmode='disable')
            with pg.connection(settings) as connection:
                with connection.cursor() as cursor:
                    cursor.execute('SHOW listen_addresses')
                    assert cursor.fetchone() == ('127.0.0.1',)
                    cursor.execute('SELECT rolsuper FROM pg_roles WHERE rolname=current_user')
                    assert cursor.fetchone() == (False,)
                    cursor.execute("SELECT state FROM ir_module_module WHERE name='base'")
                    assert cursor.fetchone() == ('installed',)
                    cursor.execute("CREATE TABLE desktop_smoke(value text); INSERT INTO desktop_smoke VALUES ('persistent')")
            command, environment = pg.client_command(settings, 'psql', '-X', '-At', '-c', 'SELECT 42')
            assert 'docker' not in command
            assert subprocess.check_output(command, env=environment, text=True).strip() == '42'
            bad = dict(settings, password='not-the-password')
            try:
                pg.connection(bad)
            except psycopg2.OperationalError:
                pass
            else:
                raise AssertionError('PostgreSQL accepted an incorrect password')
            store = data / 'odoo/filestore/odoo'
            store.mkdir(parents=True, exist_ok=True)
            (store / 'smoke-attachment').write_bytes(b'attachment contents')
            jobs = root / 'jobs'
            jobs.mkdir()
            archive, _ = backup.create_archive(settings, store, jobs)
            restore = root / 'restore'
            restore.mkdir()
            backup.import_archive(settings, store, archive, restore, print)
            assert (store / 'smoke-attachment').read_bytes() == b'attachment contents'
            # A broken restore must roll back the schema drop completely.
            invalid = root / 'broken.sql'
            invalid.write_text('CREATE SCHEMA public; THIS IS NOT SQL;')
            try:
                pg.replace(settings, invalid, root / 'restore.sql')
            except RuntimeError:
                pass
            else:
                raise AssertionError('Invalid restore unexpectedly succeeded')
        finally:
            database.close()
        assert database.process.poll() is not None
        assert not (database.cluster / 'postmaster.pid').exists()
        again = LocalDatabase(runtime, config, data)
        try:
            assert again.start()
            assert again.first_login is None, 'Restart must not reset web admin credentials'
            settings['port'] = again.port
            with pg.connection(settings) as connection:
                with connection.cursor() as cursor:
                    cursor.execute('SELECT value FROM desktop_smoke')
                    assert cursor.fetchone() == ('persistent',)
        finally:
            again.close()
        # Test the actual service protocol and termination used by the Rust app.
        ready = root / 'ready.json'
        env = native_environment(runtime)
        helper = runtime.parent / 'managed_database.py'
        if not helper.is_file():
            helper = Path(__file__).with_name('managed_database.py')
        service = subprocess.Popen([str(runtime / 'python/bin/python3'),
            str(helper), str(runtime), str(config), str(data),
            str(ready), str(os.getpid())], env=env)
        try:
            deadline = time.monotonic() + 60
            while not ready.exists():
                assert service.poll() is None, 'Database supervisor exited before readiness'
                assert time.monotonic() < deadline, 'Database readiness timed out'
                time.sleep(0.1)
            service.send_signal(signal.SIGTERM)
            assert service.wait(timeout=45) == 0
            assert not (database.cluster / 'postmaster.pid').exists()
        finally:
            if service.poll() is None:
                service.terminate()
                service.wait(timeout=45)
        # A killed desktop must not leave a hidden database service behind.
        crash_ready = root / 'crash-ready.json'
        launcher = subprocess.Popen([str(runtime / 'python/bin/python3'), '-c', '''import os, subprocess, sys, time
child = subprocess.Popen([sys.executable, *sys.argv[1:], str(os.getpid())])
print(child.pid, flush=True)
time.sleep(120)
''', str(helper), str(runtime), str(config), str(data), str(crash_ready)], env=env, stdout=subprocess.PIPE, text=True)
        orphan = int(launcher.stdout.readline().strip())
        try:
            deadline = time.monotonic() + 60
            while not crash_ready.exists():
                assert launcher.poll() is None
                assert time.monotonic() < deadline, 'Crash-test database readiness timed out'
                time.sleep(0.1)
            launcher.kill()
            launcher.wait(timeout=10)
            deadline = time.monotonic() + 45
            while (database.cluster / 'postmaster.pid').exists():
                assert time.monotonic() < deadline, 'Database was orphaned after desktop termination'
                time.sleep(0.1)
        finally:
            if launcher.poll() is None:
                launcher.kill()
                launcher.wait(timeout=10)
            try:
                os.kill(orphan, signal.SIGTERM)
            except ProcessLookupError:
                pass
            launcher.stdout.close()
        html = root / 'report.html'
        html.write_text('<html><body><h1>Odoo PDF smoke test — Olá</h1><p>Bundled fonts and patched Qt.</p></body></html>')
        pdf, image = root / 'report.pdf', root / 'report.png'
        subprocess.run([str(runtime / 'native/bin/wkhtmltopdf'), '--quiet', '--header-center', 'Odoo Desktop',
            str(html), str(pdf)], env=env, check=True, timeout=60)
        assert pdf.read_bytes().startswith(b'%PDF-') and pdf.stat().st_size > 1000
        subprocess.run([str(runtime / 'native/bin/wkhtmltoimage'), '--quiet', '--format', 'png',
            str(html), str(image)], env=env, check=True, timeout=60)
        assert image.read_bytes().startswith(b'\x89PNG')
        print('Native bundle smoke test passed: first run, authentication, persistence, ZIP backup/restore, rollback, shutdown, parent termination, PDF and PNG.')


if __name__ == '__main__':
    check(sys.argv[1])
