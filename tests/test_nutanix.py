"""Phase 6: catalog + manifest-building + installer-dispatch coverage for
Nutanix, proving the driver-registry design lets a second platform reuse
the exact same install-method orchestration code as vSphere."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

import pytest

from clusterbuild.core import installers  # noqa: F401
from clusterbuild.core.catalog_loader import Catalog
from clusterbuild.core.drivers.registry import driver_for
from clusterbuild.core.installers import agent
from clusterbuild.core.manifest_builder import build_manifests
from clusterbuild.core.secrets import SecretsBackend
from clusterbuild.core.state import Bastion, Cluster, get_session

ENV_PROFILE = Path(__file__).parent.parent / "clusterbuild" / "environments" / "nutanix-lab.yaml"


class _FakeSecrets(SecretsBackend):
    def get(self, namespace, key, *, backend="keyring"):
        return {
            ("vsphere", "pull_secret"): '{"auths": {}}',
            ("nutanix", "prism_central_username"): "admin",
            ("nutanix", "prism_central_password"): "pw",
        }.get((namespace, key))


@pytest.mark.parametrize("method", ["ipi", "upi", "agent", "assisted"])
def test_all_nutanix_catalog_entries_load(method):
    entry = Catalog().load_entry("4.18", "nutanix", method)
    assert entry.platform == "nutanix"
    assert entry.infra_provisioning_target == "nutanix"
    assert entry.manifests


def test_nutanix_ipi_manifest_uses_prism_central_block():
    entry = Catalog().load_entry("4.18", "nutanix", "ipi")
    answers = {
        "metadata.name": "nx-test1",
        "baseDomain": "lab.example.com",
        "platform.nutanix.apiVIPs": ["10.0.0.10"],
        "platform.nutanix.ingressVIPs": ["10.0.0.11"],
    }
    results = build_manifests(entry, environment_profile_path=ENV_PROFILE, answers=answers, secrets=_FakeSecrets())
    doc = results[0].content_dict
    assert doc["platform"]["nutanix"]["prismCentral"]["username"] == "admin"
    assert doc["platform"]["nutanix"]["apiVIPs"] == ["10.0.0.10"]
    assert "subnetUUIDs" in doc["platform"]["nutanix"]


def test_driver_registry_resolves_nutanix_and_vsphere_to_distinct_modules():
    from clusterbuild.core.drivers import nutanix as nutanix_driver
    from clusterbuild.core.drivers import vsphere as vsphere_driver

    assert driver_for("nutanix") is nutanix_driver
    assert driver_for("vsphere") is vsphere_driver
    assert nutanix_driver.SECRET_NAMESPACE == "nutanix"
    assert vsphere_driver.SECRET_NAMESPACE == "vsphere"


def test_unknown_infra_target_raises():
    from clusterbuild.core.drivers.registry import UnknownDriverError

    with pytest.raises(UnknownDriverError):
        driver_for("openstack")


# -- agent.py reused, unmodified, against the Nutanix driver -----------------


class FakeChannel:
    def recv_exit_status(self):
        return 0


class FakeStdout:
    def __init__(self):
        self.channel = FakeChannel()

    def __iter__(self):
        return iter(["ok"])


class FakeBastionExecutor:
    instances: list["FakeBastionExecutor"] = []

    def __init__(self, host, user, port=22, key_filename=None):
        self.host, self.user = host, user
        self.written_files = {}
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
        return Path(f"/tmp/fake/{cluster_name}/{filename}"), "deadbeef" * 4

    @contextmanager
    def run_streaming(self, command):
        yield FakeStdout()


def _seed_nutanix_cluster() -> tuple[int, int]:
    session = get_session()
    try:
        bastion = Bastion(host="bastion.lab", ssh_user="qe", install_dir="/home/qe/clusterbuild-installs")
        session.add(bastion)
        session.commit()
        cluster = Cluster(
            name="nx-agent1",
            base_domain="lab.example.com",
            ocp_version="4.18",
            install_config_platform="nutanix",
            infra_provisioning_target="nutanix",
            install_method="agent",
            bastion_id=bastion.id,
            status="created",
        )
        session.add(cluster)
        session.commit()
        return bastion.id, cluster.id
    finally:
        session.close()


def test_agent_installer_provisions_via_nutanix_driver_unmodified(tmp_path, monkeypatch):
    monkeypatch.setenv("CLUSTERBUILD_HOME", str(tmp_path))
    monkeypatch.setattr(agent, "BastionExecutor", FakeBastionExecutor)
    monkeypatch.setattr("clusterbuild.core.installers.base.SecretsBackend", _FakeSecrets)
    monkeypatch.setattr(agent, "SecretsBackend", _FakeSecrets)

    calls = {"created": [], "powered_on": []}
    monkeypatch.setattr(agent.nutanix_driver, "upload_iso_to_datastore", lambda *a, **k: None)
    monkeypatch.setattr(agent.nutanix_driver, "create_vm_from_iso", lambda *a, **k: calls["created"].append(k["vm_name"]))
    monkeypatch.setattr(agent.nutanix_driver, "power_on", lambda *a, **k: calls["powered_on"].append(k["vm_name"]))

    FakeBastionExecutor.instances.clear()
    bastion_id, cluster_id = _seed_nutanix_cluster()
    params = {
        "cluster_id": cluster_id,
        "cluster_name": "nx-agent1",
        "bastion_id": bastion_id,
        "platform": "nutanix",
        "install_method": "agent",
        "ocp_version": "4.18",
        "environment_profile": "nutanix-lab",
        "answers": {
            "metadata.name": "nx-agent1",
            "baseDomain": "lab.example.com",
            "platform.nutanix.apiVIPs": ["10.0.0.10"],
            "platform.nutanix.ingressVIPs": ["10.0.0.11"],
            "rendezvousIP": "10.0.0.20",
            "hosts": [
                {
                    "hostname": "master-0",
                    "role": "master",
                    "interface_name": "eth0",
                    "mac_address": "00:ef:44:21:e6:a5",
                    "ip_address": "10.0.0.20",
                    "prefix_length": "24",
                    "gateway": "10.0.0.1",
                    "dns_server": "10.0.0.2",
                }
            ],
        },
    }

    agent.run(params, job_dir=None)

    assert calls["created"] == ["nx-agent1-master-0"]
    assert calls["powered_on"] == ["nx-agent1-master-0"]

    session = get_session()
    try:
        cluster = session.get(Cluster, cluster_id)
        assert cluster.status == "installed"
    finally:
        session.close()
