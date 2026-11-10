# Odoo Desktop (Tauri 2, Linux)

A native WebKit window around the existing Odoo UI. The window opens immediately
with a startup message while Rust starts a private, loopback-only Odoo process.
Once ready, the same window loads Odoo. Closing the window, including during
startup, cancels startup and stops Odoo; there is no tray/background mode.
It never creates, migrates, or initializes a database automatically.
Only one desktop instance is allowed. Odoo pages have no Tauri IPC permissions.

## Development

Prerequisites: Rust/Cargo (stable), Node/pnpm, uv, and the Linux Tauri 2 system
dependencies (GTK 3, WebKitGTK 4.1, a C compiler, OpenSSL development files, and
AppImage tooling). See https://v2.tauri.app/start/prerequisites/ for your distro.

From the project root:

```sh
git submodule update --init --recursive
uv sync --locked
cd desktop
pnpm install
pnpm dev
```

Development uses the repository's `odoo.conf`. You can override it with an
absolute path, in both development and packaged builds:

```sh
ODOO_DESKTOP_CONFIG=/absolute/path/to/odoo.conf pnpm dev
```

The database must already contain a compatible Odoo installation. For a **new,
empty** database only, initialize separately from the repository root:

```sh
PGCHANNELBINDING=require uv run python -m odoo -c odoo.conf -i base --without-demo=True --stop-after-init
```

Do not initialize an existing production database.

## Build an AppImage

```sh
git submodule update --init --recursive
cd desktop
pnpm install
pnpm build
```

The build script stages a uv-managed, relocatable CPython 3.14 runtime,
installs the locked dependencies, copies Odoo source/assets, and smoke-tests
`python -m odoo --help`. This can download hundreds of megabytes and may require
native build dependencies for packages such as python-ldap and psycopg2.
It exposes wheel-private shared library paths to linuxdeploy during packaging.
To retry packaging without compiling/staging again, run `pnpm bundle`.
The resulting app does not require uv, system Python, or a checkout of this repo.
No database credentials are bundled. Artifacts are under
`src-tauri/target/release/bundle/appimage/`.

The `desktop` branch is based on Odoo 20.0 commit
`b0d4e4ab7a21af8ac4ef66e8cdd57198f1378f35`, matching the 2026-11-09
nightly distribution. Its design themes are a pinned upstream Git submodule,
not a second copy of the source. Both upstream layouts are merged into the
bundled runtime's `odoo/addons` directory during packaging.

Install the result for your user, copy your existing config on first install,
and register it with KDE/KRunner:

```sh
uv run --project .. python scripts/install_linux.py \
  src-tauri/target/release/bundle/appimage/*.AppImage --config ../odoo.conf
```

This installs `~/AppImages/Odoo-Desktop.AppImage` and
`~/.local/share/applications/odoo-desktop.desktop`. Search KRunner for
**Odoo Desktop**. Reinstalling preserves an existing private configuration.

**Portability needs release testing:** this first target is Linux on the build
machine's architecture. Build on the oldest distro you intend to support. Python
extensions may depend on system libraries (for example libpq, LDAP, and libmagic)
that Tauri does not automatically discover inside Python. Test on a clean target
machine and package those dependencies before distributing as fully standalone.
PDF reports may separately need a supported wkhtmltopdf installation. Windows
and macOS runtime packaging are not implemented.

## Configuration, data, and diagnostics

On first packaged launch, a credentials-free config template is written to
`$XDG_CONFIG_HOME/local.odoo.desktop/odoo.conf` (normally
`~/.config/local.odoo.desktop/odoo.conf`). A dialog tells you to edit it and reopen
the app. The config and log are created with owner-only permissions on Linux.
For Neon, use the **direct** host, not the `-pooler` host, and `db_sslmode = require`.
For local PostgreSQL, use `db_host = 127.0.0.1`, its local database/user/password,
and `db_sslmode = disable` if that local server has SSL disabled. The launcher
honors the config's SSL mode and libpq's default channel-binding preference;
it does not force cloud-only connection settings.

The desktop log (`odoo.log`) lives in
`$XDG_DATA_HOME/local.odoo.desktop/` (normally
`~/.local/share/local.odoo.desktop/`). These persist when the AppImage is replaced.
The launcher overrides the configured port with an available local port.
Odoo's filestore and sessions honor `data_dir` in your config; without it, Odoo
uses its usual `~/.local/share/Odoo/` directory on Linux. This preserves the
attachments of an existing local installation without moving them. Back up
the database and matching `filestore/<database-name>/` directory together.

Startup may take a few minutes on a remote database; an error dialog points to
the log if Odoo exits or is not ready within five minutes. External navigation is
blocked in the app window; third-party Odoo OAuth flows need separate integration.
Google Drive sign-in opens in the system browser. No automatic database upgrade
is provided.

### Reports and downloads

PDF reports require a supported patched-Qt `wkhtmltopdf` installation. Set
`bin_path` in your private `odoo.conf` to the directory containing it and
`wkhtmltoimage`. These renderers are not included in the AppImage.

The app allows downloads from its own backend, including Odoo's generated blob
URLs. Files are staged privately under the desktop data directory; once the
download completes a native **Save As** dialog opens (defaulting to Downloads).
All file pickers (downloads, backup ZIPs, and Google client JSON) use XDG Desktop
Portal, so KDE supplies its own picker on Plasma. Other desktops choose their
configured portal backend. Install/run `xdg-desktop-portal` and the matching
desktop backend, such as `xdg-desktop-portal-kde`; no system-wide configuration
is changed by the app. Message and confirmation dialogs retain their existing
backend so they do not acquire an extra Zenity dependency.
The app also requests portal-backed GTK/WebKit file controls using an app-local
`GTK_USE_PORTAL=1`, set before GTK starts.
After saving, a confirmation shows the exact path. Cancelling removes the staged
file; a save error retains it and shows its temporary path. Downloads do not
silently overwrite files: the native chooser handles overwrite confirmation.

At each backend start the app updates Odoo's `report.url` to its current local
port, so PDF renderers can fetch report CSS, fonts, and images even after a
restart/import. It does not rewrite `web.base.url` or change public-facing links.

This is an installed-data app, not a USB-contained data bundle: settings and
attachments stay in the user's persistent directories.

## Complete ZIP backups and Google Drive

The native **Data** menu offers **Back up to Google Drive…** and **Import backup
ZIP…**. This replaces the earlier remote-database transfer UI. Existing remote
settings and old recovery files are left untouched but are no longer used.

Each backup contains `dump.sql`, `filestore/` files, and `manifest.json` with
Odoo/PostgreSQL versions, installed addon names, and SHA-256 checksums. Desktop
connection settings and OAuth credentials are excluded. The database itself
contains sensitive business data and may contain application-level secrets;
the ZIP is **not password-encrypted**. A missing referenced attachment causes
backup to fail rather than silently upload an incomplete snapshot.

### One-time Google setup

1. Open <https://console.cloud.google.com/> and create/select a project.
2. Enable **Google Drive API** for that project.
3. Configure the OAuth consent screen. For an external app in **Testing** mode,
   add the Google account that will sign in as a test user.
4. Create an **OAuth client ID → Desktop app** and download its JSON file.
5. In Odoo's backup window choose **Choose Google client JSON…**, then
   **Sign in with Google…**. Sign-in opens in your ordinary browser, not an
   embedded webview. Return to Odoo after granting access.

If no browser tab opens, the backup window displays the temporary sign-in URL.
Click it to select, copy it, and paste it into your browser while Odoo stays open.
The URL is removed from status when the operation finishes and cannot complete
sign-in after its loopback listener times out.

No Google client is shipped in this repository. Google testing-mode grants can
expire after seven days and require reconnecting; publishing and any applicable
Google verification/consent requirements belong to the OAuth project's owner.
This integration uses the `drive.file` scope (only files created/opened by this
app), PKCE, a random OAuth state, and a temporary loopback callback listener.
Sign-in can be cancelled in the backup window and times out after three minutes.

Client settings and tokens are saved with mode `0600` in
`~/.config/local.odoo.desktop/google-client.json` and `google-token.json`.
They are private files, **not an encrypted OS keychain**. Disconnect attempts to
revoke the Google grant, removes saved tokens, and leaves existing Drive backups
alone. No credentials or OAuth tokens are passed as command-line arguments.

### Backup and import

Backup creates a new timestamped ZIP in **Google Drive / Odoo Desktop backups**.
It never overwrites a cloud database. A local copy is always retained under
`~/.local/share/local.odoo.desktop/backups/<job>/`, including if upload fails.
Uploads use Google's resumable-upload protocol in chunks; retries after an app
restart currently create a new backup rather than resume an earlier session.

Download any backup ZIP from Drive and use **Choose backup ZIP…** to import it.
Only this app's versioned ZIP format is accepted, not arbitrary ZIP archives.
Import validates paths, checksums, Odoo version, PostgreSQL compatibility,
available addons, and disk space before changing live data. Type the local
database name and accept the native confirmation to replace it and its files.
Only import trusted backups: hashes detect damage, not a maliciously authored SQL
dump. PostgreSQL SQL dumps must be trusted input.

Before import the app saves a complete `recovery-before-import.zip`. Database
replacement uses one transaction; filestore files are staged and renamed on the
same filesystem afterward. A filesystem failure triggers restoration of the
previous database and files. The original filestore is retained in a sibling
`.odoo-before-import-*` directory. PostgreSQL and filesystem changes do not share
one transaction: a power loss/process kill can still require manual recovery.
If automatic recovery fails, Odoo is left stopped and the recovery path is shown.

Odoo pauses during backup/import and restarts afterward. Close is temporarily
blocked during an operation so no job continues invisibly. No background backup
service is installed. Do not use other Odoo clients during these operations.
The existing `odoo-db` Docker container provides PostgreSQL tools; local
PostgreSQL must be on port 5432. Sources must be Odoo 20 with a single `public`
user schema. Importing from a newer PostgreSQL major into an older one is
rejected. Database roles, ownership, and grants are not copied.

### Tests

```sh
uv run --project .. python -m unittest discover -s scripts -p 'test_*.py'
```

Transactional replacement and rollback can be tested against disposable test
databases created in the local container (the real `odoo` database is untouched):

```sh
ODOO_TRANSFER_INTEGRATION=1 uv run --project .. python -m unittest discover -s scripts -p 'test_*.py'
```
