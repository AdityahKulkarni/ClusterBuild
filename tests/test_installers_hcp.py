"""Phase 8: exercise the Hosted Control Planes job handler with a fake bastion
executor -- no real hcp CLI/management-cluster kubeconfig needed."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

import pytest

from clusterbuild.core import installers  # noqa: F401  (registers handlers)
from clusterbuild.core.catalog_loader import Catalog
from clusterbuild.core.installers import hcp
from clusterbuild.core.state import Bastion, Cluster, get_session


class FakeChannel:
    def __init__(self, exit_code: int = 0):
        self._exit_code = exit_code

    def recv_exit_status(self) -> int:
        return self._exit_code


class FakeStdout:
    def __init__(self, lines, exit_code=0):
        self._lines = lines
        self.channel = FakeChannel(exit_code)

    def __iter__(self):
        return iter(self._lines)


class FakeResult:
    def __init__(self, ok=True, stdout="", stderr=""):
        self.ok = ok
        self.stdout = stdout
        self.stderr = stderr


class FakeBastionExecutor:
    instances: list["FakeBastionExecutor"] = []

    def __init__(self, host, user, port=22, key_filename=None):
        self.host, self.user = host, user
        self.written_files: dict[str, str] = {}
        self.dirs_ensured: list[str] = []
        self.commands_run: list[str] = []
        self.uploaded: dict[str, str] = {}
        self.exit_code = 0
        self.kubeconfig_stdout = "apiVersion: v1\nkind: Config\n"
        FakeBastionExecutor.instances.append(self)

    def connect(self, password=None):
        pass

    def close(self):
        pass

    def ensure_dir(self, path):
        self.dirs_ensured.append(path)

    def write_file(self, remote_path, content):
        self.written_files[remote_path] = content

    def upload_from_local(self, local_path: Path, remote_path: str):
        self.uploaded[remote_path] = str(local_path)

    def run(self, command, timeout=None):
        self.commands_run.append(command)
        if "hcp create kubeconfig" in command:
            return FakeResult(ok=True, stdout=self.kubeconfig_stdout)
        return FakeResult(ok=True, stdout="")

    def backup_manifest(self, cluster_name, remote_install_dir, filename):
        return Path(f"/tmp/fake-hcp/{cluster_name}/{filename}"), "deadbeef" * 8

    @contextmanager
    def run_streaming(self, command):
        self.commands_run.append(command)
        yield FakeStdout(["control plane is ready"], self.exit_code)


class _FakeSecretsBackend:
    def get(self, namespace, key, *, backend="keyring"):
        return {("vsphere", "pull_secret"): '{"auths": {}}'}.get((namespace, key))


@pytest.fixture()
def _fake_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CLUSTERBUILD_HOME", str(tmp_path))
    monkeypatch.setattr(hcp, "BastionExecutor", FakeBastionExecutor)
    monkeypatch.setattr(hcp, "SecretsBackend", _FakeSecretsBackend)
    monkeypatch.setattr("clusterbuild.core.installers.base.SecretsBackend", _FakeSecretsBackend)
    FakeBastionExecutor.instances.clear()


def test_hcp_catalog_entry_loads():
    entry = Catalog().load_entry("4.18", "kubevirt", "hcp")
    assert entry.platform == "kubevirt"
    assert entry.install_method == "hcp"
    assert entry.manifests


def _seed_bastion_and_cluster(mgmt_kubeconfig_path: Path) -> tuple[int, int]:
    session = get_session()
    try:
        bastion = Bastion(host="bastion.lab.example.com", ssh_user="qe", install_dir="/home/qe/clusterbuild-installs")
        session.add(bastion)
        session.commit()
        cluster = Cluster(
            name="hcp-test1",
            base_domain="",
            ocp_version="4.18",
            install_config_platform="kubevirt",
            infra_provisioning_target="kubevirt",
            install_method="hcp",
            bastion_id=bastion.id,
            status="created",
        )
        session.add(cluster)
        session.commit()
        return bastion.id, cluster.id
    finally:
        session.close()


def test_hcp_handler_uploads_kubeconfig_stages_pull_secret_and_installs(_fake_env, tmp_path):
    mgmt_kubeconfig = tmp_path / "mgmt-kubeconfig"
    mgmt_kubeconfig.write_text("apiVersion: v1\nkind: Config\n")
    bastion_id, cluster_id = _seed_bastion_and_cluster(mgmt_kubeconfig)

    params = {
        "cluster_id": cluster_id,
        "cluster_name": "hcp-test1",
        "bastion_id": bastion_id,
        "platform": "kubevirt",
        "install_method": "hcp",
        "ocp_version": "4.18",
        "environment_profile": None,
        "management_cluster_name": "mgmt-cluster",
        "management_cluster_kubeconfig_local_path": str(mgmt_kubeconfig),
        "answers": {
            "metadata.name": "hcp-test1",
            "namespace": "clusters",
            "nodePoolReplicas": 2,
            "memory": "8Gi",
            "cores": 2,
            "etcdStorageClass": "lvm-immediate",
            "waitTimeout": "45m",
        },
    }

    hcp.run(params, job_dir=None)

    executor = FakeBastionExecutor.instances[-1]
    assert executor.uploaded[f"{'/home/qe/clusterbuild-installs/hcp-test1'}/management-kubeconfig"] == str(mgmt_kubeconfig)
    assert executor.written_files[f"{'/home/qe/clusterbuild-installs/hcp-test1'}/pull-secret.json"] == '{"auths": {}}'
    assert any("hcp create cluster kubevirt" in c and "--wait --timeout 45m" in c for c in executor.commands_run)
    assert any("--etcd-storage-class=lvm-immediate" in c for c in executor.commands_run)
    assert any("hcp create kubeconfig --name hcp-test1" in c for c in executor.commands_run)
    assert executor.written_files[f"{'/home/qe/clusterbuild-installs/hcp-test1'}/auth/kubeconfig"] == executor.kubeconfig_stdout

    session = get_session()
    try:
        cluster = session.get(Cluster, cluster_id)
        assert cluster.status == "installed"
    finally:
        session.close()


def test_hcp_handler_marks_failed_on_nonzero_exit(_fake_env, tmp_path, monkeypatch):
    mgmt_kubeconfig = tmp_path / "mgmt-kubeconfig"
    mgmt_kubeconfig.write_text("apiVersion: v1\nkind: Config\n")
    bastion_id, cluster_id = _seed_bastion_and_cluster(mgmt_kubeconfig)

    original_init = FakeBastionExecutor.__init__

    def _init_with_failure(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        self.exit_code = 1

    monkeypatch.setattr(FakeBastionExecutor, "__init__", _init_with_failure)

    params = {
        "cluster_id": cluster_id,
        "cluster_name": "hcp-test1",
        "bastion_id": bastion_id,
        "platform": "kubevirt",
        "install_method": "hcp",
        "ocp_version": "4.18",
        "environment_profile": None,
        "management_cluster_name": "mgmt-cluster",
        "management_cluster_kubeconfig_local_path": str(mgmt_kubeconfig),
        "answers": {
            "metadata.name": "hcp-test1",
            "namespace": "clusters",
            "nodePoolReplicas": 2,
            "memory": "8Gi",
            "cores": 2,
            "etcdStorageClass": "lvm-immediate",
            "waitTimeout": "45m",
        },
    }

    with pytest.raises(hcp.HcpInstallError, match="exited with code 1"):
        hcp.run(params, job_dir=None)

    session = get_session()
    try:
        cluster = session.get(Cluster, cluster_id)
        assert cluster.status == "failed"
    finally:
        session.close()


def test_hcp_handler_raises_when_pull_secret_missing(_fake_env, tmp_path, monkeypatch):
    class _EmptySecrets:
        def get(self, namespace, key, *, backend="keyring"):
            return None

    monkeypatch.setattr(hcp, "SecretsBackend", _EmptySecrets)

    mgmt_kubeconfig = tmp_path / "mgmt-kubeconfig"
    mgmt_kubeconfig.write_text("apiVersion: v1\nkind: Config\n")
    bastion_id, cluster_id = _seed_bastion_and_cluster(mgmt_kubeconfig)

    params = {
        "cluster_id": cluster_id,
        "cluster_name": "hcp-test1",
        "bastion_id": bastion_id,
        "platform": "kubevirt",
        "install_method": "hcp",
        "ocp_version": "4.18",
        "environment_profile": None,
        "management_cluster_name": "mgmt-cluster",
        "management_cluster_kubeconfig_local_path": str(mgmt_kubeconfig),
        "answers": {
            "metadata.name": "hcp-test1",
            "namespace": "clusters",
            "nodePoolReplicas": 2,
            "memory": "8Gi",
            "cores": 2,
            "etcdStorageClass": "lvm-immediate",
            "waitTimeout": "45m",
        },
    }

    with pytest.raises(hcp.HcpInstallError, match="No pull secret found"):
        hcp.run(params, job_dir=None)
