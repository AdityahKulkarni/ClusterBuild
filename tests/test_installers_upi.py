"""Phase 4 + platform-none variant: exercise the UPI installer job handler
with fake bastion/vSphere driver calls (no real SSH/govc/vCenter/DNS)."""

from __future__ import annotations

import base64
import json
from contextlib import contextmanager
from dataclasses import dataclass

import pytest

from clusterbuild.core import installers  # noqa: F401
from clusterbuild.core.installers import upi
from clusterbuild.core.state import Bastion, Cluster, get_session


@dataclass
class FakeResult:
    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


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


COREOS_STREAM = {
    "architectures": {
        "x86_64": {"artifacts": {"vmware": {"formats": {"ova": {"disk": {"location": "https://example.com/rhcos.ova"}}}}}}
    }
}


class FakeBastionExecutor:
    instances: list["FakeBastionExecutor"] = []

    def __init__(self, host, user, port=22, key_filename=None):
        self.host, self.user, self.port = host, user, port
        self.written_files = {}
        self.commands_run = []
        self.remote_files = {
            "master.ign": "MASTER_IGNITION_CONTENT",
            "worker.ign": "WORKER_IGNITION_CONTENT",
        }
        FakeBastionExecutor.instances.append(self)

    def connect(self, password=None):
        pass

    def close(self):
        pass

    def ensure_dir(self, path):
        pass

    def write_file(self, remote_path, content):
        self.written_files[remote_path] = content

    def read_file(self, remote_path):
        filename = remote_path.rsplit("/", 1)[-1]
        return self.remote_files[filename]

    def backup_manifest(self, cluster_name, remote_install_dir, filename):
        from pathlib import Path

        return Path(f"/tmp/fake/{cluster_name}/{filename}"), "cafebabe" * 4

    def run(self, command, timeout=None):
        self.commands_run.append(command)
        if "coreos print-stream-json" in command:
            return FakeResult(stdout=json.dumps(COREOS_STREAM))
        return FakeResult()

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
    monkeypatch.setattr(upi, "BastionExecutor", FakeBastionExecutor)
    monkeypatch.setattr("clusterbuild.core.installers.base.SecretsBackend", _FakeSecretsBackend)
    monkeypatch.setattr(upi, "SecretsBackend", _FakeSecretsBackend)

    calls = {"templates_checked": [], "cloned": [], "guestinfo": {}, "powered_on": [], "powered_off": [], "destroyed": []}

    monkeypatch.setattr(upi.vsphere_driver, "template_exists", lambda *a, **k: (calls["templates_checked"].append(k["template_name"]), False)[1])
    monkeypatch.setattr(upi.vsphere_driver, "import_image_as_template", lambda *a, **k: None)
    monkeypatch.setattr(upi.vsphere_driver, "clone_vm_from_template", lambda *a, **k: calls["cloned"].append(k["vm_name"]))
    monkeypatch.setattr(
        upi.vsphere_driver,
        "set_ignition",
        lambda *a, **k: calls["guestinfo"].__setitem__(k["vm_name"], k["ignition_base64"]),
    )
    monkeypatch.setattr(upi.vsphere_driver, "power_on", lambda *a, **k: calls["powered_on"].append(k["vm_name"]))
    monkeypatch.setattr(upi.vsphere_driver, "power_off", lambda *a, **k: calls["powered_off"].append(k["vm_name"]))
    monkeypatch.setattr(upi.vsphere_driver, "destroy_vm", lambda *a, **k: calls["destroyed"].append(k["vm_name"]))

    FakeBastionExecutor.instances.clear()
    return calls


def _seed(platform: str) -> tuple[int, int]:
    session = get_session()
    try:
        bastion = Bastion(host="bastion.lab", ssh_user="qe", install_dir="/home/qe/clusterbuild-installs")
        session.add(bastion)
        session.commit()
        cluster = Cluster(
            name="upi-test1",
            base_domain="lab.example.com",
            ocp_version="4.18",
            install_config_platform=platform,
            infra_provisioning_target="vsphere",
            install_method="upi",
            bastion_id=bastion.id,
            status="created",
        )
        session.add(cluster)
        session.commit()
        return bastion.id, cluster.id
    finally:
        session.close()


def _base_params(platform: str, bastion_id: int, cluster_id: int) -> dict:
    answers = {
        "metadata.name": "upi-test1",
        "baseDomain": "lab.example.com",
        "controlPlane.replicas": 3,
    }
    if platform == "vsphere":
        answers["compute.replicas"] = 2
    return {
        "cluster_id": cluster_id,
        "cluster_name": "upi-test1",
        "bastion_id": bastion_id,
        "platform": platform,
        "install_method": "upi",
        "ocp_version": "4.18",
        "environment_profile": "vsphere-pnq2",
        "skip_preflight": True,
        "worker_vm_count": 2,
        "answers": answers,
    }


def test_upi_install_creates_bootstrap_masters_workers_and_tears_down_bootstrap(_fake_env):
    bastion_id, cluster_id = _seed("vsphere")
    params = _base_params("vsphere", bastion_id, cluster_id)

    upi.run(params, job_dir=None)

    assert _fake_env["cloned"] == [
        "upi-test1-bootstrap",
        "upi-test1-master-0",
        "upi-test1-master-1",
        "upi-test1-master-2",
        "upi-test1-worker-0",
        "upi-test1-worker-1",
    ]
    assert "upi-test1-bootstrap" in _fake_env["powered_on"]
    assert _fake_env["powered_off"] == ["upi-test1-bootstrap"]
    assert _fake_env["destroyed"] == ["upi-test1-bootstrap"]

    # bootstrap gets a small pointer ignition (HTTP merge), not the raw master/worker content
    bootstrap_ign = base64.b64decode(_fake_env["guestinfo"]["upi-test1-bootstrap"]).decode()
    assert "http://bastion.lab:8080/bootstrap.ign" in bootstrap_ign
    master_ign = base64.b64decode(_fake_env["guestinfo"]["upi-test1-master-0"]).decode()
    assert master_ign == "MASTER_IGNITION_CONTENT"

    session = get_session()
    try:
        cluster = session.get(Cluster, cluster_id)
        assert cluster.status == "installed"
    finally:
        session.close()


def test_upi_install_none_platform_omits_vsphere_manifest_block(_fake_env):
    bastion_id, cluster_id = _seed("none")
    params = _base_params("none", bastion_id, cluster_id)

    upi.run(params, job_dir=None)

    executor = FakeBastionExecutor.instances[-1]
    install_config = next(v for k, v in executor.written_files.items() if k.endswith("install-config.yaml"))
    assert "vsphere" not in install_config
    assert "none: {}" in install_config
    # worker_vm_count falls back to the params override since compute.replicas is a manifest constant (0), not an answer
    assert _fake_env["cloned"].count("upi-test1-worker-0") == 1
    assert _fake_env["cloned"].count("upi-test1-worker-1") == 1


def test_upi_quotes_cluster_name_in_remote_commands_and_stops_http_server(_fake_env):
    """Security-pass regression: `remote_install_dir` (which embeds the
    user-supplied cluster name) must be shlex-quoted everywhere it lands in a
    command run on the bastion, and the ignition HTTP server must be stopped
    once bootstrap is torn down."""
    bastion_id, cluster_id = _seed("vsphere")
    params = _base_params("vsphere", bastion_id, cluster_id)

    upi.run(params, job_dir=None)

    executor = FakeBastionExecutor.instances[-1]
    install_dir = "/home/qe/clusterbuild-installs/upi-test1"

    ignition_cmd = next(c for c in executor.commands_run if "create ignition-configs" in c)
    assert f"--dir {install_dir}" in ignition_cmd

    bootstrap_wait_cmd = next(c for c in executor.commands_run if "wait-for bootstrap-complete" in c)
    assert f"--dir {install_dir}" in bootstrap_wait_cmd

    install_wait_cmd = next(c for c in executor.commands_run if "wait-for install-complete" in c)
    assert f"--dir {install_dir}" in install_wait_cmd

    assert any("pkill" in c and "http.server 8080" in c for c in executor.commands_run)


def test_upi_quotes_shell_metacharacters_in_cluster_name(_fake_env):
    """Security-pass regression: a cluster name containing shell
    metacharacters (an easy typo, or a malicious/corrupted value) must never
    let commands run on the bastion break out of their intended argument --
    every `--dir` value must be the properly shlex-quoted install dir, never
    the bare/unquoted name."""
    import shlex

    session = get_session()
    try:
        bastion = Bastion(host="bastion.lab", ssh_user="qe", install_dir="/home/qe/clusterbuild-installs")
        session.add(bastion)
        session.commit()
        cluster = Cluster(
            name="evil-cluster",
            base_domain="lab.example.com",
            ocp_version="4.18",
            install_config_platform="vsphere",
            infra_provisioning_target="vsphere",
            install_method="upi",
            bastion_id=bastion.id,
            status="created",
        )
        session.add(cluster)
        session.commit()
        bastion_id, cluster_id = bastion.id, cluster.id
    finally:
        session.close()

    malicious_name = "evil'; touch /tmp/pwned; echo '"
    params = _base_params("vsphere", bastion_id, cluster_id)
    params["cluster_name"] = malicious_name
    params["answers"]["metadata.name"] = malicious_name

    upi.run(params, job_dir=None)

    executor = FakeBastionExecutor.instances[-1]
    install_dir = f"/home/qe/clusterbuild-installs/{malicious_name}"
    quoted_install_dir = shlex.quote(install_dir)

    dir_commands = [c for c in executor.commands_run if "--dir" in c]
    assert dir_commands, "expected at least one command with --dir"
    for cmd in dir_commands:
        # Proof the malicious segment is confined inside a single, properly
        # shlex-quoted --dir argument rather than able to terminate it early.
        assert f"--dir {quoted_install_dir}" in cmd


def test_upi_preflight_failure_aborts_before_any_vm_is_created(_fake_env, monkeypatch):
    bastion_id, cluster_id = _seed("vsphere")
    params = _base_params("vsphere", bastion_id, cluster_id)
    params["skip_preflight"] = False
    monkeypatch.setattr(upi, "run_preflight", lambda *a, **k: ["DNS record does not resolve: api.upi-test1.lab.example.com"])

    with pytest.raises(RuntimeError, match="Pre-flight checks failed"):
        upi.run(params, job_dir=None)

    assert _fake_env["cloned"] == []
    session = get_session()
    try:
        cluster = session.get(Cluster, cluster_id)
        assert cluster.status == "preflight-failed"
    finally:
        session.close()
