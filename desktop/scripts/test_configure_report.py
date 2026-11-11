from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import configure_report


class ReportUrlTests(unittest.TestCase):
    def test_rejects_external_origins_and_embedded_credentials(self):
        for url in ['https://example.com', 'http://localhost:8000', 'http://user@127.0.0.1:8000', 'http://127.0.0.1:8000/path', 'http://127.0.0.1:8000/?key=value']:
            with self.subTest(url=url), self.assertRaises(ValueError):
                configure_report.configure('/not/read', url)

    def test_only_updates_report_url_using_bound_parameters(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'odoo.conf'
            path.write_text('[options]\ndb_name = example\ndb_user = example\ndb_password = secret\n')
            with patch.object(configure_report.psycopg2, 'connect') as connect:
                cursor = connect.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value
                configure_report.configure(path, 'http://127.0.0.1:8001')
                statement, parameters = cursor.execute.call_args.args
                self.assertIn("'report.url'", statement)
                self.assertNotIn('web.base.url', statement)
                self.assertEqual(parameters, ('http://127.0.0.1:8001',))


if __name__ == '__main__':
    unittest.main()
