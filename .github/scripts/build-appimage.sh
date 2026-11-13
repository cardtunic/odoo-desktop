#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/../.."
source_root="$PWD"
export CARGO_INCREMENTAL=0
export CARGO_BUILD_JOBS="${CARGO_BUILD_JOBS:-2}"
export APPIMAGE_EXTRACT_AND_RUN=1

uv sync --locked
uv run --no-sync python -m unittest discover -s desktop/scripts -p 'test_*.py'
pnpm --dir desktop install --frozen-lockfile
pnpm --dir desktop build

# Extract and exercise the packaged interpreter, not the development venv.
# This catches missing files/dependencies without opening a GUI. The native
# smoke test uses only a disposable PostgreSQL cluster, never an existing DB.
bundle="$PWD/desktop/src-tauri/target/release/bundle/appimage"
mapfile -d '' images < <(find "$bundle" -maxdepth 1 -type f -name '*.AppImage' -print0)
if [[ ${#images[@]} -ne 1 ]]; then
  echo "Expected exactly one AppImage, found ${#images[@]}" >&2
  exit 1
fi
smoke=$(mktemp -d)
trap 'rm -rf "$smoke"' EXIT
(
  cd "$smoke"
  # The extract-and-run override would otherwise launch the GUI during extraction.
  env -u APPIMAGE_EXTRACT_AND_RUN "${images[0]}" --appimage-extract >/dev/null
  mapfile -d '' interpreters < <(find squashfs-root -path '*/runtime/python/bin/python3' -print0)
  if [[ ${#interpreters[@]} -ne 1 ]]; then
    echo 'Could not locate the packaged Python runtime' >&2
    exit 1
  fi
  python=$(realpath "${interpreters[0]}")
  runtime=$(dirname "$(dirname "$(dirname "$python")")")
  cd "$runtime/app"
  env -u PYTHONPATH PYTHONHOME="$runtime/python" PYTHONNOUSERSITE=1 \
    LD_LIBRARY_PATH="$runtime/native/lib:$runtime/native/postgres/lib" \
    "$python" -m odoo --help >/dev/null
  test -f "$runtime/../managed_database.py"
  env PYTHONHOME="$runtime/python" PYTHONNOUSERSITE=1 \
    LD_LIBRARY_PATH="$runtime/native/lib:$runtime/native/postgres/lib" \
    "$python" "$source_root/desktop/scripts/smoke_native_runtime.py" "$runtime"
  if [[ ${ODOO_TEST_MINIMAL_RUNTIME:-0} == 1 ]]; then
    # No apt-installed build libraries, Docker socket, credentials, or network.
    # Ubuntu's unprivileged uid 1000 is present even when the CI host uid differs.
    relative=${runtime#"$smoke/squashfs-root"}
    docker run --rm --network=none --user 1000:1000 \
      -e "PYTHONHOME=/bundle$relative/python" \
      -e "LD_LIBRARY_PATH=/bundle$relative/native/lib:/bundle$relative/native/postgres/lib" \
      -v "$smoke/squashfs-root:/bundle:ro" \
      -v "$source_root/desktop/scripts:/tests:ro" \
      ubuntu:24.04 "/bundle$relative/python/bin/python3" \
      /tests/smoke_native_runtime.py "/bundle$relative"
  fi
)
(
  cd "$bundle"
  sha256sum -- *.AppImage > SHA256SUMS
  sha256sum -c SHA256SUMS
)
