"""Security-pass regression coverage for BastionExecutor's file-transfer
helpers: credential-bearing files written to the bastion must always be
chmod'd owner-only, closing the window an SFTP server's default create mode
would otherwise leave a manifest/kubeconfig/pull-secret group- or
world-readable on a shared bastion."""

from __future__ import annotations

from pathlib import Path

import paramiko
import pytest

from clusterbuild.core.bastion_exec import BastionError, BastionExecutor


class _FakeFileHandle:
    def __init__(self):
        self.written = b""

    def write(self, data):
        self.written += data.encode() if isinstance(data, str) else data

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class _FakeSftp:
    def __init__(self):
        self.chmod_calls: list[tuple[str, int]] = []
        self.put_calls: list[tuple[str, str]] = []
        self._handles: dict[str, _FakeFileHandle] = {}

    def open(self, remote_path, mode):
        handle = _FakeFileHandle()
        self._handles[remote_path] = handle
        return handle

    def put(self, local_path, remote_path):
        self.put_calls.append((local_path, remote_path))

    def chmod(self, remote_path, mode):
        self.chmod_calls.append((remote_path, mode))

    def close(self):
        pass


class _FakeSshClient:
    def __init__(self, sftp: _FakeSftp):
        self._sftp = sftp

    def open_sftp(self):
        return self._sftp


@pytest.fixture()
def executor():
    ex = BastionExecutor(host="bastion.lab", user="qe")
    ex._client = _FakeSshClient(_FakeSftp())  # noqa: SLF001 -- test needs a connected-looking executor
    return ex


def test_write_file_chmods_owner_only_by_default(executor):
    executor.write_file("/home/qe/install/auth/kubeconfig", "apiVersion: v1\n")

    sftp = executor._client._sftp  # noqa: SLF001
    assert sftp.chmod_calls == [("/home/qe/install/auth/kubeconfig", 0o600)]


def test_write_file_honors_explicit_mode(executor):
    executor.write_file("/home/qe/install/run.sh", "#!/bin/sh\n", mode=0o700)

    sftp = executor._client._sftp  # noqa: SLF001
    assert sftp.chmod_calls == [("/home/qe/install/run.sh", 0o700)]


def test_upload_from_local_chmods_owner_only_by_default(executor, tmp_path):
    local = tmp_path / "mgmt-kubeconfig"
    local.write_text("apiVersion: v1\n")

    executor.upload_from_local(local, "/home/qe/install/management-kubeconfig")

    sftp = executor._client._sftp  # noqa: SLF001
    assert sftp.put_calls == [(str(local), "/home/qe/install/management-kubeconfig")]
    assert sftp.chmod_calls == [("/home/qe/install/management-kubeconfig", 0o600)]


def test_connect_auth_failure_without_password_hints_at_password_flags(monkeypatch):
    """Security-pass follow-up: an AuthenticationException with no password
    supplied should nudge the operator toward `bastion register
    --password`/`--ask-password` rather than raising a generic SSH error."""

    def _raise_auth_failure(self, *args, **kwargs):
        raise paramiko.AuthenticationException("Authentication failed.")

    monkeypatch.setattr(paramiko.SSHClient, "load_system_host_keys", lambda self: None)
    monkeypatch.setattr(paramiko.SSHClient, "connect", _raise_auth_failure)

    ex = BastionExecutor(host="bastion.lab", user="qe")
    with pytest.raises(BastionError, match="no SSH key/agent auth succeeded and no password was supplied"):
        ex.connect()


def test_connect_auth_failure_with_password_hints_password_was_rejected(monkeypatch):
    def _raise_auth_failure(self, *args, **kwargs):
        raise paramiko.AuthenticationException("Authentication failed.")

    monkeypatch.setattr(paramiko.SSHClient, "load_system_host_keys", lambda self: None)
    monkeypatch.setattr(paramiko.SSHClient, "connect", _raise_auth_failure)

    ex = BastionExecutor(host="bastion.lab", user="qe")
    with pytest.raises(BastionError, match="neither SSH key/agent auth nor the supplied password were accepted"):
        ex.connect(password="wrong")


def test_connect_non_auth_ssh_failure_hints_at_host_keys(monkeypatch):
    def _raise_ssh_failure(self, *args, **kwargs):
        raise paramiko.SSHException("Server not found.")

    monkeypatch.setattr(paramiko.SSHClient, "load_system_host_keys", lambda self: None)
    monkeypatch.setattr(paramiko.SSHClient, "connect", _raise_ssh_failure)

    ex = BastionExecutor(host="bastion.lab", user="qe")
    with pytest.raises(BastionError, match="known_hosts"):
        ex.connect()
