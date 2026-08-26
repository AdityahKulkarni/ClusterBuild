"""Bastion Executor: SSH into the user's RHEL bastion (Phase 1).

Wraps paramiko so the rest of the app never shells out to `ssh` directly.
Responsible for: verifying required CLI tooling is present, managing the
install directory, writing manifests, running installer/govc commands,
tailing remote output, and backing up install-config.yaml/agent-config.yaml
to the local `~/.clusterbuild/backups/` tree before install starts.
"""

from __future__ import annotations

import hashlib
import io
import shlex
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

import paramiko

from clusterbuild.core.config import backup_path_for

REQUIRED_TOOLS = {
    "openshift-install": "openshift-install version",
    "oc": "oc version --client",
}
# `govc` is only needed for vSphere-driven VM provisioning (UPI/Agent/Assisted
# on the vsphere/none platforms); `nmstatectl` only for static-IP NMState
# validation. Neither applies to IPI-only bastions (Nutanix REST API, or the
# AWS/Azure/GCP clouds where openshift-install provisions infra itself), so
# both are optional rather than blocking `bastion register`/`verify`.
OPTIONAL_TOOLS = {
    "govc": "govc version",
    "nmstatectl": "nmstatectl --version",
    "hcp": "hcp version",
}


class BastionError(RuntimeError):
    pass


@dataclass(frozen=True)
class CommandResult:
    exit_code: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


class BastionExecutor:
    def __init__(self, host: str, user: str, port: int = 22, key_filename: Optional[str] = None):
        self.host = host
        self.user = user
        self.port = port
        self.key_filename = key_filename
        self._client: Optional[paramiko.SSHClient] = None

    def connect(self, password: Optional[str] = None) -> None:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
        client.load_system_host_keys()
        try:
            client.connect(
                self.host,
                port=self.port,
                username=self.user,
                key_filename=self.key_filename,
                password=password,
                timeout=15,
            )
        except paramiko.SSHException as exc:
            raise BastionError(
                f"SSH connection to {self.user}@{self.host}:{self.port} failed: {exc}. "
                "If this is the first time connecting, add the bastion's host key to "
                "~/.ssh/known_hosts first (ClusterBuild refuses unknown host keys by design)."
            ) from exc
        self._client = client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def __enter__(self) -> "BastionExecutor":
        self.connect()
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def _require_client(self) -> paramiko.SSHClient:
        if self._client is None:
            raise BastionError("Not connected -- call connect() first.")
        return self._client

    def run(self, command: str, timeout: Optional[float] = None) -> CommandResult:
        client = self._require_client()
        stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
        out = stdout.read().decode(errors="replace")
        err = stderr.read().decode(errors="replace")
        exit_code = stdout.channel.recv_exit_status()
        return CommandResult(exit_code=exit_code, stdout=out, stderr=err)

    @contextmanager
    def run_streaming(self, command: str) -> Iterator[paramiko.ChannelFile]:
        """Yield the live stdout channel of a long-running remote command for line-by-line tailing."""
        client = self._require_client()
        _, stdout, _ = client.exec_command(command, get_pty=True)
        try:
            yield stdout
        finally:
            stdout.channel.recv_exit_status()

    # -- tooling verification --------------------------------------------
    def verify_tools(self) -> dict[str, Optional[str]]:
        results: dict[str, Optional[str]] = {}
        for tool, check_cmd in {**REQUIRED_TOOLS, **OPTIONAL_TOOLS}.items():
            result = self.run(check_cmd, timeout=10)
            results[tool] = result.stdout.strip().splitlines()[0] if result.ok and result.stdout.strip() else None
        return results

    def missing_required_tools(self) -> list[str]:
        verified = self.verify_tools()
        return [tool for tool in REQUIRED_TOOLS if not verified.get(tool)]

    # -- install directory / file transfer -------------------------------
    def ensure_dir(self, remote_path: str) -> None:
        result = self.run(f"mkdir -p {shlex.quote(remote_path)}")
        if not result.ok:
            raise BastionError(f"could not create {remote_path} on bastion: {result.stderr}")

    def write_file(self, remote_path: str, content: str) -> None:
        client = self._require_client()
        sftp = client.open_sftp()
        try:
            with sftp.open(remote_path, "w") as fh:
                fh.write(content)
        finally:
            sftp.close()

    def read_file(self, remote_path: str) -> str:
        client = self._require_client()
        sftp = client.open_sftp()
        try:
            with sftp.open(remote_path, "r") as fh:
                return fh.read().decode()
        finally:
            sftp.close()

    def download_to_local(self, remote_path: str, local_path: Path) -> None:
        client = self._require_client()
        sftp = client.open_sftp()
        try:
            local_path.parent.mkdir(parents=True, exist_ok=True)
            sftp.get(remote_path, str(local_path))
        finally:
            sftp.close()

    def upload_from_local(self, local_path: Path, remote_path: str) -> None:
        client = self._require_client()
        sftp = client.open_sftp()
        try:
            sftp.put(str(local_path), remote_path)
        finally:
            sftp.close()

    # -- explicit requirement: backup manifests before install starts ----
    def backup_manifest(self, cluster_name: str, remote_install_dir: str, filename: str) -> tuple[Path, str]:
        """Download `<remote_install_dir>/<filename>` to a local, timestamped backup and
        return (local_backup_path, sha256_checksum)."""
        remote_path = f"{remote_install_dir}/{filename}"
        content = self.read_file(remote_path)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        local_path = backup_path_for(cluster_name, filename, timestamp)
        local_path.write_text(content, encoding="utf-8")
        checksum = hashlib.sha256(content.encode()).hexdigest()
        return local_path, checksum
