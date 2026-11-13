import hashlib
import io
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import native_dependencies as native


class NativeDependencyTests(unittest.TestCase):
    def test_bad_download_is_never_installed(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(native.urllib.request, 'urlopen', return_value=io.BytesIO(b'corrupt')):
                with self.assertRaisesRegex(ValueError, 'Checksum mismatch'):
                    native.download('https://example.test/binary', '0' * 64, Path(directory))
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_cache_is_verified_before_reuse(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory)
            (cache / 'binary').write_bytes(b'cached')
            digest = hashlib.sha256(b'cached').hexdigest()
            with patch.object(native.urllib.request, 'urlopen') as request:
                self.assertEqual(native.download('https://example.test/binary', digest, cache), cache / 'binary')
            request.assert_not_called()
            (cache / 'binary').write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError, 'Cached download checksum mismatch'):
                native.download('https://example.test/binary', digest, cache)

    def test_wrapper_is_relocatable_and_preserves_argument_boundaries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'runtime with spaces'
            (root / 'bin').mkdir(parents=True)
            (root / 'programs').mkdir()
            program = root / 'programs/example'
            program.write_text('#!/bin/sh\nprintf "%s\\n" "$LD_LIBRARY_PATH" "$@"\n')
            program.chmod(0o755)
            native.wrapper('example', 'programs/example', root)
            result = subprocess.check_output([str(root / 'bin/example'), 'argument with spaces'],
                env=dict(os.environ, LD_LIBRARY_PATH='host'), text=True).splitlines()
            self.assertEqual(result[0], f'{root}/lib:{root}/postgres/lib:host')
            self.assertEqual(result[1], 'argument with spaces')

    def test_glibc_is_not_mixed_with_the_host_loader(self):
        for name in ('libc.so.6', 'libm.so.6', 'libpthread.so.0', 'ld-linux-x86-64.so.2'):
            self.assertIsNotNone(native.SYSTEM_LIBRARIES.fullmatch(name))
        for name in ('libpq.so.5', 'libssl.so.3', 'libfontconfig.so.1'):
            self.assertIsNone(native.SYSTEM_LIBRARIES.fullmatch(name))
