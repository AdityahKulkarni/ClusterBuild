#!/usr/bin/env bash
# ClusterBuild installer: downloads the prebuilt single-file `clusterbuild`
# binary for your OS/arch from a GitHub Release, verifies its checksum, and
# installs it onto your PATH. No Python environment required.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/AdityahKulkarni/ClusterBuild/main/scripts/install.sh | bash
#
# Env vars (all optional):
#   CLUSTERBUILD_VERSION   Release tag to install, e.g. "v0.2.0" (default: latest)
#   CLUSTERBUILD_INSTALL_DIR   Directory to install into (default: ~/.local/bin,
#                               or /usr/local/bin if writable and preferred)
#   CLUSTERBUILD_BASE_URL   Override the GitHub Releases URL entirely (e.g. an
#                            internal mirror, or a local dir:// for testing);
#                            the asset/checksum are fetched as "$CLUSTERBUILD_BASE_URL/<asset>[.sha256]"
set -euo pipefail

REPO="AdityahKulkarni/ClusterBuild"
VERSION="${CLUSTERBUILD_VERSION:-latest}"

log() { printf '%s\n' "$*" >&2; }
die() { log "error: $*"; exit 1; }

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "required command '$1' not found -- install it and retry"
}
need_cmd curl
need_cmd uname
need_cmd mktemp

detect_os() {
  case "$(uname -s)" in
    Linux) echo "linux" ;;
    Darwin) echo "darwin" ;;
    *) die "unsupported OS: $(uname -s) (ClusterBuild ships prebuilt binaries for Linux and macOS only -- see README.md for a source install)" ;;
  esac
}

detect_arch() {
  case "$(uname -m)" in
    x86_64|amd64) echo "x86_64" ;;
    arm64|aarch64) echo "arm64" ;;
    *) die "unsupported architecture: $(uname -m)" ;;
  esac
}

OS="$(detect_os)"
ARCH="$(detect_arch)"
ASSET="clusterbuild-${OS}-${ARCH}"

if [ -n "${CLUSTERBUILD_BASE_URL:-}" ]; then
  BASE_URL="$CLUSTERBUILD_BASE_URL"
else
  if [ "$VERSION" = "latest" ]; then
    RELEASE_PATH="latest/download"
    log "Resolving latest ClusterBuild release ..."
  else
    RELEASE_PATH="download/${VERSION}"
  fi
  BASE_URL="https://github.com/${REPO}/releases/${RELEASE_PATH}"
fi

BINARY_URL="${BASE_URL}/${ASSET}"
CHECKSUM_URL="${BINARY_URL}.sha256"

WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

log "Downloading ${ASSET} from ${REPO} (${VERSION}) ..."
if ! curl -fsSL -o "${WORKDIR}/${ASSET}" "$BINARY_URL"; then
  die "download failed: ${BINARY_URL}
Check that a release exists with a '${ASSET}' asset -- see README.md 'Releasing a new version'
if you're building/publishing this yourself, or pass CLUSTERBUILD_VERSION=vX.Y.Z to pin one."
fi

if curl -fsSL -o "${WORKDIR}/${ASSET}.sha256" "$CHECKSUM_URL" 2>/dev/null; then
  log "Verifying checksum ..."
  EXPECTED="$(awk '{print $1}' "${WORKDIR}/${ASSET}.sha256")"
  if command -v sha256sum >/dev/null 2>&1; then
    ACTUAL="$(sha256sum "${WORKDIR}/${ASSET}" | awk '{print $1}')"
  elif command -v shasum >/dev/null 2>&1; then
    ACTUAL="$(shasum -a 256 "${WORKDIR}/${ASSET}" | awk '{print $1}')"
  else
    die "no sha256sum/shasum available to verify checksum"
  fi
  [ "$EXPECTED" = "$ACTUAL" ] || die "checksum mismatch for ${ASSET} (expected ${EXPECTED}, got ${ACTUAL}) -- aborting"
else
  log "warning: no .sha256 checksum found for this release asset -- skipping verification"
fi

chmod +x "${WORKDIR}/${ASSET}"

if [ -n "${CLUSTERBUILD_INSTALL_DIR:-}" ]; then
  INSTALL_DIR="$CLUSTERBUILD_INSTALL_DIR"
elif [ -w "/usr/local/bin" ] 2>/dev/null; then
  INSTALL_DIR="/usr/local/bin"
else
  INSTALL_DIR="${HOME}/.local/bin"
fi
mkdir -p "$INSTALL_DIR"
mv "${WORKDIR}/${ASSET}" "${INSTALL_DIR}/clusterbuild"

log "Installed clusterbuild to ${INSTALL_DIR}/clusterbuild"

case ":${PATH}:" in
  *":${INSTALL_DIR}:"*) ;;
  *)
    log ""
    log "NOTE: ${INSTALL_DIR} is not on your PATH. Add it, e.g.:"
    log "  echo 'export PATH=\"${INSTALL_DIR}:\$PATH\"' >> ~/.bashrc && source ~/.bashrc"
    ;;
esac

log ""
log "Run 'clusterbuild version' and 'clusterbuild doctor run' to verify the install."
