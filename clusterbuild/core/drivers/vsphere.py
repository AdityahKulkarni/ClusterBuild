"""vSphere Platform Driver -- wraps `govc`, run remotely on the bastion.

Per the plan: "Decoupled from the install-config.yaml `platform` block" --
this driver provisions VMs on vSphere infra the same way regardless of
whether the generated manifest says `platform: vsphere` or `platform: none`.
It shells out to `govc` *on the bastion* (verified present by
bastion_exec.verify_tools) rather than reimplementing the vSphere API client,
per the "wrap existing tools" development approach in the plan.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass

import yaml

from clusterbuild.core.bastion_exec import BastionExecutor, CommandResult


class VsphereDriverError(RuntimeError):
    pass


@dataclass(frozen=True)
class VsphereCredentials:
    username: str
    password: str


Credentials = VsphereCredentials  # generic alias used by installers/*.py via drivers.registry
SECRET_NAMESPACE = "vsphere"
SECRET_USERNAME_KEY = "username"
SECRET_PASSWORD_KEY = "password"


def load_environment_profile(path) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _datastore_name(datastore_path: str) -> str:
    """Environment profiles store full inventory paths like
    /OpenShift-DC/datastore/OCP-PNQ-Datastore; govc's -ds/-iso-datastore
    flags want just the datastore name."""
    return datastore_path.rstrip("/").split("/")[-1]


def _govc_env_prefix(profile: dict, creds: VsphereCredentials) -> str:
    server = profile["vcenter"]["server"]
    datacenter = profile["vcenter"]["datacenter"]
    return (
        f"GOVC_URL={shlex.quote(server)} "
        f"GOVC_USERNAME={shlex.quote(creds.username)} "
        f"GOVC_PASSWORD={shlex.quote(creds.password)} "
        f"GOVC_DATACENTER={shlex.quote(datacenter)} "
        f"GOVC_INSECURE=1"
    )


def _run(executor: BastionExecutor, profile: dict, creds: VsphereCredentials, govc_args: str, *, timeout=300) -> CommandResult:
    cmd = f"{_govc_env_prefix(profile, creds)} govc {govc_args}"
    result = executor.run(cmd, timeout=timeout)
    if not result.ok:
        raise VsphereDriverError(f"govc {govc_args.split()[0]} failed (exit {result.exit_code}): {result.stderr or result.stdout}")
    return result


def upload_iso_to_datastore(
    executor: BastionExecutor, profile: dict, creds: VsphereCredentials, *, local_iso_path: str, remote_iso_name: str
) -> None:
    datastore = _datastore_name(profile["datastore"])
    _run(
        executor,
        profile,
        creds,
        f"datastore.upload -ds {shlex.quote(datastore)} {shlex.quote(local_iso_path)} {shlex.quote(remote_iso_name)}",
        timeout=900,
    )


def create_vm_from_iso(
    executor: BastionExecutor,
    profile: dict,
    creds: VsphereCredentials,
    *,
    vm_name: str,
    iso_remote_name: str,
    cpu: int = 16,
    memory_mb: int = 32768,
    disk_gb: int = 120,
) -> None:
    datastore = _datastore_name(profile["datastore"])
    pool = profile["resource_pool"]
    network = profile["network"]
    _run(
        executor,
        profile,
        creds,
        (
            f"vm.create -on=false -net.adapter=vmxnet3 -disk.controller=pvscsi "
            f"-pool={shlex.quote(pool)} -c={cpu} -m={memory_mb} -disk={disk_gb}GB "
            f"-disk-datastore={shlex.quote(datastore)} -iso-datastore={shlex.quote(datastore)} "
            f"-iso={shlex.quote(iso_remote_name)} -net={shlex.quote(network)} {shlex.quote(vm_name)}"
        ),
        timeout=180,
    )
    # Required for RHCOS/CoreOS on every UPI/Agent-based vSphere VM per the docs.
    _run(executor, profile, creds, f"vm.change -vm {shlex.quote(vm_name)} -e disk.EnableUUID=TRUE")


def set_ignition(
    executor: BastionExecutor, profile: dict, creds: VsphereCredentials, *, vm_name: str, ignition_base64: str
) -> None:
    _run(
        executor,
        profile,
        creds,
        (
            f"vm.change -vm {shlex.quote(vm_name)} "
            f"-e guestinfo.ignition.config.data={shlex.quote(ignition_base64)} "
            f"-e guestinfo.ignition.config.data.encoding=base64 "
            f"-e disk.EnableUUID=TRUE"
        ),
    )


def import_image_as_template(
    executor: BastionExecutor, profile: dict, creds: VsphereCredentials, *, remote_image_path: str, template_name: str
) -> None:
    datastore = _datastore_name(profile["datastore"])
    pool = profile["resource_pool"]
    _run(
        executor,
        profile,
        creds,
        (
            f"import.ova -ds={shlex.quote(datastore)} -pool={shlex.quote(pool)} "
            f"-name={shlex.quote(template_name)} {shlex.quote(remote_image_path)}"
        ),
        timeout=900,
    )
    _run(executor, profile, creds, f"vm.markastemplate {shlex.quote(template_name)}")


def template_exists(executor: BastionExecutor, profile: dict, creds: VsphereCredentials, *, template_name: str) -> bool:
    cmd = f"{_govc_env_prefix(profile, creds)} govc vm.info {shlex.quote(template_name)}"
    result = executor.run(cmd, timeout=30)
    return result.ok and template_name in result.stdout


def clone_vm_from_template(
    executor: BastionExecutor, profile: dict, creds: VsphereCredentials, *, template_name: str, vm_name: str
) -> None:
    pool = profile["resource_pool"]
    _run(
        executor,
        profile,
        creds,
        f"vm.clone -vm={shlex.quote(template_name)} -pool={shlex.quote(pool)} -on=false {shlex.quote(vm_name)}",
        timeout=180,
    )


def stream_disk_location(stream: dict) -> str:
    """Extracts the vSphere OVA download URL from
    `openshift-install coreos print-stream-json` output."""
    return stream["architectures"]["x86_64"]["artifacts"]["vmware"]["formats"]["ova"]["disk"]["location"]


def power_on(executor: BastionExecutor, profile: dict, creds: VsphereCredentials, *, vm_name: str) -> None:
    _run(executor, profile, creds, f"vm.power -on=true {shlex.quote(vm_name)}", timeout=60)


def power_off(executor: BastionExecutor, profile: dict, creds: VsphereCredentials, *, vm_name: str) -> None:
    _run(executor, profile, creds, f"vm.power -off=true {shlex.quote(vm_name)}", timeout=60)


def destroy_vm(executor: BastionExecutor, profile: dict, creds: VsphereCredentials, *, vm_name: str) -> None:
    _run(executor, profile, creds, f"vm.destroy {shlex.quote(vm_name)}", timeout=60)
