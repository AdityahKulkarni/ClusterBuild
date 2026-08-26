"""Phase 5: Assisted Installer for vSphere (self-hosted podman or SaaS).

Unlike ipi/upi/agent, the Assisted Installer is driven through the
assisted-service REST API rather than `openshift-install`; the "manifest" is
a JSON request body (catalog/4.18/vsphere/assisted.yaml) built with the same
deterministic `manifest_builder.build_manifests` used everywhere else --
only the target format differs.
"""

from __future__ import annotations

import json
import shlex

from clusterbuild.core.bastion_exec import BastionExecutor
from clusterbuild.core.drivers import nutanix as nutanix_driver
from clusterbuild.core.drivers import vsphere as vsphere_driver
from clusterbuild.core.drivers.registry import driver_for
from clusterbuild.core.drivers.assisted_api import (
    SAAS_BASE_URL,
    AssistedApiError,
    AssistedServiceClient,
    exchange_offline_token,
)
from clusterbuild.core.installers.base import (
    get_catalog_entry_for_job,
    load_bastion,
    log,
    record_local_backup,
    resolve_environment_profile_path,
    set_cluster_status,
)
from clusterbuild.core.jobs import register_job_handler
from clusterbuild.core.manifest_builder import build_manifests
from clusterbuild.core.secrets import SecretsBackend

SELF_HOSTED_PORT = 8090


@register_job_handler("assisted_install")
def run(params: dict, job_dir) -> None:  # noqa: ARG001
    cluster_id = params["cluster_id"]
    cluster_name = params["cluster_name"]
    bastion = load_bastion(params["bastion_id"])
    entry = get_catalog_entry_for_job(params)
    backend = params.get("backend", entry.backend_options[0] if entry.backend_options else "self_hosted")
    if entry.backend_options and backend not in entry.backend_options:
        raise RuntimeError(f"backend={backend!r} is not one of {entry.backend_options} for this catalog entry")

    log(f"=== Assisted Installer ({backend} backend): {cluster_name} (OCP {params['ocp_version']}) ===")
    set_cluster_status(cluster_id, "building-request")

    secrets = SecretsBackend()
    answers = dict(params["answers"])
    answers.setdefault("openshift_version", params["ocp_version"])

    env_profile_path = resolve_environment_profile_path(params.get("environment_profile"))
    results = build_manifests(entry, environment_profile_path=env_profile_path, answers=answers, secrets=secrets)
    cluster_request = results[0].content_dict
    record_local_backup(
        cluster_id=cluster_id,
        cluster_name=cluster_name,
        filename="cluster-request.json",
        content=json.dumps(cluster_request, indent=2).encode(),
        entry=entry,
    )

    executor = BastionExecutor(bastion.host, bastion.ssh_user, port=bastion.ssh_port)
    executor.connect()
    try:
        if backend == "self_hosted":
            client = _ensure_self_hosted_service(executor, bastion.host)
        else:
            offline_token = secrets.get("assisted_saas", "offline_token")
            if not offline_token:
                raise RuntimeError(
                    "No Red Hat offline token stored -- run "
                    "`clusterbuild credentials set --platform assisted_saas` first."
                )
            access_token = exchange_offline_token(offline_token)
            client = AssistedServiceClient(SAAS_BASE_URL, access_token=access_token)

        set_cluster_status(cluster_id, "creating-cluster")
        remote_cluster = client.create_cluster(cluster_request)
        remote_cluster_id = remote_cluster["id"]
        log(f"Created assisted-service cluster {remote_cluster_id}")

        infra_env = client.create_infra_env(
            {
                "name": f"{cluster_name}-infra-env",
                "cluster_id": remote_cluster_id,
                "openshift_version": params["ocp_version"],
                "pull_secret": secrets.get("vsphere", "pull_secret"),
                "cpu_architecture": "x86_64",
            }
        )
        infra_env_id = infra_env["id"]
        log(f"Created infra-env {infra_env_id}")

        iso_url = client.discovery_iso_url(infra_env_id)
        log(f"Discovery ISO available at {iso_url}")

        host_count = int(answers.get("controlPlane.replicas", 3)) + int(params.get("worker_vm_count", 0))
        set_cluster_status(cluster_id, "provisioning-vms")
        _provision_hosts(executor, entry.infra_provisioning_target, params, bastion, iso_url, host_count)

        set_cluster_status(cluster_id, "waiting-for-ready")
        client.wait_for_status(
            remote_cluster_id,
            ["ready"],
            timeout=params.get("ready_timeout_seconds", 3600),
            on_poll=lambda c: log(f"cluster status: {c.get('status')} ({c.get('status_info', '')})"),
        )

        set_cluster_status(cluster_id, "installing")
        client.install_cluster(remote_cluster_id)
        client.wait_for_status(
            remote_cluster_id,
            ["installed"],
            timeout=params.get("install_timeout_seconds", 7200),
            on_poll=lambda c: log(f"cluster status: {c.get('status')} ({c.get('status_info', '')})"),
        )

        log("Install complete. Retrieving kubeconfig ...")
        kubeconfig_bytes = client.kubeconfig(remote_cluster_id)
        local_path = record_local_backup(
            cluster_id=cluster_id,
            cluster_name=cluster_name,
            filename="auth/kubeconfig",
            content=kubeconfig_bytes,
            entry=entry,
        )
        log(f"kubeconfig saved to {local_path}")
        set_cluster_status(cluster_id, "installed")
    except AssistedApiError as exc:
        set_cluster_status(cluster_id, "failed")
        raise RuntimeError(str(exc)) from exc
    finally:
        executor.close()


def _ensure_self_hosted_service(executor: BastionExecutor, bastion_host: str) -> AssistedServiceClient:
    base_url = f"http://{bastion_host}:{SELF_HOSTED_PORT}/api/assisted-install"
    health = executor.run(f"curl -sf http://localhost:{SELF_HOSTED_PORT}/health", timeout=10)
    if not health.ok:
        log("assisted-service not reachable on :8090 -- attempting `podman play kube` deploy ...")
        deploy = executor.run(
            "podman play kube ~/assisted-service-deploy/pod.yml "
            "--configmap ~/assisted-service-deploy/configmap.yml",
            timeout=120,
        )
        if not deploy.ok:
            raise RuntimeError(
                "Self-hosted assisted-service is not running and could not be auto-deployed. Prepare "
                "~/assisted-service-deploy/{pod.yml,configmap.yml} on the bastion per "
                "https://github.com/openshift/assisted-service/tree/master/deploy/podman, or pass --backend saas."
            )
        health = executor.run(f"curl -sf http://localhost:{SELF_HOSTED_PORT}/health", timeout=10)
        if not health.ok:
            raise RuntimeError("assisted-service still not responding on :8090 after the deploy attempt.")
    return AssistedServiceClient(base_url)


def _provision_hosts(
    executor: BastionExecutor, infra_provisioning_target: str, params: dict, bastion, iso_url: str, host_count: int
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

    cluster_name = params["cluster_name"]
    remote_install_dir = f"{bastion.install_dir}/{cluster_name}"
    executor.ensure_dir(remote_install_dir)
    remote_iso_path = f"{remote_install_dir}/discovery.iso"

    log("Downloading discovery ISO to the bastion ...")
    download = executor.run(f"curl -L -o {remote_iso_path} {shlex.quote(iso_url)}", timeout=600)
    if not download.ok:
        raise RuntimeError("failed to download the discovery ISO onto the bastion")

    remote_iso_name = f"clusterbuild/{cluster_name}/discovery.iso"
    driver.upload_iso_to_datastore(executor, profile, creds, local_iso_path=remote_iso_path, remote_iso_name=remote_iso_name)

    for i in range(host_count):
        vm_name = f"{cluster_name}-host-{i}"
        log(f"Creating VM {vm_name} from {remote_iso_name} ...")
        driver.create_vm_from_iso(executor, profile, creds, vm_name=vm_name, iso_remote_name=remote_iso_name)
        driver.power_on(executor, profile, creds, vm_name=vm_name)
