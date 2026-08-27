"""Phase 2 (+ Phase 7 cloud extension): IPI end-to-end flow.

install-config.yaml -> `openshift-install create cluster` on the bastion,
streamed into the job log -> config backup (already done as part of
staging) -> kubeconfig retrieval. Works unmodified across every IPI-eligible
platform (vSphere, Nutanix, AWS, Azure, GCP): the installer provisions all
infrastructure itself, so there is no per-platform VM-provisioning driver
call here (contrast with agent.py/upi.py's `drivers.registry.driver_for`).
The only platform-specific step is staging cloud credentials onto the
bastion for AWS/Azure/GCP (see `cloud_credentials.py`) -- vSphere/Nutanix
credentials are already embedded in install-config.yaml by manifest_builder.
"""

from __future__ import annotations

import shlex

from clusterbuild.core import cloud_credentials
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
from clusterbuild.core.secrets import SecretsBackend, get_bastion_password


@register_job_handler("ipi_install")
def run(params: dict, job_dir) -> None:  # noqa: ARG001 -- job_dir unused, kept for handler signature symmetry
    cluster_id = params["cluster_id"]
    bastion = load_bastion(params["bastion_id"])
    entry = get_catalog_entry_for_job(params)
    remote_install_dir = f"{bastion.install_dir}/{params['cluster_name']}"

    log(f"=== {entry.platform} IPI install: {params['cluster_name']} (OCP {params['ocp_version']}) ===")
    set_cluster_status(cluster_id, "staging-manifests")

    executor = BastionExecutor(bastion.host, bastion.ssh_user, port=bastion.ssh_port)
    executor.connect(password=get_bastion_password(SecretsBackend(), bastion.host))
    try:
        build_and_stage_manifests(
            entry=entry,
            environment_profile=params.get("environment_profile"),
            answers=params["answers"],
            executor=executor,
            remote_install_dir=remote_install_dir,
            cluster_id=cluster_id,
        )

        if entry.platform in cloud_credentials.CLOUD_PLATFORMS:
            log(f"Staging {entry.platform} credentials on the bastion ...")
            cloud_credentials.stage(executor, entry.platform, SecretsBackend())

        set_cluster_status(cluster_id, "installing")
        # remote_install_dir embeds the user-supplied cluster name -- quote it
        # so a stray shell metacharacter in a cluster-name typo can't be
        # interpreted by the bastion's shell.
        exit_code = run_remote_streaming(
            executor,
            f"openshift-install create cluster --dir {shlex.quote(remote_install_dir)} --log-level=info",
        )
        if exit_code != 0:
            set_cluster_status(cluster_id, "failed")
            raise RuntimeError(f"openshift-install exited with code {exit_code}")

        log("Install complete. Retrieving kubeconfig ...")
        local_kubeconfig = executor.backup_manifest(
            cluster_name=params["cluster_name"],
            remote_install_dir=remote_install_dir,
            filename="auth/kubeconfig",
        )
        log(f"kubeconfig saved to {local_kubeconfig[0]}")
        set_cluster_status(cluster_id, "installed")
    finally:
        executor.close()
