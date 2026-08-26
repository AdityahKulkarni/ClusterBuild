"""Phase 5: exercise the Assisted Installer job handler with a fake
assisted-service client + fake bastion/vSphere driver calls (no real
SSH/govc/vCenter/assisted-service needed)."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from clusterbuild.core import installers  # noqa: F401
from clusterbuild.core.installers import assisted
from clusterbuild.core.state import Bastion, Cluster, get_session


@dataclass
class FakeResult:
    exit_code: int = 0
    stdout: str = ""

    @property
    def ok(self):
        return self.exit_code == 0


class FakeBastionExecutor:
    instances: list["FakeBastionExecutor"] = []

    def __init__(self, host, user, port=22, key_filename=None):
        self.host, self.user = host, user
        self.commands_run = []
        FakeBastionExecutor.instances.append(self)

    def connect(self, password=None):
        pass

    def close(self):
        pass

    def ensure_dir(self, path):
        pass

    def run(self, command, timeout=None):
        self.commands_run.append(command)
        if "curl -sf" in command:
            return FakeResult(exit_code=0)  # self-hosted health check passes
        return FakeResult(exit_code=0)


@dataclass
class FakeAssistedClient:
    created_clusters: list[dict] = field(default_factory=list)
    created_infra_envs: list[dict] = field(default_factory=list)
    installed: list[str] = field(default_factory=list)
    status_sequence: list[str] = field(default_factory=lambda: ["ready", "installed"])

    def create_cluster(self, payload):
        self.created_clusters.append(payload)
        return {"id": "remote-cluster-1"}

    def create_infra_env(self, payload):
        self.created_infra_envs.append(payload)
        return {"id": "infra-env-1"}

    def discovery_iso_url(self, infra_env_id):
        return "https://example.com/discovery.iso"

    def install_cluster(self, cluster_id):
        self.installed.append(cluster_id)
        return {"id": cluster_id, "status": "installing"}

    def wait_for_status(self, cluster_id, target_statuses, **kwargs):
        return {"id": cluster_id, "status": next(iter(target_statuses))}

    def kubeconfig(self, cluster_id):
        return b"apiVersion: v1\nkind: Config\n"


class _FakeSecretsBackend:
    def get(self, namespace, key, *, backend="keyring"):
        return {
            ("vsphere", "pull_secret"): '{"auths": {}}',
            ("vsphere", "username"): "admin@vsphere.local",
            ("vsphere", "password"): "pw",
            ("assisted_saas", "offline_token"): "fake-offline-token",
        }.get((namespace, key))


@pytest.fixture()
def _fake_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CLUSTERBUILD_HOME", str(tmp_path))
    monkeypatch.setattr(assisted, "BastionExecutor", FakeBastionExecutor)
    monkeypatch.setattr("clusterbuild.core.installers.base.SecretsBackend", _FakeSecretsBackend)
    monkeypatch.setattr(assisted, "SecretsBackend", _FakeSecretsBackend)

    calls = {"created_vms": [], "powered_on": [], "uploaded_isos": []}
    monkeypatch.setattr(
        assisted.vsphere_driver, "upload_iso_to_datastore", lambda *a, **k: calls["uploaded_isos"].append(k["remote_iso_name"])
    )
    monkeypatch.setattr(assisted.vsphere_driver, "create_vm_from_iso", lambda *a, **k: calls["created_vms"].append(k["vm_name"]))
    monkeypatch.setattr(assisted.vsphere_driver, "power_on", lambda *a, **k: calls["powered_on"].append(k["vm_name"]))

    fake_client = FakeAssistedClient()
    monkeypatch.setattr(assisted, "AssistedServiceClient", lambda *a, **k: fake_client)
    monkeypatch.setattr(assisted, "exchange_offline_token", lambda token, **k: "fake-access-token")

    FakeBastionExecutor.instances.clear()
    return calls, fake_client


def _seed() -> tuple[int, int]:
    session = get_session()
    try:
        bastion = Bastion(host="bastion.lab", ssh_user="qe", install_dir="/home/qe/clusterbuild-installs")
        session.add(bastion)
        session.commit()
        cluster = Cluster(
            name="assisted-test1",
            base_domain="lab.example.com",
            ocp_version="4.18",
            install_config_platform="vsphere",
            infra_provisioning_target="vsphere",
            install_method="assisted",
            bastion_id=bastion.id,
            status="created",
        )
        session.add(cluster)
        session.commit()
        return bastion.id, cluster.id
    finally:
        session.close()


def _base_params(backend: str, bastion_id: int, cluster_id: int) -> dict:
    return {
        "cluster_id": cluster_id,
        "cluster_name": "assisted-test1",
        "bastion_id": bastion_id,
        "platform": "vsphere",
        "install_method": "assisted",
        "ocp_version": "4.18",
        "environment_profile": "vsphere-pnq2",
        "backend": backend,
        "worker_vm_count": 2,
        "answers": {
            "name": "assisted-test1",
            "base_dns_domain": "lab.example.com",
            "controlPlane.replicas": 3,
            "api_vips": ["10.74.232.10"],
            "ingress_vips": ["10.74.232.11"],
        },
    }


def test_assisted_self_hosted_end_to_end(_fake_env):
    calls, client = _fake_env
    bastion_id, cluster_id = _seed()
    params = _base_params("self_hosted", bastion_id, cluster_id)

    assisted.run(params, job_dir=None)

    assert len(client.created_clusters) == 1
    assert client.created_infra_envs[0]["cluster_id"] == "remote-cluster-1"
    assert client.installed == ["remote-cluster-1"]
    # 3 control-plane + 2 worker VMs
    assert len(calls["created_vms"]) == 5
    assert len(calls["powered_on"]) == 5

    session = get_session()
    try:
        cluster = session.get(Cluster, cluster_id)
        assert cluster.status == "installed"
    finally:
        session.close()


def test_assisted_saas_exchanges_offline_token(_fake_env):
    calls, client = _fake_env
    bastion_id, cluster_id = _seed()
    params = _base_params("saas", bastion_id, cluster_id)

    assisted.run(params, job_dir=None)

    assert len(client.created_clusters) == 1
    session = get_session()
    try:
        cluster = session.get(Cluster, cluster_id)
        assert cluster.status == "installed"
    finally:
        session.close()


def test_assisted_unknown_backend_rejected(_fake_env):
    bastion_id, cluster_id = _seed()
    params = _base_params("totally_not_a_backend", bastion_id, cluster_id)
    with pytest.raises(RuntimeError, match="is not one of"):
        assisted.run(params, job_dir=None)
