"""Build with the bundled Python wheels' private library paths visible to linuxdeploy."""

import os
from pathlib import Path
import subprocess
import sys

from prepare_runtime import DESKTOP, DESTINATION, stage
from native_dependencies import stage as stage_native


def main() -> None:
    bundle_only = "--bundle-only" in sys.argv[1:]
    if not bundle_only:
        stage()
        stage_native(DESTINATION)
    if not (DESTINATION / 'native/bin/postgres').is_file():
        raise SystemExit('Native runtime is missing. Run pnpm build before pnpm bundle.')
    libraries = [DESTINATION / "python/lib"]
    libraries += [DESTINATION / 'native/lib', DESTINATION / 'native/postgres/lib']
    for packages in (DESTINATION / "python/lib").glob("python*/site-packages"):
        libraries.extend(path for path in packages.glob("*.libs") if path.is_dir())
    environment = os.environ.copy()
    environment["APPIMAGE_EXTRACT_AND_RUN"] = "1"
    environment["LD_LIBRARY_PATH"] = os.pathsep.join(map(str, libraries)) + (
        os.pathsep + environment["LD_LIBRARY_PATH"] if environment.get("LD_LIBRARY_PATH") else ""
    )
    command = ["pnpm", "exec", "tauri", "bundle" if bundle_only else "build"]
    subprocess.run(command, cwd=DESKTOP, env=environment, check=True)


if __name__ == "__main__":
    main()
