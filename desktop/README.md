# Odoo Desktop (Tauri 2, Linux)

A native WebKit window around the existing Odoo UI. The window opens immediately
with a startup message while Rust starts a private, loopback-only Odoo process.
Once ready, the same window loads Odoo. Closing the window, including during
startup, cancels startup and stops Odoo and its managed PostgreSQL server;
there is no tray/background mode. Fresh installs initialize their own private
local database. Existing database configurations are never initialized,
migrated, or replaced automatically.
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

Development uses the repository's `odoo.conf` when present; otherwise it uses
the same private managed configuration as packaged builds. For a new managed
installation, stage the native runtime with `pnpm build` in the Ubuntu build
environment first. You can override the config with an
absolute path, in both development and packaged builds:

```sh
ODOO_DESKTOP_CONFIG=/absolute/path/to/odoo.conf pnpm dev
```

An externally configured database must already contain a compatible Odoo
installation. For a **new,
empty** database only, initialize separately from the repository root:

```sh
PGCHANNELBINDING=require uv run python -m odoo -c odoo.conf -i base --without-demo=True --stop-after-init
```

Do not initialize an existing production database.

## Build an AppImage

### Automatic builds

The original `v0.1.0` release predates native dependency bundling. Use a build
artifact from the updated `desktop` branch until a new release is published.

GitHub Actions builds a Linux x86_64 AppImage on Ubuntu 24.04 after every
branch push. You can also start **Build AppImage** manually from the Actions tab.
The workflow runs the desktop unit tests, builds the app, extracts the AppImage,
and tests its bundled Python/Odoo runtime. An isolated disposable PostgreSQL
cluster exercises first-run initialization, authentication, persistence,
ZIP backup/restore, transactional rollback, and shutdown. PDF and PNG rendering
are also checked. No existing database or user configuration is used.
CI repeats the runtime checks in a minimal Ubuntu container with networking
disabled, to catch dependencies accidentally supplied by the build runner.

Download the artifact from the completed Actions run. It contains the AppImage
and `SHA256SUMS` and is retained for 14 days. These are development builds;
the workflow does not create releases or replace the latest release.
After extracting the artifact ZIP, restore executable permission with
`chmod +x ./*.AppImage` and verify it with `sha256sum -c SHA256SUMS`.

To reproduce the build on Ubuntu 24.04 with Node 24, pnpm 12.3.4, uv,
and stable Rust installed, run from the repository root:

```sh
sudo bash .github/scripts/install-linux-deps.sh
git submodule update --init --recursive
bash .github/scripts/build-appimage.sh
```

Set `ODOO_TEST_MINIMAL_RUNTIME=1` when running that script on a Docker-enabled
host to also run CI's minimal-container runtime check.

Alternatively, from the repository root, test the same build in Docker
(allow at least 12 GB of free disk space):

```sh
docker build -f .github/containers/appimage.Dockerfile -t odoo-desktop-builder .
mkdir -p .ci-cache
docker run --rm --cpus=2 --user "$(id -u):$(id -g)" \
  -e HOME=/tmp/build-home -e UV_LINK_MODE=copy \
  -v "$PWD/.ci-cache:/tmp/build-home" \
  -v "$PWD:/workspace" odoo-desktop-builder
```

The local container is limited to two CPU cores, including AppImage compression,
so packaging does not consume every host core. It downloads build
tools/dependencies and writes `.ci-cache`, `.venv`,
`desktop/node_modules`, and `desktop/src-tauri/target` in your checkout.
It does not mount your home directory or connect to your database.

### Manual builds

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
It builds PostgreSQL 18.6 from checksum-pinned upstream source (including
`pg_trgm` and `unaccent`) and bundles patched-Qt wkhtmltopdf/wkhtmltoimage
0.12.6.1-3, fonts, client tools, and their shared libraries. Downloads are
verified against pinned SHA-256 checksums; source URLs and license notices are
included under `runtime/native`. Native packaging targets Ubuntu 24.04 x86_64;
use the Docker builder above when developing on another distro.
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

Install the result for your user and
and register it with KDE/KRunner:

```sh
uv run --project .. python scripts/install_linux.py \
  src-tauri/target/release/bundle/appimage/*.AppImage
```

This installs `~/AppImages/Odoo-Desktop.AppImage` and
`~/.local/share/applications/odoo-desktop.desktop`. Search KRunner for
**Odoo Desktop**. Reinstalling preserves an existing private configuration.
To use an existing database instead, add `--config ../odoo.conf` on first
installation. Never copy another installation's managed metadata without also
restoring its matching database and attachments.

**Portability:** the x86_64 AppImage bundles PostgreSQL, PDF renderers, fonts,
libpq, LDAP, libmagic (including its database), and scanned shared-library
dependencies. It still requires a compatible Linux desktop, glibc (Ubuntu 24.04
or newer baseline), and desktop portal services. No glibc/loader is bundled.
Windows and macOS runtime packaging are not implemented.

## Configuration, data, and diagnostics

On first launch without an existing config, private credentials are generated at
`$XDG_CONFIG_HOME/local.odoo.desktop/odoo.conf` (normally
`~/.config/local.odoo.desktop/odoo.conf`). A matching `odoo.conf.managed.json`
records ownership and recovery credentials. Both files are owner-readable only.
The app initializes a new PostgreSQL cluster and Odoo database, listens only on
127.0.0.1 with a random available port and SCRAM authentication, and gives Odoo
a non-superuser database role. The initial web login is `admin` with a random
password shown once in a dialog and saved privately in `first-login.txt` beside
the config. Change it in Odoo and delete that file afterward.

The cluster lives at `$XDG_DATA_HOME/local.odoo.desktop/postgres/cluster`;
managed Odoo sessions/attachments live under the sibling `odoo` directory.
Neither is inside the AppImage. Updating the app preserves them. A file lock
prevents concurrent ownership, and mismatched PostgreSQL major versions or
incomplete databases fail safely rather than being erased or auto-upgraded.
Keep the private configuration/metadata with a backup. Never delete the cluster
to resolve a startup error; check `odoo.log` and restore or migrate explicitly.

Existing external configs are left untouched: no new cluster, initialization,
or migration is performed for them. For an external database, copy a configured
file before first launch or set `ODOO_DESKTOP_CONFIG` to an existing file.
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

Patched-Qt `wkhtmltopdf` and `wkhtmltoimage` are bundled and exposed to Odoo
through an app-local PATH. No separate installation or `bin_path` is needed.
An explicitly configured external `bin_path` retains Odoo's normal precedence.

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
Bundled PostgreSQL client tools handle backups without Docker or system
PostgreSQL clients. The app-managed server stays running while Odoo pauses for
backup/import, and stops when the desktop closes. Existing local servers are
also supported; no fixed port is required. Sources must be Odoo 20 with a single `public`
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
