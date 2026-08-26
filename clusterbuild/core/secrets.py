"""Credential storage: OS keyring by default, optional team Vault (Phase 1).

No app-managed secrets server. Every credential lives either in the
requesting user's own OS keychain (macOS Keychain / GNOME Keyring / Windows
Credential Manager, via the `keyring` library) or, if the team already runs
a HashiCorp Vault, in that Vault under the user's own personal token -- never
in ClusterBuild's local SQLite state or in plaintext config files.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import keyring
from keyring.errors import PasswordDeleteError

_SERVICE_PREFIX = "clusterbuild"


class SecretsError(RuntimeError):
    pass


def _service_name(namespace: str) -> str:
    return f"{_SERVICE_PREFIX}:{namespace}"


@dataclass(frozen=True)
class VaultConfig:
    address: str
    token: str
    mount_point: str = "secret"


class SecretsBackend:
    """Keyring-first backend, with an optional Vault client for teams that have one."""

    def __init__(self, vault_config: Optional[VaultConfig] = None) -> None:
        self._vault_config = vault_config
        self._vault_client = None

    # -- keyring (default) ---------------------------------------------
    def set_keyring_secret(self, namespace: str, key: str, value: str) -> None:
        keyring.set_password(_service_name(namespace), key, value)

    def get_keyring_secret(self, namespace: str, key: str) -> Optional[str]:
        return keyring.get_password(_service_name(namespace), key)

    def delete_keyring_secret(self, namespace: str, key: str) -> None:
        try:
            keyring.delete_password(_service_name(namespace), key)
        except PasswordDeleteError:
            pass

    # -- optional Vault --------------------------------------------------
    def _vault(self):
        if self._vault_client is None:
            if self._vault_config is None:
                raise SecretsError("Vault is not configured; use `clusterbuild credentials set --backend keyring`.")
            import hvac  # local import: optional dependency path

            self._vault_client = hvac.Client(url=self._vault_config.address, token=self._vault_config.token)
            if not self._vault_client.is_authenticated():
                raise SecretsError("Vault authentication failed -- check the personal token.")
        return self._vault_client

    def set_vault_secret(self, namespace: str, key: str, value: str) -> None:
        path = f"clusterbuild/{namespace}"
        client = self._vault()
        existing = {}
        try:
            existing = client.secrets.kv.v2.read_secret_version(
                path=path, mount_point=self._vault_config.mount_point
            )["data"]["data"]
        except Exception:
            pass
        existing[key] = value
        client.secrets.kv.v2.create_or_update_secret(
            path=path, secret=existing, mount_point=self._vault_config.mount_point
        )

    def get_vault_secret(self, namespace: str, key: str) -> Optional[str]:
        path = f"clusterbuild/{namespace}"
        client = self._vault()
        try:
            data = client.secrets.kv.v2.read_secret_version(
                path=path, mount_point=self._vault_config.mount_point
            )["data"]["data"]
        except Exception:
            return None
        return data.get(key)

    # -- unified entrypoint used by the rest of the app ------------------
    def get(self, namespace: str, key: str, *, backend: str = "keyring") -> Optional[str]:
        if backend == "vault":
            return self.get_vault_secret(namespace, key)
        return self.get_keyring_secret(namespace, key)

    def set(self, namespace: str, key: str, value: str, *, backend: str = "keyring") -> None:
        if backend == "vault":
            self.set_vault_secret(namespace, key, value)
        else:
            self.set_keyring_secret(namespace, key, value)

    def delete(self, namespace: str, key: str, *, backend: str = "keyring") -> None:
        if backend == "vault":
            raise SecretsError("Deleting individual Vault keys isn't supported yet -- edit the secret in Vault directly.")
        self.delete_keyring_secret(namespace, key)


def default_backend() -> SecretsBackend:
    return SecretsBackend()


def resolve_reference(ref: str, backend: SecretsBackend) -> str:
    """Resolve a `{{ keyring:<namespace>.<key> }}` placeholder used in Environment Profiles."""
    ref = ref.strip()
    if not (ref.startswith("{{") and ref.endswith("}}")):
        return ref
    inner = ref[2:-2].strip()
    if ":" not in inner or "." not in inner:
        raise SecretsError(f"Malformed secret reference: {ref!r}")
    kind, rest = inner.split(":", 1)
    namespace, key = rest.split(".", 1)
    value = backend.get(namespace, key, backend="vault" if kind == "vault" else "keyring")
    if value is None:
        raise SecretsError(
            f"No credential found for {namespace}.{key}. Run `clusterbuild credentials set --platform {namespace}`."
        )
    return value
