"""Phase 2: exercise the vSphere IPI job handler with a fake bastion executor
(no real SSH/vCenter needed) to validate the manifest-build -> backup ->
install -> kubeconfig-retrieval sequencing and status transitions."""

from __future__ import annotations

from contextlib import contextmanager

import pytest

from clusterbuild.core import installers  # noqa: F401  (registers handlers)
from clusterbuild.core.installers import ipi
from clusterbuild.core.state import Bastion, Cluster, get_session


class FakeChannel:
    def __init__(self, exit_code: int):
        self._exit_code = exit_code

    def recv_exit_status(self) -> int:
        return self._exit_code


class FakeStdout:
    def __init__(self, lines: list[str], exit_code: int):
        self._lines = lines
        self.channel = FakeChannel(exit_code)

    def __iter__(self):
        return iter(self._lines)


class FakeBastionExecutor:
    """Stands in for core.bastion_exec.BastionExecutor -- records every call
    the IPI handler makes instead of touching a real SSH connection."""

    instances: list["FakeBastionExecutor"] = []

    def __init__(self, host, user, port=22, key_filename=None):
        self.host, self.user, self.port = host, user, port
        self.connected = False
        self.connect_password = None
        self.dirs_ensured: list[str] = []
        self.written_files: dict[str, str] = {}
        self.commands_run: list[str] = []
        self.exit_code = 0
        FakeBastionExecutor.instances.append(self)

    def connect(self, password=None):
        self.connected = True
        self.connect_password = password

    def close(self):
        self.connected = False

    def ensure_dir(self, path):
        self.dirs_ensured.append(path)

    def write_file(self, remote_path, content):
        self.written_files[remote_path] = content

    def backup_manifest(self, cluster_name, remote_install_dir, filename):
        from pathlib import Path

        return Path(f"/tmp/fake-backup/{cluster_name}/{filename}"), "deadbeef" * 8

    @contextmanager
    def run_streaming(self, command):
        self.commands_run.append(command)
        yield FakeStdout(["Bootstrap complete", "Cluster is ready"], self.exit_code)


@pytest.fixture()
def _fake_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CLUSTERBUILD_HOME", str(tmp_path))
    monkeypatch.setattr(ipi, "BastionExecutor", FakeBastionExecutor)
    monkeypatch.setattr("clusterbuild.core.installers.base.SecretsBackend", _FakeSecretsBackend)
    monkeypatch.setattr(ipi, "SecretsBackend", _FakeSecretsBackend)
    FakeBastionExecutor.instances.clear()


class _FakeSecretsBackend:
    def get(self, namespace, key, *, backend="keyring"):
        return {"pull_secret": '{"auths": {}}', "username": "admin@vsphere.local", "password": "pw"}.get(key)


def _seed_bastion_and_cluster() -> tuple[int, int]:
    session = get_session()
    try:
        bastion = Bastion(host="bastion.lab.example.com", ssh_user="qe", install_dir="/home/qe/clusterbuild-installs")
        session.add(bastion)
        session.commit()
        cluster = Cluster(
            name="ipi-test1",
            base_domain="lab.example.com",
            ocp_version="4.18",
            install_config_platform="vsphere",
            infra_provisioning_target="vsphere",
            install_method="ipi",
            bastion_id=bastion.id,
            status="created",
        )
        session.add(cluster)
        session.commit()
        return bastion.id, cluster.id
    finally:
        session.close()


def test_ipi_handler_stages_manifests_and_runs_install(_fake_env):
    bastion_id, cluster_id = _seed_bastion_and_cluster()
    params = {
        "cluster_id": cluster_id,
        "cluster_name": "ipi-test1",
        "bastion_id": bastion_id,
        "platform": "vsphere",
        "install_method": "ipi",
        "ocp_version": "4.18",
        "environment_profile": "vsphere-pnq2",
        "answers": {
            "metadata.name": "ipi-test1",
            "baseDomain": "lab.example.com",
            "platform.vsphere.apiVIPs": ["10.74.232.10"],
            "platform.vsphere.ingressVIPs": ["10.74.232.11"],
        },
    }

    ipi.run(params, job_dir=None)

    executor = FakeBastionExecutor.instances[-1]
    assert executor.connected is False  # closed after use
    assert any(p.endswith("/ipi-test1") for p in executor.dirs_ensured)
    written = executor.written_files
    assert any(f.endswith("install-config.yaml") for f in written)
    assert "vcenter.vmware.gsslab.pnq2.redhat.com" in written[next(iter(written))]
    assert any("openshift-install create cluster" in c for c in executor.commands_run)

    session = get_session()
    try:
        cluster = session.get(Cluster, cluster_id)
        assert cluster.status == "installed"
    finally:
        session.close()


def test_ipi_handler_threads_stored_bastion_password_into_connect(_fake_env, monkeypatch):
    """Security-pass follow-up: a password stored via
    `clusterbuild bastion register --password`/`--ask-password` must reach
    `BastionExecutor.connect()` so it's available as a key/agent-auth
    fallback -- including for a background install job like this one, which
    reconnects long after any interactive prompt could happen."""
    bastion_id, cluster_id = _seed_bastion_and_cluster()
    params = {
        "cluster_id": cluster_id,
        "cluster_name": "ipi-test1",
        "bastion_id": bastion_id,
        "platform": "vsphere",
        "install_method": "ipi",
        "ocp_version": "4.18",
        "environment_profile": "vsphere-pnq2",
        "answers": {
            "metadata.name": "ipi-test1",
            "baseDomain": "lab.example.com",
            "platform.vsphere.apiVIPs": ["10.74.232.10"],
            "platform.vsphere.ingressVIPs": ["10.74.232.11"],
        },
    }

    class _FakeSecretsBackendWithBastionPassword(_FakeSecretsBackend):
        def get(self, namespace, key, *, backend="keyring"):
            if namespace == "bastion:bastion.lab.example.com" and key == "ssh_password":
                return "s3cret"
            return super().get(namespace, key, backend=backend)

    monkeypatch.setattr(ipi, "SecretsBackend", _FakeSecretsBackendWithBastionPassword)

    ipi.run(params, job_dir=None)

    executor = FakeBastionExecutor.instances[-1]
    assert executor.connect_password == "s3cret"


def test_ipi_handler_marks_cluster_failed_on_nonzero_exit(_fake_env, monkeypatch):
    bastion_id, cluster_id = _seed_bastion_and_cluster()
    params = {
        "cluster_id": cluster_id,
        "cluster_name": "ipi-test1",
        "bastion_id": bastion_id,
        "platform": "vsphere",
        "install_method": "ipi",
        "ocp_version": "4.18",
        "environment_profile": "vsphere-pnq2",
        "answers": {
            "metadata.name": "ipi-test1",
            "baseDomain": "lab.example.com",
            "platform.vsphere.apiVIPs": ["10.74.232.10"],
            "platform.vsphere.ingressVIPs": ["10.74.232.11"],
        },
    }

    original_init = FakeBastionExecutor.__init__

    def _init_with_failure(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        self.exit_code = 1

    monkeypatch.setattr(FakeBastionExecutor, "__init__", _init_with_failure)

    with pytest.raises(RuntimeError, match="exited with code 1"):
        ipi.run(params, job_dir=None)

    session = get_session()
    try:
        cluster = session.get(Cluster, cluster_id)
        assert cluster.status == "failed"
    finally:
        session.close()
