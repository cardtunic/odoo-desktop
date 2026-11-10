"""Stage a relocatable CPython + Odoo runtime without any local credentials.

Run from desktop/ via `pnpm prepare-runtime`. Linux only for this first build.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys


DESKTOP = Path(__file__).resolve().parents[1]
PROJECT = DESKTOP.parent
DESTINATION = DESKTOP / "src-tauri" / "runtime"


def copy_ignore(directory: str, names: list[str]) -> set[str]:
    return {
        name for name in names
        if name in {"__pycache__", ".git", ".venv", "node_modules"}
        or name.endswith((".pyc", ".pyo"))
    }


def stage(destination: Path = DESTINATION) -> None:
    if sys.platform != "linux":
        raise SystemExit("This runtime builder currently supports Linux only.")
    destination.mkdir(parents=True, exist_ok=True)
    # Downloads stay in uv's cache, not in the distributable directory.
    subprocess.run(["uv", "python", "install", "3.14", "--no-bin"], check=True)
    executable = Path(subprocess.check_output(
        ["uv", "python", "find", "--managed-python", "--no-project", "3.14"],
        text=True,
    ).strip()).resolve()
    # python-build-standalone distributions downloaded by uv are relocatable.
    python_root = executable.parent.parent
    # Replace only these generated payloads so repeated builds cannot retain
    # removed modules or fail while overwriting interpreter symlinks.
    for payload in (destination / "python", destination / "app"):
        if payload.exists():
            shutil.rmtree(payload)
    shutil.copytree(python_root, destination / "python",
                    ignore=copy_ignore, symlinks=True)
    requirements = subprocess.check_output(
        ["uv", "export", "--project", str(PROJECT), "--frozen", "--no-dev",
         "--no-emit-project", "--format", "requirements-txt"], text=True,
    )
    requirements_path = destination / "requirements.txt"
    requirements_path.write_text(requirements)
    bundled_python = destination / "python" / "bin" / "python3"
    subprocess.run(
        ["uv", "pip", "install", "--python", str(bundled_python), "--system",
         "--break-system-packages", "--link-mode=copy",
         "-r", str(requirements_path)], check=True,
    )
    # Only Odoo's source and its assets are copied. In particular: no config,
    # database password, home-directory filestore, or development environment.
    shutil.copytree(PROJECT / "odoo", destination / "app" / "odoo",
                     dirs_exist_ok=True, ignore=copy_ignore)
    # Upstream Git checkouts keep most addons at the repository root; nightly
    # source distributions already place them inside odoo/addons.
    if (PROJECT / ".gitmodules").is_file() and not (PROJECT / "design-themes" / "theme_graphene").is_dir():
        raise SystemExit("Missing design themes. Run git submodule update --init --recursive first.")
    for addons in (PROJECT / "addons", PROJECT / "design-themes"):
        if addons.is_dir():
            shutil.copytree(addons, destination / "app" / "odoo" / "addons",
                            dirs_exist_ok=True, ignore=copy_ignore)
    environment = dict(os.environ, PYTHONNOUSERSITE="1")
    environment.pop("PYTHONPATH", None)
    subprocess.run(
        [str(bundled_python), "-m", "odoo", "--help"],
        cwd=destination / "app", env=environment, check=True,
        stdout=subprocess.DEVNULL,
    )
    print(f"Runtime staged and smoke-tested at {destination}")


if __name__ == "__main__":
    stage()
