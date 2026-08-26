"""Phase 3 (+ Phase 3/4b platform-none variant): Agent-based Installer on
vSphere infra, static IP via NMState.

Handles both `platform: vsphere` and `platform: none` agent installs with
the exact same code path -- the only difference between them lives in the
catalog (catalog/4.18/vsphere/agent.yaml vs catalog/4.18/none/agent.yaml),
which controls whether install-config.yaml gets a vSphere platform block.
The VM provisioning below always goes through the vSphere driver, per
`entry.infra_provisioning_target`, regardless of what `platform` ends up in
the manifest -- this is the "decoupled platform driver" design point from
the plan.
"""

from __future__ import annotations

import shlex

from clusterbuild.core.bastion_exec import BastionExecutor
from clusterbuild.core.drivers import nutanix as nutanix_driver
from clusterbuild.core.drivers import vsphere as vsphere_driver
from clusterbuild.core.drivers.registry import driver_for
from clusterbuild.core.installers.base import (
    build_and_stage_manifests,
    get_catalog_entry_for_job,
    load_bastion,
    log,
    resolve_environment_profile_path,
    run_remote_streaming,
    set_cluster_status,
)
from clusterbuild.core.jobs import register_job_handler
from clusterbuild.core.secrets import SecretsBackend


@register_job_handler("agent_install")
def run(params: dict, job_dir) -> None:  # noqa: ARG001
    cluster_id = params["cluster_id"]
    cluster_name = params["cluster_name"]
    bastion = load_bastion(params["bastion_id"])
    entry = get_catalog_entry_for_job(params)
    remote_install_dir = f"{bastion.install_dir}/{cluster_name}"
    hosts = params["answers"].get("hosts", [])
    if not hosts:
        raise RuntimeError("No hosts defined in agent-config -- at least one host is required.")

    log(f"=== Agent-based install ({entry.platform} platform, {entry.infra_provisioning_target} infra): "
        f"{cluster_name} (OCP {params['ocp_version']}) ===")
    set_cluster_status(cluster_id, "staging-manifests")

    executor = BastionExecutor(bastion.host, bastion.ssh_user, port=bastion.ssh_port)
    executor.connect()
    try:
        build_and_stage_manifests(
            entry=entry,
            environment_profile=params.get("environment_profile"),
            answers=params["answers"],
            executor=executor,
            remote_install_dir=remote_install_dir,
            cluster_id=cluster_id,
        )

        set_cluster_status(cluster_id, "building-agent-iso")
        exit_code = run_remote_streaming(
            executor, f"openshift-install --dir {shlex.quote(remote_install_dir)} agent create image"
        )
        if exit_code != 0:
            set_cluster_status(cluster_id, "failed")
            raise RuntimeError(f"agent create image exited with code {exit_code}")

        _provision_vms(executor, entry.infra_provisioning_target, params, hosts, remote_install_dir)

        set_cluster_status(cluster_id, "installing")
        exit_code = run_remote_streaming(
            executor, f"openshift-install --dir {shlex.quote(remote_install_dir)} agent wait-for bootstrap-complete"
        )
        if exit_code != 0:
            set_cluster_status(cluster_id, "failed")
            raise RuntimeError(f"agent wait-for bootstrap-complete exited with code {exit_code}")

        exit_code = run_remote_streaming(
            executor, f"openshift-install --dir {shlex.quote(remote_install_dir)} agent wait-for install-complete"
        )
        if exit_code != 0:
            set_cluster_status(cluster_id, "failed")
            raise RuntimeError(f"agent wait-for install-complete exited with code {exit_code}")

        log("Install complete. Retrieving kubeconfig ...")
        local_kubeconfig = executor.backup_manifest(
            cluster_name=cluster_name, remote_install_dir=remote_install_dir, filename="auth/kubeconfig"
        )
        log(f"kubeconfig saved to {local_kubeconfig[0]}")
        set_cluster_status(cluster_id, "installed")
    finally:
        executor.close()


def _provision_vms(
    executor: BastionExecutor, infra_provisioning_target: str, params: dict, hosts: list[dict], remote_install_dir: str
) -> None:
    driver = driver_for(infra_provisioning_target)
    profile_path = resolve_environment_profile_path(params.get("environment_profile"))
    if profile_path is None:
        raise RuntimeError(f"An --environment-profile is required for {infra_provisioning_target} VM provisioning.")
    profile = driver.load_environment_profile(profile_path)

    secrets = SecretsBackend()
    username = secrets.get(driver.SECRET_NAMESPACE, driver.SECRET_USERNAME_KEY)
    password = secrets.get(driver.SECRET_NAMESPACE, driver.SECRET_PASSWORD_KEY)
    if not username or not password:
        raise RuntimeError(
            f"{infra_provisioning_target} credentials not found -- run "
            f"`clusterbuild credentials set --platform {driver.SECRET_NAMESPACE}`."
        )
    creds = driver.Credentials(username=username, password=password)

    remote_iso_path = f"{remote_install_dir}/agent.x86_64.iso"
    remote_iso_name = f"clusterbuild/{params['cluster_name']}/agent.x86_64.iso"

    log(f"Uploading {remote_iso_path} as {remote_iso_name} via the {infra_provisioning_target} driver ...")
    driver.upload_iso_to_datastore(executor, profile, creds, local_iso_path=remote_iso_path, remote_iso_name=remote_iso_name)

    for host in hosts:
        vm_name = f"{params['cluster_name']}-{host['hostname']}"
        log(f"Creating VM {vm_name} from {remote_iso_name} ...")
        driver.create_vm_from_iso(executor, profile, creds, vm_name=vm_name, iso_remote_name=remote_iso_name)
        log(f"Powering on {vm_name} ...")
        driver.power_on(executor, profile, creds, vm_name=vm_name)
