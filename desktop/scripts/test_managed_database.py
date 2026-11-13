import configparser
import fcntl
import json
import signal
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from managed_database import LocalDatabase, parent_death_signal


class ConfigurationTests(unittest.TestCase):
    def test_pid_one_is_a_valid_parent_in_a_container(self):
        with patch('managed_database.os.getppid', return_value=1), \
             patch('managed_database.ctypes.CDLL') as library, patch('managed_database.os.kill') as kill, \
             patch('managed_database.signal.signal') as handler:
            library.return_value.prctl.return_value = 0
            parent_death_signal(1)
        kill.assert_not_called()
        handler.assert_any_call(signal.SIGINT, signal.SIG_DFL)

    def test_child_does_not_outlive_parent_that_died_before_preexec(self):
        with patch('managed_database.os.getppid', return_value=1), \
             patch('managed_database.os.getpid', return_value=123), \
             patch('managed_database.ctypes.CDLL') as library, patch('managed_database.os.kill') as kill, \
             patch('managed_database.signal.signal'):
            library.return_value.prctl.return_value = 0
            parent_death_signal(456)
        kill.assert_called_once_with(123, signal.SIGINT)

    def test_existing_external_configuration_is_never_changed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / 'odoo.conf'
            original = '[options]\ndb_host = example.test\ndb_name = production\ndb_user = existing\ndb_password = private\n'
            config.write_text(original)
            database = LocalDatabase(root / 'runtime', config, root / 'data')
            with patch('managed_database.subprocess.Popen') as process:
                self.assertFalse(database.start())
            process.assert_not_called()
            self.assertEqual(config.read_text(), original)
            self.assertFalse(database.marker.exists())
            self.assertFalse(database.cluster.exists())

    def test_new_configuration_and_credentials_are_private_and_reusable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = LocalDatabase(root / 'runtime', root / 'config/odoo.conf', root / 'data')
            self.assertTrue(database.configuration())
            options = configparser.ConfigParser(interpolation=None)
            options.read(database.config)
            self.assertEqual(options['options']['db_host'], '127.0.0.1')
            self.assertNotEqual(database.state['password'], database.state['postgres_password'])
            self.assertNotEqual(database.state['login_password'], 'admin')
            for path in (database.config, database.marker):
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            original = database.config.read_bytes()
            self.assertTrue(database.configuration())
            self.assertEqual(database.config.read_bytes(), original)

    def test_editing_connection_opts_out_of_managed_database(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = LocalDatabase(root / 'runtime', root / 'odoo.conf', root / 'data')
            database.configuration()
            database.config.write_text(database.config.read_text().replace('127.0.0.1', 'remote.test'))
            original = database.config.read_bytes()
            self.assertFalse(database.start())
            self.assertEqual(database.config.read_bytes(), original)
            self.assertFalse(database.cluster.exists())

    def test_no_replacement_of_cluster_without_ownership_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = LocalDatabase(root / 'runtime', root / 'odoo.conf', root / 'data')
            database.cluster.mkdir(parents=True)
            original = database.cluster / 'keep'
            original.write_text('existing database')
            with self.assertRaisesRegex(ValueError, 'ownership metadata'):
                database.configuration()
            self.assertEqual(original.read_text(), 'existing database')
            self.assertFalse(database.config.exists())

    def test_relocated_metadata_is_not_silently_used_for_another_cluster(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = LocalDatabase(root / 'runtime', root / 'odoo.conf', root / 'data')
            database.configuration()
            value = json.loads(database.marker.read_text())
            value['cluster'] = '/some/other/cluster'
            database.marker.write_text(json.dumps(value))
            with self.assertRaisesRegex(ValueError, 'metadata does not match'):
                database.start()

    def test_second_owner_cannot_change_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = LocalDatabase(root / 'runtime', root / 'odoo.conf', root / 'data')
            database.configuration()
            database.cluster.parent.mkdir(parents=True)
            original = database.config.read_bytes()
            with (database.cluster.parent / '.desktop.lock').open('a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaisesRegex(RuntimeError, 'Another desktop process'):
                    database.start()
            self.assertEqual(database.config.read_bytes(), original)

    def test_major_version_mismatch_preserves_cluster(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = LocalDatabase(root / 'runtime', root / 'odoo.conf', root / 'data')
            database.configuration()
            database.cluster.mkdir(parents=True)
            (database.cluster / 'PG_VERSION').write_text('17\n')
            (database.cluster / 'sentinel').write_text('keep database')
            (root / 'runtime/native/bin').mkdir(parents=True)
            (root / 'runtime/native/bin/postgres').touch()
            with patch('managed_database.subprocess.Popen') as process:
                with self.assertRaisesRegex(RuntimeError, 'different major version'):
                    database.start()
            process.assert_not_called()
            self.assertEqual((database.cluster / 'sentinel').read_text(), 'keep database')

    def test_missing_initialized_cluster_is_not_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = LocalDatabase(root / 'runtime', root / 'odoo.conf', root / 'data')
            database.configuration()
            database.state['initialized'] = True
            database.save()
            (root / 'runtime/native/bin').mkdir(parents=True)
            (root / 'runtime/native/bin/postgres').touch()
            with patch('managed_database.subprocess.Popen') as process:
                with self.assertRaisesRegex(RuntimeError, 'data is missing'):
                    database.start()
            process.assert_not_called()
            self.assertFalse(database.cluster.exists())
