"""Phase 8: Hosted Control Planes (HyperShift) on OpenShift Virtualization (KubeVirt).

Unlike ipi/upi/agent/assisted, this install method never builds an
install-config.yaml -- it creates a HostedCluster/NodePool against an
*already-installed* OpenShift management cluster via the `hcp` CLI, per
https://docs.redhat.com/en/documentation/openshift_container_platform/4.18/html/hosted_control_planes/deploying-hosted-control-planes

The "manifest" built/backed up here (hcp-create-cluster-params.yaml) is a
record of the CLI parameters used, for audit/reproducibility -- not a file
`hcp`/`openshift-install` itself reads. The actual inputs specific to this
method are:
  - `management_cluster_kubeconfig_local_path`: the local backup path of an
    already-installed ClusterBuild-tracked cluster's auth/kubeconfig (see
    `cli/cluster.py`'s `--management-cluster` option), uploaded to the
    bastion and passed as KUBECONFIG for every `hcp`/`oc` invocation.
  - the pull secret, fetched directly from the keyring (same "vsphere"
    namespace fallback as manifest_builder.build_manifests) and written to
    a bastion-local file rather than embedded in the backed-up params file.
"""

from __future__ import annotations

from pathlib import Path

from clusterbuild.core.bastion_exec import BastionExecutor
from clusterbuild.core.installers.base import (
    build_and_stage_manifests,
    get_catalog_entry_for_job,
    load_bastion,
    log,
    run_remote_streaming,
    set_cluster_status,
)
from clusterbuild.core.jobs import register_job_handler
from clusterbuild.core.secrets import SecretsBackend

_PULL_SECRET_FALLBACK_NAMESPACE = "vsphere"


class HcpInstallError(RuntimeError):
    pass


def _resolve_pull_secret(secrets: SecretsBackend, platform: str) -> str:
    value = secrets.get(platform, "pull_secret") or secrets.get(_PULL_SECRET_FALLBACK_NAMESPACE, "pull_secret")
    if value is None:
        raise HcpInstallError(
            f"No pull secret found for {platform!r} (or fallback {_PULL_SECRET_FALLBACK_NAMESPACE!r}). "
            f"Run `clusterbuild credentials set --platform {platform}`."
        )
    return value


@register_job_handler("hcp_install")
def run(params: dict, job_dir) -> None:  # noqa: ARG001
    cluster_id = params["cluster_id"]
    cluster_name = params["cluster_name"]
    bastion = load_bastion(params["bastion_id"])
    entry = get_catalog_entry_for_job(params)
    remote_install_dir = f"{bastion.install_dir}/{cluster_name}"
    answers = params["answers"]

    management_cluster_name = params["management_cluster_name"]
    management_kubeconfig_local = params["management_cluster_kubeconfig_local_path"]
    hcp_namespace = answers.get("namespace", "clusters")

    log(
        f"=== Hosted Control Plane install (kubevirt platform): {cluster_name} "
        f"(OCP {params['ocp_version']}) on management cluster {management_cluster_name!r} ==="
    )
    set_cluster_status(cluster_id, "staging-manifests")

    executor = BastionExecutor(bastion.host, bastion.ssh_user, port=bastion.ssh_port)
    executor.connect()
    try:
        build_and_stage_manifests(
            entry=entry,
            environment_profile=params.get("environment_profile"),
            answers=answers,
            executor=executor,
            remote_install_dir=remote_install_dir,
            cluster_id=cluster_id,
        )

        remote_kubeconfig = f"{remote_install_dir}/management-kubeconfig"
        log(f"Uploading management cluster ({management_cluster_name}) kubeconfig to the bastion ...")
        executor.upload_from_local(Path(management_kubeconfig_local), remote_kubeconfig)

        pull_secret_json = _resolve_pull_secret(SecretsBackend(), entry.platform)
        remote_pull_secret = f"{remote_install_dir}/pull-secret.json"
        executor.write_file(remote_pull_secret, pull_secret_json)

        set_cluster_status(cluster_id, "installing")

        node_pool_replicas = int(answers.get("nodePoolReplicas", 2))
        memory = answers.get("memory", "8Gi")
        cores = int(answers.get("cores", 2))
        etcd_storage_class = answers["etcdStorageClass"]
        wait_timeout = answers.get("waitTimeout", "45m")

        command = (
            f"KUBECONFIG={remote_kubeconfig} hcp create cluster kubevirt "
            f"--name {cluster_name} --namespace {hcp_namespace} "
            f"--node-pool-replicas {node_pool_replicas} --pull-secret {remote_pull_secret} "
            f"--memory {memory} --cores {cores} --etcd-storage-class={etcd_storage_class} "
            f"--wait --timeout {wait_timeout}"
        )
        release_image = answers.get("releaseImage")
        if release_image:
            command += f" --release-image {release_image}"

        exit_code = run_remote_streaming(executor, command)
        if exit_code != 0:
            set_cluster_status(cluster_id, "failed")
            raise HcpInstallError(f"hcp create cluster exited with code {exit_code}")

        log("Hosted cluster ready. Retrieving kubeconfig ...")
        kubeconfig_result = executor.run(
            f"KUBECONFIG={remote_kubeconfig} hcp create kubeconfig --name {cluster_name} --namespace {hcp_namespace}",
            timeout=60,
        )
        if not kubeconfig_result.ok:
            set_cluster_status(cluster_id, "failed")
            raise HcpInstallError(f"hcp create kubeconfig failed: {kubeconfig_result.stderr or kubeconfig_result.stdout}")

        executor.ensure_dir(f"{remote_install_dir}/auth")
        executor.write_file(f"{remote_install_dir}/auth/kubeconfig", kubeconfig_result.stdout)

        local_kubeconfig = executor.backup_manifest(
            cluster_name=cluster_name, remote_install_dir=remote_install_dir, filename="auth/kubeconfig"
        )
        log(f"kubeconfig saved to {local_kubeconfig[0]}")
        set_cluster_status(cluster_id, "installed")
    finally:
        executor.close()
