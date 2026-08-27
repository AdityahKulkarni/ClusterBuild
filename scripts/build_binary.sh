#!/usr/bin/env bash
# Build a single-file `clusterbuild` binary with PyInstaller, for teams that
# don't want to manage a Python environment (see README.md "Distribution").
#
# The catalog + environment profiles are plain YAML files loaded at runtime
# via importlib.resources (core/config.py's bundled_catalog_dir()/
# bundled_environments_dir()) -- PyInstaller doesn't pick those up
# automatically the way a wheel's package-data does, so they're added
# explicitly below.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

if ! python -c "import PyInstaller" >/dev/null 2>&1; then
  echo "PyInstaller not installed -- run: pip install '.[build]'" >&2
  exit 1
fi

# Asset name matches what scripts/install.sh looks for in a GitHub Release,
# e.g. clusterbuild-linux-x86_64 / clusterbuild-darwin-arm64. PyInstaller
# can't cross-compile, so this must be run once per target OS/arch (see
# README.md "Releasing a new version").
OS="$(uname -s | tr '[:upper:]' '[:lower:]')"
case "$OS" in
  linux) OS="linux" ;;
  darwin) OS="darwin" ;;
  *) echo "Unsupported OS for prebuilt binary: $OS" >&2; exit 1 ;;
esac
ARCH="$(uname -m)"
case "$ARCH" in
  x86_64|amd64) ARCH="x86_64" ;;
  arm64|aarch64) ARCH="arm64" ;;
  *) echo "Unsupported architecture for prebuilt binary: $ARCH" >&2; exit 1 ;;
esac
ASSET_NAME="clusterbuild-${OS}-${ARCH}"

REPO_ROOT="$(pwd)"
rm -rf build/pyinstaller "dist/${ASSET_NAME}"

pyinstaller \
  --name "${ASSET_NAME}" \
  --onefile \
  --clean \
  --distpath dist \
  --workpath build/pyinstaller \
  --specpath build/pyinstaller \
  --add-data "${REPO_ROOT}/clusterbuild/catalog:clusterbuild/catalog" \
  --add-data "${REPO_ROOT}/clusterbuild/environments:clusterbuild/environments" \
  --collect-all keyring \
  --hidden-import keyring.backends \
  "${REPO_ROOT}/clusterbuild/cli/main.py"

(cd dist && sha256sum "${ASSET_NAME}" > "${ASSET_NAME}.sha256")

echo
echo "Built dist/${ASSET_NAME} (+ .sha256 checksum)."
echo "For a team release: upload both files as-is to the GitHub Release for"
echo "this version -- scripts/install.sh downloads this exact asset name."
echo "For local use: copy dist/${ASSET_NAME} anywhere on PATH as 'clusterbuild'."
echo "Smoke test: dist/${ASSET_NAME} version && dist/${ASSET_NAME} doctor"
