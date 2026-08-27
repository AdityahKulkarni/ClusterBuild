"""Phase 4 (+ platform-none variant): UPI on vSphere infra.

DNS/LB pre-flight validation -> ignition config generation -> RHCOS
OVA/template -> govc-driven VM provisioning sequence (bootstrap -> masters ->
workers) -> bootstrap teardown -> install-complete, per
catalog/4.18/vsphere/upi.yaml#provisioning (also used, minus vSphere manifest
fields, by catalog/4.18/none/upi.yaml).
"""

from __future__ import annotations

import base64
import json
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
from clusterbuild.core.preflight import run_preflight
from clusterbuild.core.secrets import SecretsBackend, get_bastion_password

HTTP_SERVER_PORT = 8080


@register_job_handler("upi_install")
def run(params: dict, job_dir) -> None:  # noqa: ARG001
    cluster_id = params["cluster_id"]
    cluster_name = params["cluster_name"]
    bastion = load_bastion(params["bastion_id"])
    entry = get_catalog_entry_for_job(params)
    remote_install_dir = f"{bastion.install_dir}/{cluster_name}"
    answers = params["answers"]
    base_domain = answers["baseDomain"]
    control_plane_count = int(answers.get("controlPlane.replicas", 3))
    worker_vm_count = int(answers.get("compute.replicas", params.get("worker_vm_count", 2)))

    log(f"=== vSphere UPI install ({entry.platform} platform): {cluster_name} (OCP {params['ocp_version']}) ===")

    if not params.get("skip_preflight"):
        log("Running DNS/load-balancer pre-flight checks ...")
        problems = run_preflight(
            entry, cluster_name=cluster_name, base_domain=base_domain, load_balancer_host=params.get("lb_host")
        )
        if problems:
            set_cluster_status(cluster_id, "preflight-failed")
            for problem in problems:
                log(f"  FAIL: {problem}")
            raise RuntimeError(
                "Pre-flight checks failed -- fix DNS/load-balancer per `clusterbuild checklist generate` "
                "before retrying, or pass --skip-preflight to override."
            )
        log("Pre-flight checks passed.")

    set_cluster_status(cluster_id, "staging-manifests")
    executor = BastionExecutor(bastion.host, bastion.ssh_user, port=bastion.ssh_port)
    executor.connect(password=get_bastion_password(SecretsBackend(), bastion.host))
    try:
        build_and_stage_manifests(
            entry=entry,
            environment_profile=params.get("environment_profile"),
            answers=answers,
            executor=executor,
            remote_install_dir=remote_install_dir,
            cluster_id=cluster_id,
        )

        set_cluster_status(cluster_id, "generating-ignition")
        exit_code = run_remote_streaming(
            executor, f"openshift-install create ignition-configs --dir {shlex.quote(remote_install_dir)}"
        )
        if exit_code != 0:
            set_cluster_status(cluster_id, "failed")
            raise RuntimeError(f"create ignition-configs exited with code {exit_code}")

        driver = driver_for(entry.infra_provisioning_target)
        profile_path = resolve_environment_profile_path(params.get("environment_profile"))
        if profile_path is None:
            raise RuntimeError(f"An --environment-profile is required for {entry.infra_provisioning_target} VM provisioning.")
        profile = driver.load_environment_profile(profile_path)

        secrets = SecretsBackend()
        username = secrets.get(driver.SECRET_NAMESPACE, driver.SECRET_USERNAME_KEY)
        password = secrets.get(driver.SECRET_NAMESPACE, driver.SECRET_PASSWORD_KEY)
        if not username or not password:
            raise RuntimeError(
                f"{entry.infra_provisioning_target} credentials not found -- run "
                f"`clusterbuild credentials set --platform {driver.SECRET_NAMESPACE}`."
            )
        creds = driver.Credentials(username=username, password=password)

        set_cluster_status(cluster_id, "provisioning-vms")
        template_name = f"rhcos-{params['ocp_version']}-template"
        _ensure_rhcos_template(executor, driver, profile, creds, remote_install_dir, template_name)
        _start_ignition_http_server(executor, remote_install_dir)

        bootstrap_vm = f"{cluster_name}-bootstrap"
        bootstrap_url = f"http://{bastion.host}:{HTTP_SERVER_PORT}/bootstrap.ign"
        bootstrap_ignition_b64 = _merge_ignition_pointer_b64(bootstrap_url)
        log(f"Cloning + booting {bootstrap_vm} (ignition served from {bootstrap_url}) ...")
        _clone_configure_and_boot(executor, driver, profile, creds, template_name, bootstrap_vm, bootstrap_ignition_b64)

        master_vms = [f"{cluster_name}-master-{i}" for i in range(control_plane_count)]
        master_ign_b64 = _read_and_b64(executor, f"{remote_install_dir}/master.ign")
        for vm_name in master_vms:
            log(f"Cloning + booting {vm_name} ...")
            _clone_configure_and_boot(executor, driver, profile, creds, template_name, vm_name, master_ign_b64)

        worker_vms = [f"{cluster_name}-worker-{i}" for i in range(worker_vm_count)]
        worker_ign_b64 = _read_and_b64(executor, f"{remote_install_dir}/worker.ign")
        for vm_name in worker_vms:
            log(f"Cloning + booting {vm_name} ...")
            _clone_configure_and_boot(executor, driver, profile, creds, template_name, vm_name, worker_ign_b64)

        set_cluster_status(cluster_id, "waiting-for-bootstrap")
        exit_code = run_remote_streaming(
            executor,
            f"openshift-install wait-for bootstrap-complete --dir {shlex.quote(remote_install_dir)} --log-level=info",
        )
        if exit_code != 0:
            set_cluster_status(cluster_id, "failed")
            raise RuntimeError(f"wait-for bootstrap-complete exited with code {exit_code}")

        log(
            f"Bootstrap complete. Remove {bootstrap_vm} from the 6443/22623 load-balancer pools now "
            "if your load balancer doesn't do this automatically."
        )
        driver.power_off(executor, profile, creds, vm_name=bootstrap_vm)
        driver.destroy_vm(executor, profile, creds, vm_name=bootstrap_vm)
        # By this point every node has already fetched its ignition config
        # (masters/workers get theirs via guestinfo at boot, not this HTTP
        # server -- only the bootstrap node used it, and bootstrap-complete
        # has now been confirmed), so the unauthenticated HTTP server is no
        # longer needed. Stop it now to shrink the window it's reachable.
        _stop_ignition_http_server(executor)

        set_cluster_status(cluster_id, "installing")
        exit_code = run_remote_streaming(
            executor,
            f"openshift-install wait-for install-complete --dir {shlex.quote(remote_install_dir)} --log-level=info",
        )
        if exit_code != 0:
            set_cluster_status(cluster_id, "failed")
            raise RuntimeError(f"wait-for install-complete exited with code {exit_code}")

        log("Install complete. Retrieving kubeconfig ...")
        local_kubeconfig = executor.backup_manifest(
            cluster_name=cluster_name, remote_install_dir=remote_install_dir, filename="auth/kubeconfig"
        )
        log(f"kubeconfig saved to {local_kubeconfig[0]}")
        set_cluster_status(cluster_id, "installed")
    finally:
        executor.close()


def _clone_configure_and_boot(executor, driver, profile, creds, template_name, vm_name, ignition_b64) -> None:
    driver.clone_vm_from_template(executor, profile, creds, template_name=template_name, vm_name=vm_name)
    driver.set_ignition(executor, profile, creds, vm_name=vm_name, ignition_base64=ignition_b64)
    driver.power_on(executor, profile, creds, vm_name=vm_name)


def _merge_ignition_pointer_b64(source_url: str) -> str:
    """A small ignition config that just tells the bootstrap node to fetch its
    real config over HTTP -- avoids the vSphere guestinfo size limit that a
    full bootstrap.ign (which embeds the release image pull spec) can hit."""
    pointer = {"ignition": {"config": {"merge": [{"source": source_url}]}, "version": "3.2.0"}}
    return base64.b64encode(json.dumps(pointer).encode()).decode()


def _read_and_b64(executor: BastionExecutor, remote_path: str) -> str:
    content = executor.read_file(remote_path)
    raw = content.encode() if isinstance(content, str) else content
    return base64.b64encode(raw).decode()


def _stop_ignition_http_server(executor: BastionExecutor) -> None:
    """Best-effort teardown of the ignition HTTP server started by
    `_start_ignition_http_server` -- never raises, since a failure here
    shouldn't fail an otherwise-successful install."""
    try:
        executor.run(f"pkill -f 'http.server {HTTP_SERVER_PORT}' || true", timeout=10)
    except Exception:  # noqa: BLE001 -- best-effort cleanup only
        pass


def _start_ignition_http_server(executor: BastionExecutor, remote_install_dir: str) -> None:
    log(f"Starting HTTP server on the bastion to serve bootstrap.ign on port {HTTP_SERVER_PORT} ...")
    # Quote remote_install_dir even though it's nested inside a single-quoted
    # `sh -c '...'` string -- shlex.quote() emits a `'"'"'`-style escape for
    # embedded single quotes, so a cluster name containing one can't break out
    # of the outer quoting and inject additional shell commands.
    executor.run(
        "sh -c " + shlex.quote(
            f"cd {shlex.quote(remote_install_dir)} && "
            f"nohup python3 -m http.server {HTTP_SERVER_PORT} > http-server.log 2>&1 & disown"
        )
    )


def _ensure_rhcos_template(executor: BastionExecutor, driver, profile: dict, creds, remote_install_dir: str, template_name: str) -> None:
    if driver.template_exists(executor, profile, creds, template_name=template_name):
        log(f"RHCOS template {template_name} already exists -- reusing it.")
        return

    log("Discovering the RHCOS disk image URL via `openshift-install coreos print-stream-json` ...")
    result = executor.run("openshift-install coreos print-stream-json", timeout=60)
    if not result.ok:
        raise RuntimeError("could not run `openshift-install coreos print-stream-json` on the bastion")
    stream = json.loads(result.stdout)
    image_url = driver.stream_disk_location(stream)

    remote_image_path = f"{remote_install_dir}/rhcos-image"
    log(f"Downloading RHCOS disk image from {image_url} ...")
    exit_code = run_remote_streaming(
        executor, f"curl -L -o {shlex.quote(remote_image_path)} {shlex.quote(image_url)}"
    )
    if exit_code != 0:
        raise RuntimeError("failed to download the RHCOS disk image onto the bastion")

    log(f"Importing {remote_image_path} as template {template_name} ...")
    driver.import_image_as_template(executor, profile, creds, remote_image_path=remote_image_path, template_name=template_name)
