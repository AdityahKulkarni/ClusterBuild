"""Phase 3 + platform-none variant: exercise the Agent-based installer job
handler with fake bastion/vSphere driver calls (no real SSH/govc/vCenter)."""

from __future__ import annotations

from contextlib import contextmanager

import pytest

from clusterbuild.core import installers  # noqa: F401
from clusterbuild.core.installers import agent
from clusterbuild.core.state import Bastion, Cluster, get_session


class FakeChannel:
    def __init__(self, exit_code=0):
        self._exit_code = exit_code

    def recv_exit_status(self):
        return self._exit_code


class FakeStdout:
    def __init__(self, lines=None, exit_code=0):
        self.channel = FakeChannel(exit_code)
        self._lines = lines or ["ok"]

    def __iter__(self):
        return iter(self._lines)


class FakeBastionExecutor:
    instances: list["FakeBastionExecutor"] = []

    def __init__(self, host, user, port=22, key_filename=None):
        self.host, self.user, self.port = host, user, port
        self.written_files = {}
        self.commands_run = []
        FakeBastionExecutor.instances.append(self)

    def connect(self, password=None):
        pass

    def close(self):
        pass

    def ensure_dir(self, path):
        pass

    def write_file(self, remote_path, content):
        self.written_files[remote_path] = content

    def backup_manifest(self, cluster_name, remote_install_dir, filename):
        from pathlib import Path

        return Path(f"/tmp/fake/{cluster_name}/{filename}"), "cafebabe" * 4

    @contextmanager
    def run_streaming(self, command):
        self.commands_run.append(command)
        yield FakeStdout()


class _FakeSecretsBackend:
    def get(self, namespace, key, *, backend="keyring"):
        return {"pull_secret": '{"auths": {}}', "username": "admin@vsphere.local", "password": "pw"}.get(key)


@pytest.fixture()
def _fake_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CLUSTERBUILD_HOME", str(tmp_path))
    monkeypatch.setattr(agent, "BastionExecutor", FakeBastionExecutor)
    monkeypatch.setattr("clusterbuild.core.installers.base.SecretsBackend", _FakeSecretsBackend)
    monkeypatch.setattr(agent, "SecretsBackend", _FakeSecretsBackend)

    vsphere_calls = {"uploaded": [], "created": [], "powered_on": []}

    def fake_upload(executor, profile, creds, *, local_iso_path, remote_iso_name):
        vsphere_calls["uploaded"].append(remote_iso_name)

    def fake_create(executor, profile, creds, *, vm_name, iso_remote_name, **kwargs):
        vsphere_calls["created"].append(vm_name)

    def fake_power_on(executor, profile, creds, *, vm_name):
        vsphere_calls["powered_on"].append(vm_name)

    monkeypatch.setattr(agent.vsphere_driver, "upload_iso_to_datastore", fake_upload)
    monkeypatch.setattr(agent.vsphere_driver, "create_vm_from_iso", fake_create)
    monkeypatch.setattr(agent.vsphere_driver, "power_on", fake_power_on)

    FakeBastionExecutor.instances.clear()
    return vsphere_calls


def _seed(platform: str) -> tuple[int, int]:
    session = get_session()
    try:
        bastion = Bastion(host="bastion.lab", ssh_user="qe", install_dir="/home/qe/clusterbuild-installs")
        session.add(bastion)
        session.commit()
        cluster = Cluster(
            name="agent-test1",
            base_domain="lab.example.com",
            ocp_version="4.18",
            install_config_platform=platform,
            infra_provisioning_target="vsphere",
            install_method="agent",
            bastion_id=bastion.id,
            status="created",
        )
        session.add(cluster)
        session.commit()
        return bastion.id, cluster.id
    finally:
        session.close()


def _base_params(platform: str, bastion_id: int, cluster_id: int) -> dict:
    return {
        "cluster_id": cluster_id,
        "cluster_name": "agent-test1",
        "bastion_id": bastion_id,
        "platform": platform,
        "install_method": "agent",
        "ocp_version": "4.18",
        "environment_profile": "vsphere-pnq2",
        "answers": {
            "metadata.name": "agent-test1",
            "baseDomain": "lab.example.com",
            "platform.vsphere.apiVIPs": ["10.74.232.10"],
            "platform.vsphere.ingressVIPs": ["10.74.232.11"],
            "rendezvousIP": "10.74.232.20",
            "hosts": [
                {
                    "hostname": "master-0",
                    "role": "master",
                    "interface_name": "eth0",
                    "mac_address": "00:ef:44:21:e6:a5",
                    "ip_address": "10.74.232.20",
                    "prefix_length": "21",
                    "gateway": "10.74.232.1",
                    "dns_server": "10.74.232.2",
                }
            ],
        },
    }


def test_agent_install_vsphere_platform_creates_and_boots_vm(_fake_env):
    bastion_id, cluster_id = _seed("vsphere")
    params = _base_params("vsphere", bastion_id, cluster_id)

    agent.run(params, job_dir=None)

    assert _fake_env["created"] == ["agent-test1-master-0"]
    assert _fake_env["powered_on"] == ["agent-test1-master-0"]
    executor = FakeBastionExecutor.instances[-1]
    written = executor.written_files
    assert any(f.endswith("agent-config.yaml") for f in written)
    assert any("agent create image" in c for c in executor.commands_run)
    assert any("wait-for install-complete" in c for c in executor.commands_run)

    session = get_session()
    try:
        cluster = session.get(Cluster, cluster_id)
        assert cluster.status == "installed"
    finally:
        session.close()


def test_agent_install_none_platform_omits_vsphere_manifest_block(_fake_env):
    """The `platform: none` variant must still provision on vSphere, but the
    written install-config.yaml must not contain any vSphere fields."""
    bastion_id, cluster_id = _seed("none")
    params = _base_params("none", bastion_id, cluster_id)
    params["answers"].pop("platform.vsphere.apiVIPs")
    params["answers"].pop("platform.vsphere.ingressVIPs")

    agent.run(params, job_dir=None)

    executor = FakeBastionExecutor.instances[-1]
    install_config = next(v for k, v in executor.written_files.items() if k.endswith("install-config.yaml"))
    assert "vsphere" not in install_config
    assert "none: {}" in install_config
    assert _fake_env["created"] == ["agent-test1-master-0"], "still provisioned via the vSphere driver"


def test_agent_install_requires_at_least_one_host(_fake_env):
    bastion_id, cluster_id = _seed("vsphere")
    params = _base_params("vsphere", bastion_id, cluster_id)
    params["answers"]["hosts"] = []
    with pytest.raises(RuntimeError, match="No hosts defined"):
        agent.run(params, job_dir=None)
