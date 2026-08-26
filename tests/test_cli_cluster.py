"""Phase 8: `_select_management_cluster` resolves an already-installed
ClusterBuild-tracked cluster (with a backed-up auth/kubeconfig) to use as the
HyperShift management cluster for `cluster create --method hcp`."""

from __future__ import annotations

import typer
import pytest

from clusterbuild.cli.cluster import _select_management_cluster
from clusterbuild.core.state import Bastion, Cluster, ClusterConfig, get_session


@pytest.fixture()
def _fake_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CLUSTERBUILD_HOME", str(tmp_path))


def _seed_installed_cluster(name: str, *, with_kubeconfig: bool = True) -> int:
    session = get_session()
    try:
        bastion = session.query(Bastion).filter_by(host="bastion.lab").one_or_none()
        if bastion is None:
            bastion = Bastion(host="bastion.lab", ssh_user="qe", install_dir="/home/qe/installs")
            session.add(bastion)
            session.commit()
        cluster = Cluster(
            name=name,
            base_domain="lab.example.com",
            ocp_version="4.18",
            install_config_platform="vsphere",
            infra_provisioning_target="vsphere",
            install_method="ipi",
            bastion_id=bastion.id,
            status="installed",
        )
        session.add(cluster)
        session.commit()
        if with_kubeconfig:
            session.add(
                ClusterConfig(
                    cluster_id=cluster.id,
                    filename="auth/kubeconfig",
                    backup_path=f"/tmp/fake/{name}/auth/kubeconfig",
                    checksum_sha256="deadbeef",
                    catalog_ocp_version="4.18",
                    catalog_schema_ref="4.18",
                )
            )
            session.commit()
        return cluster.id
    finally:
        session.close()


def test_selects_the_only_installed_candidate_automatically(_fake_env):
    _seed_installed_cluster("mgmt1")
    name, kubeconfig_path = _select_management_cluster(None)
    assert name == "mgmt1"
    assert kubeconfig_path == "/tmp/fake/mgmt1/auth/kubeconfig"


def test_selects_by_explicit_name(_fake_env):
    _seed_installed_cluster("mgmt1")
    _seed_installed_cluster("mgmt2")
    name, kubeconfig_path = _select_management_cluster("mgmt2")
    assert name == "mgmt2"
    assert kubeconfig_path == "/tmp/fake/mgmt2/auth/kubeconfig"


def test_raises_when_named_cluster_not_found(_fake_env):
    _seed_installed_cluster("mgmt1")
    with pytest.raises(typer.Exit):
        _select_management_cluster("does-not-exist")


def test_raises_when_no_installed_clusters_exist(_fake_env):
    with pytest.raises(typer.Exit):
        _select_management_cluster(None)


def test_raises_when_candidate_has_no_backed_up_kubeconfig(_fake_env):
    _seed_installed_cluster("mgmt1", with_kubeconfig=False)
    with pytest.raises(typer.Exit):
        _select_management_cluster("mgmt1")


def test_excludes_other_hcp_clusters_from_candidates(_fake_env):
    """A hosted cluster can't itself be a HyperShift management cluster in
    this flow -- only "real" IPI/UPI/Agent/Assisted installs qualify."""
    session = get_session()
    try:
        bastion = Bastion(host="bastion.lab", ssh_user="qe", install_dir="/home/qe/installs")
        session.add(bastion)
        session.commit()
        hosted = Cluster(
            name="already-hosted",
            base_domain="",
            ocp_version="4.18",
            install_config_platform="kubevirt",
            infra_provisioning_target="kubevirt",
            install_method="hcp",
            bastion_id=bastion.id,
            status="installed",
        )
        session.add(hosted)
        session.commit()
    finally:
        session.close()

    with pytest.raises(typer.Exit):
        _select_management_cluster(None)
