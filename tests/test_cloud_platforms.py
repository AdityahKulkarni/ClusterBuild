"""Phase 7: AWS/Azure/GCP IPI catalog + manifest-building + credential-staging
coverage.

Deliberately does NOT add Agent-based catalog entries for these clouds: per
official docs (docs.redhat.com Agent-based Installer "Supported platforms"),
the Agent-based Installer only supports baremetal/vsphere/nutanix/external/
none -- not aws/azure/gcp. IPI is the documented cloud installation path.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

import pytest

from clusterbuild.core import cloud_credentials
from clusterbuild.core import installers  # noqa: F401
from clusterbuild.core.catalog_loader import Catalog, CatalogError
from clusterbuild.core.installers import ipi
from clusterbuild.core.manifest_builder import build_manifests
from clusterbuild.core.secrets import SecretsBackend
from clusterbuild.core.state import Bastion, Cluster, get_session

ENV_DIR = Path(__file__).parent.parent / "clusterbuild" / "environments"


class _FakeSecrets(SecretsBackend):
    def __init__(self, values):
        self._values = values

    def get(self, namespace, key, *, backend="keyring"):
        return self._values.get((namespace, key))


@pytest.mark.parametrize(
    "platform,env_profile,answers,expected_platform_block",
    [
        (
            "aws",
            "aws-lab.yaml",
            {"metadata.name": "aws-t1", "baseDomain": "lab.example.com"},
            {"region": "REPLACE-WITH-YOUR-AWS-REGION"},
        ),
        (
            "azure",
            "azure-lab.yaml",
            {"metadata.name": "azure-t1", "baseDomain": "lab.example.com"},
            {"region": "REPLACE-WITH-YOUR-AZURE-REGION", "baseDomainResourceGroupName": "REPLACE-WITH-YOUR-DNS-ZONE-RESOURCE-GROUP"},
        ),
        (
            "gcp",
            "gcp-lab.yaml",
            {"metadata.name": "gcp-t1", "baseDomain": "lab.example.com"},
            {"projectID": "REPLACE-WITH-YOUR-GCP-PROJECT-ID", "region": "REPLACE-WITH-YOUR-GCP-REGION"},
        ),
    ],
)
def test_cloud_ipi_catalog_loads_and_builds_manifest(platform, env_profile, answers, expected_platform_block):
    entry = Catalog().load_entry("4.18", platform, "ipi")
    assert entry.platform == platform
    assert entry.infra_provisioning_target == platform

    secrets = _FakeSecrets({("vsphere", "pull_secret"): '{"auths": {}}'})
    results = build_manifests(
        entry,
        environment_profile_path=ENV_DIR / env_profile,
        answers=answers,
        secrets=secrets,
        keyring_namespace=platform,
    )
    doc = results[0].content_dict
    assert doc["platform"][platform] == expected_platform_block
    assert doc["pullSecret"] == '{"auths": {}}'  # fell back to the "vsphere" namespace


@pytest.mark.parametrize("platform", ["aws", "azure", "gcp"])
def test_no_agent_based_catalog_entry_for_clouds(platform):
    """Confirms we deliberately did not fabricate Agent-based support for
    platforms the official docs don't list it for."""
    with pytest.raises(CatalogError):
        Catalog().load_entry("4.18", platform, "agent")


class FakeChannel:
    def __init__(self, exit_code=0):
        self._exit_code = exit_code

    def recv_exit_status(self):
        return self._exit_code


class FakeStdout:
    def __init__(self, exit_code=0):
        self.channel = FakeChannel(exit_code)

    def __iter__(self):
        return iter(["level=info msg=Cluster is ready"])


class FakeBastionExecutor:
    instances: list["FakeBastionExecutor"] = []

    def __init__(self, host, user, port=22, key_filename=None):
        self.host, self.user = host, user
        self.written_files: dict[str, str] = {}
        self.dirs_ensured: list[str] = []
        self.commands_run: list[str] = []
        FakeBastionExecutor.instances.append(self)

    def connect(self, password=None):
        pass

    def close(self):
        pass

    def ensure_dir(self, path):
        self.dirs_ensured.append(path)

    def write_file(self, remote_path, content):
        self.written_files[remote_path] = content

    def run(self, command, timeout=None):
        self.commands_run.append(command)
        if command == "echo $HOME":
            return type("R", (), {"ok": True, "stdout": "/home/qe\n"})()
        return type("R", (), {"ok": True, "stdout": ""})()

    def backup_manifest(self, cluster_name, remote_install_dir, filename):
        return Path(f"/tmp/fake/{cluster_name}/{filename}"), "deadbeef" * 4

    @contextmanager
    def run_streaming(self, command):
        self.commands_run.append(command)
        yield FakeStdout()


class _FakeSecretsBackend:
    def get(self, namespace, key, *, backend="keyring"):
        return {
            ("vsphere", "pull_secret"): '{"auths": {}}',
            ("aws", "access_key_id"): "AKIAFAKE",
            ("aws", "secret_access_key"): "s3cr3t",
        }.get((namespace, key))


def _seed_aws_cluster() -> tuple[int, int]:
    session = get_session()
    try:
        bastion = Bastion(host="bastion.lab", ssh_user="qe", install_dir="/home/qe/clusterbuild-installs")
        session.add(bastion)
        session.commit()
        cluster = Cluster(
            name="aws-ipi1",
            base_domain="lab.example.com",
            ocp_version="4.18",
            install_config_platform="aws",
            infra_provisioning_target="aws",
            install_method="ipi",
            bastion_id=bastion.id,
            status="created",
        )
        session.add(cluster)
        session.commit()
        return bastion.id, cluster.id
    finally:
        session.close()


def test_ipi_stages_aws_credentials_before_create_cluster(tmp_path, monkeypatch):
    monkeypatch.setenv("CLUSTERBUILD_HOME", str(tmp_path))
    monkeypatch.setattr(ipi, "BastionExecutor", FakeBastionExecutor)
    monkeypatch.setattr("clusterbuild.core.installers.base.SecretsBackend", _FakeSecretsBackend)
    monkeypatch.setattr(ipi, "SecretsBackend", _FakeSecretsBackend)
    FakeBastionExecutor.instances.clear()

    bastion_id, cluster_id = _seed_aws_cluster()
    params = {
        "cluster_id": cluster_id,
        "cluster_name": "aws-ipi1",
        "bastion_id": bastion_id,
        "platform": "aws",
        "install_method": "ipi",
        "ocp_version": "4.18",
        "environment_profile": "aws-lab",
        "answers": {"metadata.name": "aws-ipi1", "baseDomain": "lab.example.com"},
    }

    ipi.run(params, job_dir=None)

    executor = FakeBastionExecutor.instances[-1]
    creds_file = executor.written_files.get("/home/qe/.aws/credentials")
    assert creds_file is not None
    assert "AKIAFAKE" in creds_file
    assert "s3cr3t" in creds_file
    assert any("openshift-install create cluster" in c for c in executor.commands_run)

    session = get_session()
    try:
        cluster = session.get(Cluster, cluster_id)
        assert cluster.status == "installed"
    finally:
        session.close()


def test_cloud_credentials_stage_is_noop_for_vsphere():
    calls = []

    class _Executor:
        def run(self, command, timeout=None):
            calls.append(command)
            return type("R", (), {"ok": True, "stdout": "/home/qe\n"})()

        def ensure_dir(self, path):
            calls.append(f"mkdir {path}")

        def write_file(self, path, content):
            calls.append(f"write {path}")

    cloud_credentials.stage(_Executor(), "vsphere", _FakeSecretsBackend())
    assert calls == []


def test_cloud_credentials_stage_raises_when_aws_creds_missing():
    class _EmptySecrets:
        def get(self, namespace, key, *, backend="keyring"):
            return None

    class _Executor:
        def run(self, command, timeout=None):
            return type("R", (), {"ok": True, "stdout": "/home/qe\n"})()

    with pytest.raises(cloud_credentials.CloudCredentialsError):
        cloud_credentials.stage(_Executor(), "aws", _EmptySecrets())
