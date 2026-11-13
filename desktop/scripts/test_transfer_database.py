import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import uuid
from unittest.mock import patch

import psycopg2
from psycopg2 import sql

spec = importlib.util.spec_from_file_location('transfer', Path(__file__).with_name('transfer_database.py'))
transfer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(transfer)


class TransferUnitTests(unittest.TestCase):
    def test_password_never_appears_in_command_arguments(self):
        settings = dict(host='example.test', port=5432, dbname='sample', user='sample', password='private-secret', sslmode='require')
        with patch.object(transfer.shutil, 'which', return_value='/usr/bin/pg_dump'):
            command, environment = transfer.client_command(settings, 'pg_dump', '--no-owner')
        self.assertNotIn('private-secret', ' '.join(command))
        self.assertNotIn('docker', command)
        self.assertEqual(environment['PGPASSWORD'], 'private-secret')

    def test_restore_uses_one_transaction_and_stops_on_error(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'source.sql'
            source.write_text('CREATE SCHEMA public;\n')
            payload = Path(directory) / 'restore.sql'
            with patch.object(transfer, 'run_client') as client:
                transfer.replace({}, source, payload)
            arguments = client.call_args.args
            self.assertIn('--single-transaction', arguments)
            self.assertIn('--set=ON_ERROR_STOP=1', arguments)
            self.assertTrue(payload.read_text().startswith("SET lock_timeout='15s'; DROP SCHEMA IF EXISTS public CASCADE;"))


@unittest.skipUnless(os.environ.get('ODOO_TRANSFER_INTEGRATION') == '1', 'Set ODOO_TRANSFER_INTEGRATION=1 for isolated PostgreSQL tests')
class TransactionTests(unittest.TestCase):
    def setUp(self):
        inspect = json.loads(subprocess.check_output(['docker', 'inspect', 'odoo-db']))[0]
        environment = dict(value.split('=', 1) for value in inspect['Config']['Env'] if '=' in value)
        self.settings = dict(host='127.0.0.1', port=5432, dbname='postgres', user='odoo', password=environment['POSTGRES_PASSWORD'], sslmode='disable')
        self.container_host = next(iter(inspect['NetworkSettings']['Networks'].values()))['IPAddress']
        self.admin = psycopg2.connect(**self.settings)
        self.admin.autocommit = True
        prefix = 'odoo_desktop_test_' + uuid.uuid4().hex[:12]
        self.names = [prefix + '_source', prefix + '_destination']
        for name in self.names:
            with self.admin.cursor() as cur:
                cur.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
        self.source = dict(self.settings, dbname=self.names[0])
        self.target = dict(self.settings, dbname=self.names[1])
        with psycopg2.connect(**self.source) as connection:
            with connection.cursor() as cur:
                cur.execute("CREATE TABLE source_data(value text); INSERT INTO source_data VALUES ('snapshot')")
                cur.execute('CREATE EXTENSION pg_trgm')
                cur.execute("CREATE TABLE ir_module_module(name text, state text, latest_version text); INSERT INTO ir_module_module VALUES ('base','installed','20.0.1.3')")
        with psycopg2.connect(**self.target) as connection:
            with connection.cursor() as cur:
                cur.execute("CREATE TABLE stale_data(value text); INSERT INTO stale_data VALUES ('keep on failure')")
                cur.execute("CREATE TABLE ir_module_module(name text, state text, latest_version text); INSERT INTO ir_module_module VALUES ('base','installed','20.0.1.3')")

    def tearDown(self):
        for name in self.names:
            with self.admin.cursor() as cur:
                cur.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))
        self.admin.close()

    def test_replacement_removes_old_tables_and_loads_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'source.sql'
            transfer.dump(self.source, source)
            transfer.replace(self.target, source, Path(directory) / 'restore.sql')
        with psycopg2.connect(**self.target) as connection:
            with connection.cursor() as cur:
                cur.execute('SELECT value FROM source_data')
                self.assertEqual(cur.fetchone(), ('snapshot',))
                cur.execute("SELECT to_regclass('stale_data')")
                self.assertIsNone(cur.fetchone()[0])
                cur.execute("SELECT extname FROM pg_extension WHERE extname='pg_trgm'")
                self.assertEqual(cur.fetchone(), ('pg_trgm',))

    def test_failed_restore_rolls_back_schema_drop(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'broken.sql'
            source.write_text('CREATE SCHEMA public; THIS IS INVALID SQL;')
            with self.assertRaises(RuntimeError):
                transfer.replace(self.target, source, Path(directory) / 'restore.sql')
        with psycopg2.connect(**self.target) as connection:
            with connection.cursor() as cur:
                cur.execute('SELECT value FROM stale_data')
                self.assertEqual(cur.fetchone(), ('keep on failure',))

    def test_full_backup_saves_destination_recovery_dump(self):
        self.check_workflow('backup')

    def test_full_import_saves_destination_and_filestore(self):
        self.check_workflow('import')

    def check_workflow(self, action):
        local = self.source if action == 'backup' else self.target
        remote_settings = self.target if action == 'backup' else self.source
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            filestore = root / 'data/filestore' / local['dbname']
            filestore.mkdir(parents=True)
            (filestore / 'test-attachment').write_text('preserve local files')
            config = root / 'odoo.conf'
            config.write_text('[options]\n' + '\n'.join(f'{key} = {value}' for key, value in {
                'db_host':'127.0.0.1', 'db_port':5432, 'db_name':local['dbname'],
                'db_user':local['user'], 'db_password':local['password'],
                'db_sslmode':'disable', 'data_dir':str(root / 'data')
            }.items()))
            remote = root / 'remote.json'
            remote.write_text(json.dumps(dict(remote_settings, host=self.container_host)))
            job = root / 'job'
            transfer.main(action, config, remote, job)
            self.assertTrue(json.loads((job / 'status.json').read_text())['done'])
            self.assertFalse(json.loads((job / 'status.json').read_text())['error'])
            self.assertIn('keep on failure', (job / 'destination-before.sql').read_text())
            self.assertTrue((job / 'local-filestore.tar.gz').is_file())
            self.assertEqual((filestore / 'test-attachment').read_text(), 'preserve local files')
            with psycopg2.connect(**self.target) as connection:
                with connection.cursor() as cur:
                    cur.execute('SELECT value FROM source_data')
                    self.assertEqual(cur.fetchone(), ('snapshot',))


if __name__ == '__main__':
    unittest.main()
