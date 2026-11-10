import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch
import tempfile


spec = importlib.util.spec_from_file_location(
    "prepare_runtime", Path(__file__).with_name("prepare_runtime.py")
)
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class RuntimeTests(unittest.TestCase):
    def test_ignores_build_cache_but_preserves_odoo_assets(self):
        names = ["__pycache__", ".git", ".venv", "thing.pyc", "static", "views", "__init__.py"]
        self.assertEqual(builder.copy_ignore("", names), {"__pycache__", ".git", ".venv", "thing.pyc"})

    def test_only_app_source_is_staged(self):
        with tempfile.TemporaryDirectory(dir="/tmp/opencode") as temporary:
            root = Path(temporary)
            source = root / "project"
            (source / "odoo").mkdir(parents=True)
            (source / "odoo" / "__init__.py").write_text("")
            (source / "addons" / "example").mkdir(parents=True)
            (source / "addons" / "example" / "__init__.py").write_text("")
            (source / "odoo.conf").write_text("db_password = secret")
            python = root / "managed" / "bin" / "python3"
            python.parent.mkdir(parents=True)
            python.write_text("")
            destination = root / "bundle"
            with patch.object(builder, "PROJECT", source), \
                 patch.object(builder.subprocess, "run") as run, \
                 patch.object(builder.subprocess, "check_output", side_effect=[str(python), "babel==2.17.0\n"] * 2):
                builder.stage(destination)
                builder.stage(destination)
            self.assertTrue((destination / "app" / "odoo" / "__init__.py").is_file())
            self.assertTrue((destination / "app" / "odoo" / "addons" / "example" / "__init__.py").is_file())
            self.assertFalse((destination / "app" / "odoo.conf").exists())
            self.assertEqual(run.call_args.kwargs["env"]["PYTHONNOUSERSITE"], "1")


if __name__ == "__main__":
    unittest.main()
