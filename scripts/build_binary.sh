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

REPO_ROOT="$(pwd)"
rm -rf build/pyinstaller dist/clusterbuild

pyinstaller \
  --name clusterbuild \
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

echo
echo "Built dist/clusterbuild -- copy this single file to a team member's"
echo "PATH (e.g. /usr/local/bin/clusterbuild); no Python install required."
echo "Smoke test: dist/clusterbuild version && dist/clusterbuild doctor"
