"""Turn a CatalogEntry + Environment Profile + user answers into real manifest
files (install-config.yaml, agent-config.yaml, ...).

This is the one place that actually assembles YAML content, and it is
deliberately *mechanical*: every value comes from one of four sources named
in the catalog field definition (`constant`, `environment_profile`,
`user_input`, `keyring`) or is `derived` from another field already built in
this same manifest set. There is no LLM call and no free-form generation
anywhere in this module -- see docs/adr/0002-deterministic-catalog-over-llm-generation.md.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml as pyyaml
from ruamel.yaml import YAML

from clusterbuild.core.catalog_loader import CatalogEntry
from clusterbuild.core.secrets import SecretsBackend, resolve_reference

_ryaml = YAML()
_ryaml.default_flow_style = False


class ManifestValidationError(RuntimeError):
    pass


def _load_environment_profile(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        return pyyaml.safe_load(fh) or {}


def _get_nested(data: dict[str, Any], dotted_key: str) -> Any:
    node: Any = data
    for part in dotted_key.split("."):
        if not isinstance(node, dict) or part not in node:
            raise ManifestValidationError(f"Environment profile is missing key {dotted_key!r}")
        node = node[part]
    return node


def _set_nested(data: dict[str, Any], dotted_path: str, value: Any) -> None:
    parts = dotted_path.split(".")
    node = data
    for part in parts[:-1]:
        node = node.setdefault(part, {})
        if not isinstance(node, dict):
            raise ManifestValidationError(f"Cannot set {dotted_path!r}: {part!r} is not a mapping")
    node[parts[-1]] = value


def _hosts_to_nmstate(hosts: list[dict[str, Any]], machine_cidr: str | None) -> list[dict[str, Any]]:
    """Render the flat per-host answers collected by the wizard into the
    NMState shape agent-config.yaml's `hosts[].networkConfig` expects, per
    https://docs.redhat.com/en/documentation/openshift_container_platform/4.20/html/installing_an_on-premise_cluster_with_the_agent-based_installer/preparing-to-install-with-agent-based-installer
    """
    rendered = []
    for host in hosts:
        ip_address = host["ip_address"]
        if machine_cidr:
            _validate_within_cidr(ip_address, machine_cidr, f"hosts[{host.get('hostname')}].ip_address")
        iface = host.get("interface_name") or "eth0"
        mac = host["mac_address"]
        rendered.append(
            {
                "hostname": host["hostname"],
                "role": host["role"],
                "interfaces": [{"name": iface, "macAddress": mac}],
                "networkConfig": {
                    "interfaces": [
                        {
                            "name": iface,
                            "type": "ethernet",
                            "state": "up",
                            "mac-address": mac,
                            "ipv4": {
                                "enabled": True,
                                "address": [
                                    {"ip": ip_address, "prefix-length": int(host.get("prefix_length", 21))}
                                ],
                                "dhcp": False,
                            },
                        }
                    ],
                    "dns-resolver": {"config": {"server": [host["dns_server"]]}},
                    "routes": {
                        "config": [
                            {
                                "destination": "0.0.0.0/0",
                                "next-hop-address": host["gateway"],
                                "next-hop-interface": iface,
                                "table-id": 254,
                            }
                        ]
                    },
                },
            }
        )
    return rendered


def _transform(name: str, value: Any, *, machine_cidr: str | None = None) -> Any:
    if name == "wrap_cidr_list":
        return [{"cidr": value, "hostPrefix": 23}]
    if name == "hosts_to_nmstate":
        return _hosts_to_nmstate(value, machine_cidr)
    raise ManifestValidationError(f"Unknown transform {name!r}")


def _resolve_secret_placeholders(value: Any, secrets: SecretsBackend) -> Any:
    if isinstance(value, str):
        return resolve_reference(value, secrets)
    if isinstance(value, list):
        return [_resolve_secret_placeholders(v, secrets) for v in value]
    if isinstance(value, dict):
        return {k: _resolve_secret_placeholders(v, secrets) for k, v in value.items()}
    return value


def _validate_within_cidr(value: Any, cidr: str, field_path: str) -> None:
    network = ipaddress.ip_network(cidr, strict=False)
    addresses = value if isinstance(value, list) else [value]
    for addr in addresses:
        if ipaddress.ip_address(addr) not in network:
            raise ManifestValidationError(f"{field_path}: {addr} is not within the environment's {cidr}")


@dataclass
class BuildResult:
    filename: str
    content_dict: dict[str, Any]
    content_yaml: str


def build_manifests(
    entry: CatalogEntry,
    *,
    environment_profile_path: Path | None,
    answers: dict[str, Any],
    secrets: SecretsBackend,
    keyring_namespace: str = "vsphere",
) -> list[BuildResult]:
    """Build every manifest file described by `entry.manifests`.

    `answers` maps a manifest field `path` (e.g. "metadata.name") to the
    value collected from the user for that field, across all manifests in
    this entry (paths are unique per entry by construction of the catalog).
    """
    env_profile = _load_environment_profile(environment_profile_path) if environment_profile_path else {}
    machine_cidr = env_profile.get("machine_network_cidr")
    derived_registry: dict[str, Any] = {}

    results: list[BuildResult] = []
    for manifest in entry.manifests:
        filename = manifest["filename"]
        doc: dict[str, Any] = {}
        for field_def in manifest.get("fields", []):
            path = field_def["path"]
            source = field_def.get("source", "user_input")

            if source == "constant":
                value = field_def["value"]
            elif source == "user_input":
                if path in answers:
                    value = answers[path]
                elif "default" in field_def:
                    value = field_def["default"]
                elif field_def.get("required"):
                    raise ManifestValidationError(f"Missing required field {path!r} for {filename}")
                else:
                    continue
            elif source == "environment_profile":
                env_key = field_def["env_profile_key"]
                block = env_profile.get("derived_blocks", {}).get(env_key)
                if block is None:
                    block = _get_nested(env_profile, env_key)
                value = _resolve_secret_placeholders(block, secrets)
            elif source == "keyring":
                key = field_def.get("keyring_key", path)
                value = secrets.get(keyring_namespace, key)
                # The pull secret is the same Red Hat OpenShift Cluster Manager
                # token regardless of platform, so fall back to whichever
                # namespace it was originally stored under (most commonly
                # "vsphere", the first platform a user configures) rather than
                # forcing every platform's credentials to be set up again.
                if value is None and keyring_namespace != "vsphere":
                    value = secrets.get("vsphere", key)
                if value is None and field_def.get("required"):
                    raise ManifestValidationError(
                        f"No secret found for {keyring_namespace}.{key}. "
                        f"Run `clusterbuild credentials set --platform {keyring_namespace}`."
                    )
            elif source == "derived":
                value = derived_registry.get(field_def.get("derived_from", ""), answers.get(path))
            else:
                raise ManifestValidationError(f"Unknown field source {source!r} for {path}")

            if "transform" in field_def and source != "constant":
                value = _transform(field_def["transform"], value, machine_cidr=machine_cidr)

            if field_def.get("validate") == "within_env_profile_cidr" and machine_cidr:
                _validate_within_cidr(value, machine_cidr, path)

            _set_nested(doc, path, value)
            derived_registry[f"{filename}#{path}"] = value

        results.append(BuildResult(filename=filename, content_dict=doc, content_yaml=_to_yaml(doc)))
    return results


def _to_yaml(doc: dict[str, Any]) -> str:
    import io

    buf = io.StringIO()
    _ryaml.dump(doc, buf)
    return buf.getvalue()


def write_manifests(results: list[BuildResult], target_dir: Path) -> list[Path]:
    target_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for result in results:
        path = target_dir / result.filename
        path.write_text(result.content_yaml, encoding="utf-8")
        written.append(path)
    return written
