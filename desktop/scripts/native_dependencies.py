"""Build/stage pinned, relocatable PostgreSQL and patched-Qt wkhtmltopdf.

Run in the Ubuntu 24.04 build environment, never on an end user's machine.
Only checksum-verified upstream downloads and build-system files are bundled.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request


POSTGRES_VERSION = '18.6'
POSTGRES_SHA256 = '555610c24d53e4316da5b7d3fc25c279d96856d5e0e23ee308c328c5fa881d9f'
POSTGRES_URL = f'https://ftp.postgresql.org/pub/source/v{POSTGRES_VERSION}/postgresql-{POSTGRES_VERSION}.tar.bz2'
WKHTML_VERSION = '0.12.6.1-3'
WKHTML_SHA256 = '4f723b2691ad8638a9df960e0421d346d7315083e3583a334f33362280ddba15'
WKHTML_URL = f'https://github.com/wkhtmltopdf/packaging/releases/download/{WKHTML_VERSION}/wkhtmltox_{WKHTML_VERSION}.jammy_amd64.deb'
PG_PROGRAMS = ('postgres', 'initdb', 'pg_ctl', 'psql', 'pg_dump', 'pg_restore', 'createdb')
WK_PROGRAMS = ('wkhtmltopdf', 'wkhtmltoimage')
# The host's glibc/loader must remain a matched pair; never redistribute one half.
SYSTEM_LIBRARIES = re.compile(r'^(?:ld-linux.*|lib(?:c|m|pthread|dl|rt|resolv|util|anl)\.so(?:\..*)?)$')


def download(url: str, digest: str, cache: Path) -> Path:
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / url.rsplit('/', 1)[-1]
    if not target.exists():
        temporary = target.with_suffix(target.suffix + '.download')
        try:
            with urllib.request.urlopen(url, timeout=120) as response, temporary.open('wb') as output:
                shutil.copyfileobj(response, output)
            with temporary.open('rb') as stream:
                if hashlib.file_digest(stream, 'sha256').hexdigest() != digest:
                    raise ValueError(f'Checksum mismatch for {target.name}')
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
    with target.open('rb') as stream:
        if hashlib.file_digest(stream, 'sha256').hexdigest() != digest:
            raise ValueError(f'Cached download checksum mismatch: {target}')
    return target


def postgres(cache: Path) -> Path:
    archive = download(POSTGRES_URL, POSTGRES_SHA256, cache)
    installed = cache / f'postgresql-{POSTGRES_VERSION}-openssl-noicu'
    if (installed / 'complete').is_file():
        return installed / 'postgres'
    # Build scratch is discarded, including when compilation fails.
    with tempfile.TemporaryDirectory(prefix='odoo-postgres-build-') as directory:
        scratch = Path(directory)
        with tarfile.open(archive) as stream:
            stream.extractall(scratch, filter='data')
        source = scratch / f'postgresql-{POSTGRES_VERSION}'
        subprocess.run(['./configure', '--prefix=/opt/odoo-desktop/postgres', '--with-openssl',
                        '--without-icu', '--without-readline'], cwd=source, check=True)
        subprocess.run(['make', '-j2'], cwd=source, check=True)
        destdir = scratch / 'install'
        for directory in (source, source / 'contrib/pg_trgm', source / 'contrib/unaccent'):
            subprocess.run(['make', '-j2', 'install', f'DESTDIR={destdir}'], cwd=directory, check=True)
        installed.mkdir(parents=True, exist_ok=True)
        payload = installed / 'postgres'
        if payload.exists():
            shutil.rmtree(payload)
        shutil.copytree(destdir / 'opt/odoo-desktop/postgres', payload)
        shutil.copy2(source / 'COPYRIGHT', payload / 'COPYRIGHT')
        (installed / 'complete').write_text(POSTGRES_SHA256)
    return installed / 'postgres'


def dependencies(executables: list[Path], library_paths: list[Path], output: Path) -> None:
    environment = dict(os.environ, LD_LIBRARY_PATH=os.pathsep.join(map(str, library_paths)))
    for executable in executables:
        result = subprocess.run(['ldd', str(executable)], env=environment, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if result.returncode or 'not found' in result.stdout:
            raise RuntimeError(f'Cannot resolve native dependencies of {executable}: {result.stdout} {result.stderr}')
        for line in result.stdout.splitlines():
            match = re.search(r'(?:=>\s+)?(/\S+)', line)
            if not match:
                continue
            path = Path(match[1])
            if not SYSTEM_LIBRARIES.fullmatch(path.name):
                target = output / path.name
                if not target.exists():
                    shutil.copy2(path.resolve(), target)


def wrapper(name: str, relative_binary: str, directory: Path) -> None:
    path = directory / 'bin' / name
    path.write_text('#!/bin/sh\nset -eu\n'
        'base=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)\n'
        'export LD_LIBRARY_PATH="$base/lib:$base/postgres/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"\n'
        'export FONTCONFIG_FILE="$base/etc/fonts/fonts.conf"\n'
        'export MAGIC="$base/share/misc/magic.mgc"\n'
        f'exec "$base/{relative_binary}" "$@"\n')
    path.chmod(0o755)


def stage(runtime: Path) -> None:
    if platform.system() != 'Linux' or platform.machine() != 'x86_64':
        raise RuntimeError('Native bundles currently require Linux x86_64 (Ubuntu 24.04 builder).')
    cache = Path(os.environ.get('XDG_CACHE_HOME', Path.home() / '.cache')) / 'odoo-desktop/native'
    pg = postgres(cache)
    deb = download(WKHTML_URL, WKHTML_SHA256, cache)
    target = runtime / 'native'
    if target.exists():
        shutil.rmtree(target)
    for folder in ('bin', 'lib', 'licenses', 'etc/fonts', 'share/fonts', 'share/misc'):
        (target / folder).mkdir(parents=True, exist_ok=True)
    shutil.copytree(pg, target / 'postgres')
    # Debian archives contain a compressed tar payload; extract only into scratch.
    with tempfile.TemporaryDirectory(prefix='odoo-wkhtml-') as directory:
        members = subprocess.check_output(['ar', 't', str(deb)], text=True).splitlines()
        member = next(name for name in members if name.startswith('data.tar.'))
        payload = Path(directory) / member
        with payload.open('wb') as output:
            subprocess.run(['ar', 'p', str(deb), member], stdout=output, check=True)
        with tarfile.open(payload) as stream:
            stream.extractall(directory, filter='data')
        shutil.copytree(Path(directory) / 'usr/local', target / 'wkhtmltox')
        for copyright in (Path(directory) / 'usr/share/doc').rglob('copyright'):
            shutil.copy2(copyright, target / 'licenses/wkhtmltox-copyright')
    for name in PG_PROGRAMS:
        wrapper(name, f'postgres/bin/{name}', target)
    for name in WK_PROGRAMS:
        wrapper(name, f'wkhtmltox/bin/{name}', target)
    inputs = [target / f'postgres/bin/{name}' for name in PG_PROGRAMS]
    inputs += [target / f'wkhtmltox/bin/{name}' for name in WK_PROGRAMS]
    inputs += list((target / 'postgres/lib').rglob('*.so'))
    inputs += list((runtime / 'python/lib').rglob('*.so'))
    # python-magic loads this with ctypes, so ELF scanning alone cannot find it.
    magic = next(Path('/usr/lib').glob('*/libmagic.so.1'))
    shutil.copy2(magic.resolve(), target / 'lib/libmagic.so.1')
    inputs.append(magic)
    dependencies(inputs, [target / 'lib', target / 'postgres/lib'], target / 'lib')
    shutil.copy2('/usr/lib/file/magic.mgc', target / 'share/misc/magic.mgc')
    fonts = Path('/usr/share/fonts/truetype/dejavu')
    if not fonts.is_dir():
        raise RuntimeError('Install fonts-dejavu-core in the build environment.')
    shutil.copytree(fonts, target / 'share/fonts/dejavu')
    (target / 'etc/fonts/fonts.conf').write_text('''<?xml version="1.0"?>
<!DOCTYPE fontconfig SYSTEM "urn:fontconfig:fonts.dtd">
<fontconfig><dir prefix="relative">../../share/fonts</dir><dir>/usr/share/fonts</dir>
<cachedir prefix="xdg">fontconfig</cachedir></fontconfig>
''')
    # Include attribution for copied distro libraries, fonts, and upstream payloads.
    for copyright in Path('/usr/share/doc').glob('*/copyright'):
        if copyright.is_file():
            shutil.copy2(copyright, target / 'licenses' / f'{copyright.parent.name}-copyright')
    (target / 'sources.json').write_text(json.dumps({
        'postgres': {'version': POSTGRES_VERSION, 'url': POSTGRES_URL, 'sha256': POSTGRES_SHA256},
        'wkhtmltopdf': {'version': WKHTML_VERSION, 'url': WKHTML_URL, 'sha256': WKHTML_SHA256},
    }, indent=2) + '\n')
    for name in (*PG_PROGRAMS, *WK_PROGRAMS):
        version = subprocess.check_output([str(target / 'bin' / name), '--version'], text=True)
        if name.startswith('wkhtmlto') and 'with patched qt' not in version:
            raise RuntimeError('Odoo requires the patched-Qt wkhtmltopdf distribution.')
    print(f'Native runtime staged: PostgreSQL {POSTGRES_VERSION}, wkhtmltopdf {WKHTML_VERSION}')


if __name__ == '__main__':
    import sys
    stage(Path(sys.argv[1]).resolve())
