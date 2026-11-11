import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location(
    "install_linux", Path(__file__).with_name("install_linux.py")
)
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class InstallTests(unittest.TestCase):
    def test_installs_launcher_and_private_config_without_overwriting(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            image = home / "test.AppImage"
            image.write_bytes(b"test")
            config = home / "source.conf"
            config.write_text("[options]\ndb_password = private\n")
            with patch.object(installer.Path, "home", return_value=home), \
                 patch.dict(installer.os.environ, {"XDG_CONFIG_HOME": str(home / "config"), "XDG_DATA_HOME": str(home / "data")}), \
                 patch.object(installer.subprocess, "run"):
                installer.install(image, config)
                installed = home / "config/local.odoo.desktop/odoo.conf"
                self.assertEqual(installed.stat().st_mode & 0o777, 0o600)
                installed.write_text("custom configuration")
                installer.install(image, config)
                self.assertEqual(installed.read_text(), "custom configuration")
                entry = (home / "data/applications/odoo-desktop.desktop").read_text()
                self.assertIn('Exec="', entry)
                self.assertNotIn("private", entry)
                self.assertTrue((home / "AppImages/Odoo-Desktop.AppImage").stat().st_mode & 0o100)


if __name__ == "__main__":
    unittest.main()
