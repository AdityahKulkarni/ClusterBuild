from pathlib import Path

import pytest

from clusterbuild.core.catalog_loader import Catalog
from clusterbuild.core.manifest_builder import ManifestValidationError, build_manifests
from clusterbuild.core.secrets import SecretsBackend

ENV_PROFILE = Path(__file__).parent.parent / "clusterbuild" / "environments" / "vsphere-pnq2.yaml"


class FakeSecrets(SecretsBackend):
    def __init__(self, values):
        super().__init__()
        self._values = values

    def get(self, namespace, key, *, backend="keyring"):
        return self._values.get((namespace, key))


def _fake_secrets():
    return FakeSecrets(
        {
            ("vsphere", "pull_secret"): '{"auths": {}}',
            ("vsphere", "username"): "administrator@vsphere.local",
            ("vsphere", "password"): "s3cr3t",
        }
    )


def test_vsphere_ipi_manifest_matches_expected_shape():
    entry = Catalog().load_entry("4.18", "vsphere", "ipi")
    answers = {
        "metadata.name": "qe-test1",
        "baseDomain": "lab.example.com",
        "controlPlane.replicas": 3,
        "compute.replicas": 2,
        "platform.vsphere.apiVIPs": ["10.74.232.10"],
        "platform.vsphere.ingressVIPs": ["10.74.232.11"],
    }
    results = build_manifests(
        entry,
        environment_profile_path=ENV_PROFILE,
        answers=answers,
        secrets=_fake_secrets(),
    )
    assert len(results) == 1
    doc = results[0].content_dict

    assert doc["metadata"]["name"] == "qe-test1"
    assert doc["platform"]["vsphere"]["vcenters"][0]["server"] == "vcenter.vmware.gsslab.pnq2.redhat.com"
    assert doc["platform"]["vsphere"]["vcenters"][0]["user"] == "administrator@vsphere.local"
    assert doc["platform"]["vsphere"]["failureDomains"][0]["topology"]["computeCluster"] == "/OpenShift-DC/host/OCP"
    assert doc["platform"]["vsphere"]["apiVIPs"] == ["10.74.232.10"]
    assert doc["networking"]["machineNetwork"] == [{"cidr": "10.74.232.0/21", "hostPrefix": 23}]


def test_vip_outside_machine_network_is_rejected():
    entry = Catalog().load_entry("4.18", "vsphere", "ipi")
    answers = {
        "metadata.name": "qe-test2",
        "baseDomain": "lab.example.com",
        "platform.vsphere.apiVIPs": ["192.168.1.5"],  # not in 10.74.232.0/21
        "platform.vsphere.ingressVIPs": ["10.74.232.11"],
    }
    with pytest.raises(ManifestValidationError):
        build_manifests(entry, environment_profile_path=ENV_PROFILE, answers=answers, secrets=_fake_secrets())


def test_none_platform_upi_omits_vsphere_block():
    entry = Catalog().load_entry("4.18", "none", "upi")
    answers = {"metadata.name": "qe-none1", "baseDomain": "lab.example.com", "controlPlane.replicas": 3}
    results = build_manifests(entry, environment_profile_path=ENV_PROFILE, answers=answers, secrets=_fake_secrets())
    doc = results[0].content_dict
    assert doc["platform"] == {"none": {}}
    assert doc["compute"]["replicas"] == 0


def test_agent_config_renders_hosts_to_nmstate():
    entry = Catalog().load_entry("4.18", "vsphere", "agent")
    answers = {
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
    }
    results = build_manifests(entry, environment_profile_path=ENV_PROFILE, answers=answers, secrets=_fake_secrets())
    agent_config = next(r for r in results if r.filename == "agent-config.yaml").content_dict
    host = agent_config["hosts"][0]
    assert host["hostname"] == "master-0"
    assert host["interfaces"][0]["macAddress"] == "00:ef:44:21:e6:a5"
    net_iface = host["networkConfig"]["interfaces"][0]
    assert net_iface["ipv4"]["address"][0]["ip"] == "10.74.232.20"
    assert net_iface["ipv4"]["address"][0]["prefix-length"] == 21
    assert host["networkConfig"]["routes"]["config"][0]["next-hop-address"] == "10.74.232.1"


def test_agent_config_rejects_host_ip_outside_machine_network():
    entry = Catalog().load_entry("4.18", "vsphere", "agent")
    answers = {
        "metadata.name": "agent-test2",
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
                "ip_address": "192.168.99.20",
                "prefix_length": "21",
                "gateway": "10.74.232.1",
                "dns_server": "10.74.232.2",
            }
        ],
    }
    with pytest.raises(ManifestValidationError):
        build_manifests(entry, environment_profile_path=ENV_PROFILE, answers=answers, secrets=_fake_secrets())


def test_missing_pull_secret_raises():
    entry = Catalog().load_entry("4.18", "vsphere", "ipi")
    answers = {
        "metadata.name": "qe-test3",
        "baseDomain": "lab.example.com",
        "platform.vsphere.apiVIPs": ["10.74.232.10"],
        "platform.vsphere.ingressVIPs": ["10.74.232.11"],
    }
    with pytest.raises(ManifestValidationError):
        build_manifests(entry, environment_profile_path=ENV_PROFILE, answers=answers, secrets=FakeSecrets({}))
