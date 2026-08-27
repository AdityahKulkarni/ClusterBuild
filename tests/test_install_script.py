"""End-to-end coverage for scripts/install.sh against a throwaway local HTTP
server standing in for a GitHub Release (via CLUSTERBUILD_BASE_URL) -- exercises
the real bash script, not a reimplementation of it, so a regression in the
actual download/verify/install logic gets caught."""

from __future__ import annotations

import hashlib
import http.server
import os
import platform
import stat
import subprocess
import threading
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_SH = REPO_ROOT / "scripts" / "install.sh"


def _asset_name() -> str:
    os_name = {"Linux": "linux", "Darwin": "darwin"}.get(platform.system())
    arch = {"x86_64": "x86_64", "AMD64": "x86_64", "arm64": "arm64", "aarch64": "arm64"}.get(platform.machine())
    if os_name is None or arch is None:
        pytest.skip(f"unsupported test-runner platform: {platform.system()}/{platform.machine()}")
    return f"clusterbuild-{os_name}-{arch}"


@pytest.fixture()
def fake_release_server(tmp_path):
    """Serves a fake release asset (+ correct/incorrect checksum, chosen per
    test) out of a temp directory over plain HTTP on localhost."""
    directory = tmp_path / "release"
    directory.mkdir()

    handler = lambda *args, **kwargs: http.server.SimpleHTTPRequestHandler(*args, directory=str(directory), **kwargs)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, directory
    finally:
        server.shutdown()
        thread.join(timeout=5)


def _write_fake_asset(directory: Path, content: bytes = b"#!/bin/sh\necho fake-clusterbuild\n") -> str:
    asset = _asset_name()
    (directory / asset).write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()
    (directory / f"{asset}.sha256").write_text(f"{digest}  {asset}\n")
    return asset


def _run_installer(base_url: str, install_dir: Path, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["CLUSTERBUILD_BASE_URL"] = base_url
    env["CLUSTERBUILD_INSTALL_DIR"] = str(install_dir)
    env.update(env_extra or {})
    return subprocess.run(
        ["bash", str(INSTALL_SH)], env=env, capture_output=True, text=True, timeout=30
    )


def test_install_downloads_verifies_and_installs_binary(fake_release_server, tmp_path):
    server, directory = fake_release_server
    _write_fake_asset(directory)
    install_dir = tmp_path / "bin"

    result = _run_installer(f"http://127.0.0.1:{server.server_port}", install_dir)

    assert result.returncode == 0, result.stderr
    installed = install_dir / "clusterbuild"
    assert installed.exists()
    assert stat.S_IMODE(installed.stat().st_mode) & stat.S_IXUSR
    assert "Installed clusterbuild to" in result.stderr


def test_install_aborts_on_checksum_mismatch(fake_release_server, tmp_path):
    server, directory = fake_release_server
    asset = _asset_name()
    (directory / asset).write_bytes(b"#!/bin/sh\necho fake\n")
    (directory / f"{asset}.sha256").write_text("0" * 64 + f"  {asset}\n")
    install_dir = tmp_path / "bin"

    result = _run_installer(f"http://127.0.0.1:{server.server_port}", install_dir)

    assert result.returncode != 0
    assert "checksum mismatch" in result.stderr
    assert not (install_dir / "clusterbuild").exists()


def test_install_proceeds_without_checksum_file(fake_release_server, tmp_path):
    server, directory = fake_release_server
    asset = _asset_name()
    (directory / asset).write_bytes(b"#!/bin/sh\necho fake\n")
    install_dir = tmp_path / "bin"

    result = _run_installer(f"http://127.0.0.1:{server.server_port}", install_dir)

    assert result.returncode == 0, result.stderr
    assert "no .sha256 checksum found" in result.stderr
    assert (install_dir / "clusterbuild").exists()


def test_install_fails_clearly_when_asset_missing(fake_release_server, tmp_path):
    server, _directory = fake_release_server
    install_dir = tmp_path / "bin"

    result = _run_installer(f"http://127.0.0.1:{server.server_port}", install_dir)

    assert result.returncode != 0
    assert "download failed" in result.stderr
