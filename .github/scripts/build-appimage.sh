#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/../.."
export CARGO_INCREMENTAL=0
export CARGO_BUILD_JOBS="${CARGO_BUILD_JOBS:-2}"
export APPIMAGE_EXTRACT_AND_RUN=1

uv sync --locked
uv run --no-sync python -m unittest discover -s desktop/scripts -p 'test_*.py'
pnpm --dir desktop install --frozen-lockfile
pnpm --dir desktop build

# Extract and exercise the packaged interpreter, not the development venv.
# This catches missing runtime files/dependencies without opening a GUI or DB.
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
    "$python" -m odoo --help >/dev/null
)
(
  cd "$bundle"
  sha256sum -- *.AppImage > SHA256SUMS
  sha256sum -c SHA256SUMS
)
