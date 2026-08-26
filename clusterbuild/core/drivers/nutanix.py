"""Nutanix Platform Driver -- Prism Central v3 REST API (Phase 6).

Exposes the exact same function names/signatures as drivers/vsphere.py so
installers/{agent,upi}.py can provision VMs on either platform through
`drivers.registry.driver_for(entry.infra_provisioning_target)` without any
platform-specific branching in the install-method orchestration code -- see
the plan's "Platform Driver is decoupled from install_config_platform"
design point, now proven out across a second platform.

Unlike govc (a CLI we shell out to on the bastion), Prism Central's v3 API
is plain REST, so this driver calls it directly over HTTPS -- still routed
through the bastion's network path implicitly, since the bastion/workstation
already needs VPN connectivity to reach Prism Central per the plan's VPN
note, and ISO/disk images are served to Prism Central via a small HTTP
server on the bastion (the same technique used for the UPI bootstrap
ignition) rather than a separate binary upload flow.

Reference: Nutanix Prism Central v3 API reference (POST /images, POST /vms,
GET /tasks/{uuid}); OpenShift-on-Nutanix UPI docs (github.com/openshift/
installer/tree/main/docs/user/nutanix) for the ignition-via-cloud-init-user-data
mechanism AHV uses in place of vSphere's guestinfo.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import requests
import yaml

REQUEST_TIMEOUT_SECONDS = 30
TASK_POLL_INTERVAL_SECONDS = 5
TASK_POLL_TIMEOUT_SECONDS = 900


class NutanixDriverError(RuntimeError):
    pass


@dataclass(frozen=True)
class NutanixCredentials:
    username: str
    password: str


Credentials = NutanixCredentials  # generic alias used by installers/*.py via drivers.registry
SECRET_NAMESPACE = "nutanix"
SECRET_USERNAME_KEY = "prism_central_username"
SECRET_PASSWORD_KEY = "prism_central_password"


def load_environment_profile(path) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


class _PrismCentralClient:
    def __init__(self, profile: dict, creds: NutanixCredentials):
        endpoint = profile["prism_central"]["endpoint"]
        port = profile["prism_central"].get("port", 9440)
        self.base_url = f"https://{endpoint}:{port}/api/nutanix/v3"
        self.session = requests.Session()
        self.session.auth = (creds.username, creds.password)
        self.session.verify = profile["prism_central"].get("verify_tls", False)

    def post(self, path: str, payload: dict) -> dict:
        resp = self.session.post(f"{self.base_url}{path}", json=payload, timeout=REQUEST_TIMEOUT_SECONDS)
        if not resp.ok:
            raise NutanixDriverError(f"POST {path} failed ({resp.status_code}): {resp.text}")
        return resp.json()

    def get(self, path: str) -> dict:
        resp = self.session.get(f"{self.base_url}{path}", timeout=REQUEST_TIMEOUT_SECONDS)
        if not resp.ok:
            raise NutanixDriverError(f"GET {path} failed ({resp.status_code}): {resp.text}")
        return resp.json()

    def delete(self, path: str) -> None:
        resp = self.session.delete(f"{self.base_url}{path}", timeout=REQUEST_TIMEOUT_SECONDS)
        if not resp.ok:
            raise NutanixDriverError(f"DELETE {path} failed ({resp.status_code}): {resp.text}")

    def wait_task(self, task_uuid: str) -> None:
        deadline = time.time() + TASK_POLL_TIMEOUT_SECONDS
        while time.time() < deadline:
            task = self.get(f"/tasks/{task_uuid}")
            status = task.get("status")
            if status == "SUCCEEDED":
                return
            if status in ("FAILED", "CANCELED"):
                raise NutanixDriverError(f"Prism Central task {task_uuid} ended with status={status}: {task}")
            time.sleep(TASK_POLL_INTERVAL_SECONDS)
        raise NutanixDriverError(f"Prism Central task {task_uuid} did not complete within {TASK_POLL_TIMEOUT_SECONDS}s")


def _client(profile: dict, creds: NutanixCredentials) -> _PrismCentralClient:
    return _PrismCentralClient(profile, creds)


def _cluster_reference(profile: dict) -> dict:
    return {"kind": "cluster", "uuid": profile["prism_element_cluster_uuid"]}


def _subnet_reference(profile: dict) -> dict:
    return {"kind": "subnet", "uuid": profile["subnet_uuid"]}


# -- image (ISO / disk) management, served via HTTP from the bastion --------


def upload_iso_to_datastore(executor, profile: dict, creds: NutanixCredentials, *, local_iso_path: str, remote_iso_name: str) -> None:
    """Registers the ISO already sitting on the bastion as a Prism Central
    image, by pointing Prism Central at an HTTP URL served from the bastion
    itself -- avoids a separate multi-GB binary upload flow."""
    http_port = profile.get("bastion_http_port", 8081)
    executor.run(
        f"sh -c 'cd {local_iso_path.rsplit('/', 1)[0]} && "
        f"nohup python3 -m http.server {http_port} > http-server-nutanix.log 2>&1 & disown'"
    )
    filename = local_iso_path.rsplit("/", 1)[-1]
    source_uri = f"http://{executor.host}:{http_port}/{filename}"
    client = _client(profile, creds)
    response = client.post(
        "/images",
        {
            "metadata": {"kind": "image"},
            "spec": {
                "name": remote_iso_name.replace("/", "_"),
                "resources": {"image_type": "ISO_IMAGE", "source_uri": source_uri},
            },
        },
    )
    task_uuid = response["status"]["execution_context"]["task_uuid"]
    client.wait_task(task_uuid)


def create_vm_from_iso(
    executor,
    profile: dict,
    creds: NutanixCredentials,
    *,
    vm_name: str,
    iso_remote_name: str,
    cpu: int = 16,
    memory_mb: int = 32768,
    disk_gb: int = 120,
) -> None:
    client = _client(profile, creds)
    image_name = iso_remote_name.replace("/", "_")
    image_uuid = _find_image_uuid(client, image_name)
    response = client.post(
        "/vms",
        {
            "metadata": {"kind": "vm"},
            "spec": {
                "name": vm_name,
                "cluster_reference": _cluster_reference(profile),
                "resources": {
                    "num_sockets": 1,
                    "num_vcpus_per_socket": cpu,
                    "memory_size_mib": memory_mb,
                    "power_state": "OFF",
                    "disk_list": [
                        {"disk_size_mib": disk_gb * 1024, "device_properties": {"device_type": "DISK"}},
                        {
                            "device_properties": {"device_type": "CDROM"},
                            "data_source_reference": {"kind": "image", "uuid": image_uuid},
                        },
                    ],
                    "nic_list": [{"subnet_reference": _subnet_reference(profile)}],
                },
            },
        },
    )
    task_uuid = response["status"]["execution_context"]["task_uuid"]
    client.wait_task(task_uuid)


def set_ignition(executor, profile: dict, creds: NutanixCredentials, *, vm_name: str, ignition_base64: str) -> None:
    """AHV has no vSphere-style guestinfo mechanism; RHCOS Ignition is instead
    delivered through `guest_customization.cloud_init.user_data` -- Ignition
    detects and consumes this directly even though it's a cloud-init field."""
    client = _client(profile, creds)
    vm_uuid = _find_vm_uuid(client, vm_name)
    vm = client.get(f"/vms/{vm_uuid}")
    vm["spec"]["resources"]["guest_customization"] = {"cloud_init": {"user_data": ignition_base64}}
    response = client.session.put(f"{client.base_url}/vms/{vm_uuid}", json=vm, timeout=REQUEST_TIMEOUT_SECONDS)
    if not response.ok:
        raise NutanixDriverError(f"PUT /vms/{vm_uuid} (set ignition) failed: {response.text}")
    task_uuid = response.json()["status"]["execution_context"]["task_uuid"]
    client.wait_task(task_uuid)


def template_exists(executor, profile: dict, creds: NutanixCredentials, *, template_name: str) -> bool:
    client = _client(profile, creds)
    return _find_image_uuid(client, template_name, required=False) is not None


def import_image_as_template(executor, profile: dict, creds: NutanixCredentials, *, remote_image_path: str, template_name: str) -> None:
    http_port = profile.get("bastion_http_port", 8081)
    executor.run(
        f"sh -c 'cd {remote_image_path.rsplit('/', 1)[0]} && "
        f"nohup python3 -m http.server {http_port} > http-server-nutanix.log 2>&1 & disown'"
    )
    filename = remote_image_path.rsplit("/", 1)[-1]
    source_uri = f"http://{executor.host}:{http_port}/{filename}"
    client = _client(profile, creds)
    response = client.post(
        "/images",
        {
            "metadata": {"kind": "image"},
            "spec": {"name": template_name, "resources": {"image_type": "DISK_IMAGE", "source_uri": source_uri}},
        },
    )
    task_uuid = response["status"]["execution_context"]["task_uuid"]
    client.wait_task(task_uuid)


def clone_vm_from_template(executor, profile: dict, creds: NutanixCredentials, *, template_name: str, vm_name: str) -> None:
    """"Cloning" on AHV means creating a new VM whose boot disk clones the
    template image, rather than a vCenter-style vm.clone of an existing VM."""
    client = _client(profile, creds)
    image_uuid = _find_image_uuid(client, template_name)
    response = client.post(
        "/vms",
        {
            "metadata": {"kind": "vm"},
            "spec": {
                "name": vm_name,
                "cluster_reference": _cluster_reference(profile),
                "resources": {
                    "num_sockets": 1,
                    "num_vcpus_per_socket": 8,
                    "memory_size_mib": 16384,
                    "power_state": "OFF",
                    "disk_list": [{"data_source_reference": {"kind": "image", "uuid": image_uuid}}],
                    "nic_list": [{"subnet_reference": _subnet_reference(profile)}],
                },
            },
        },
    )
    task_uuid = response["status"]["execution_context"]["task_uuid"]
    client.wait_task(task_uuid)


def power_on(executor, profile: dict, creds: NutanixCredentials, *, vm_name: str) -> None:
    _set_power_state(profile, creds, vm_name, "ON")


def power_off(executor, profile: dict, creds: NutanixCredentials, *, vm_name: str) -> None:
    _set_power_state(profile, creds, vm_name, "OFF")


def destroy_vm(executor, profile: dict, creds: NutanixCredentials, *, vm_name: str) -> None:
    client = _client(profile, creds)
    vm_uuid = _find_vm_uuid(client, vm_name)
    client.delete(f"/vms/{vm_uuid}")


def stream_disk_location(stream: dict[str, Any]) -> str:
    """RHCOS build metadata (`openshift-install coreos print-stream-json`)
    doesn't publish a dedicated Nutanix artifact; Nutanix's own UPI docs
    direct users to the generic QEMU qcow2.gz artifact, which AHV can import
    directly as a disk image."""
    return stream["architectures"]["x86_64"]["artifacts"]["qemu"]["formats"]["qcow2.gz"]["disk"]["location"]


def _set_power_state(profile: dict, creds: NutanixCredentials, vm_name: str, state: str) -> None:
    client = _client(profile, creds)
    vm_uuid = _find_vm_uuid(client, vm_name)
    vm = client.get(f"/vms/{vm_uuid}")
    vm["spec"]["resources"]["power_state"] = state
    response = client.session.put(f"{client.base_url}/vms/{vm_uuid}", json=vm, timeout=REQUEST_TIMEOUT_SECONDS)
    if not response.ok:
        raise NutanixDriverError(f"PUT /vms/{vm_uuid} (power {state}) failed: {response.text}")
    task_uuid = response.json()["status"]["execution_context"]["task_uuid"]
    client.wait_task(task_uuid)


def _find_image_uuid(client: _PrismCentralClient, name: str, *, required: bool = True) -> str | None:
    result = client.post("/images/list", {"filter": f"name=={name}", "kind": "image"})
    entities = result.get("entities", [])
    if not entities:
        if required:
            raise NutanixDriverError(f"No Prism Central image named {name!r} found")
        return None
    return entities[0]["metadata"]["uuid"]


def _find_vm_uuid(client: _PrismCentralClient, name: str) -> str:
    result = client.post("/vms/list", {"filter": f"vm_name=={name}", "kind": "vm"})
    entities = result.get("entities", [])
    if not entities:
        raise NutanixDriverError(f"No Prism Central VM named {name!r} found")
    return entities[0]["metadata"]["uuid"]
