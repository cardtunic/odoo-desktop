"""Install a built AppImage and a per-user KRunner/application-menu entry."""

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


def desktop_quote(path: Path) -> str:
    # Desktop Entry Exec quoting is not shell quoting.
    value = str(path).replace("%", "%%")
    for character in ("\\", '"', "`", "$"):
        value = value.replace(character, "\\" + character)
    return '"' + value + '"'


def install(image: Path, config: Path | None) -> None:
    desktop = Path(__file__).resolve().parents[1]
    home = Path.home()
    data = Path(os.environ.get("XDG_DATA_HOME", home / ".local/share"))
    settings = Path(os.environ.get("XDG_CONFIG_HOME", home / ".config")) / "local.odoo.desktop"
    applications = data / "applications"
    icons = data / "icons/hicolor/128x128/apps"
    target = home / "AppImages/Odoo-Desktop.AppImage"
    for directory in (settings, applications, icons, target.parent):
        directory.mkdir(parents=True, exist_ok=True)
    destination_config = settings / "odoo.conf"
    if not destination_config.exists():
        source = config or desktop / "odoo.conf.example"
        # Exclusive, owner-only creation: never replace an existing user config.
        with open(destination_config, "x", opener=lambda path, flags: os.open(path, flags, 0o600)) as output:
            output.write(source.read_text())
        print(f"Created private configuration: {destination_config}")
    else:
        print(f"Preserved existing configuration: {destination_config}")
    # Replace atomically: an older, still-mounted AppImage must not be
    # truncated while its error dialog/window is still open.
    with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".odoo-install-", delete=False) as temporary:
        staged = Path(temporary.name)
    try:
        shutil.copy2(image, staged)
        staged.chmod(0o755)
        staged.replace(target)
    finally:
        staged.unlink(missing_ok=True)
    shutil.copy2(desktop / "src-tauri/icons/icon.png", icons / "odoo-desktop.png")
    entry = applications / "odoo-desktop.desktop"
    entry.write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=Odoo Desktop\n"
        "Comment=Local Odoo connected to your PostgreSQL database\n"
        f"Exec={desktop_quote(target)}\n"
        f"TryExec={target}\n"
        "Icon=odoo-desktop\n"
        "Terminal=false\n"
        "Categories=Office;Finance;\n"
        "Keywords=Odoo;ERP;CRM;\n"
        "StartupNotify=true\n"
    )
    for command in (
        ["desktop-file-validate", str(entry)],
        ["update-desktop-database", str(applications)],
        ["kbuildsycoca6", "--noincremental"],
    ):
        if shutil.which(command[0]):
            subprocess.run(command, check=True)
    print(f"Installed {target}\nLauncher: {entry}\nSearch KRunner for Odoo Desktop.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("appimage", type=Path)
    parser.add_argument("--config", type=Path, help="Copy this config on first install only")
    args = parser.parse_args()
    if not args.appimage.is_file():
        parser.error("AppImage does not exist")
    install(args.appimage, args.config)
