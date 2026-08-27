"""Unit tests for the bastion-password keyring helpers in secrets.py.

Uses a fake in-memory backend (never the real OS keyring, matching every
other test's `_FakeSecretsBackend` pattern) so these run identically in any
environment."""

from __future__ import annotations

from clusterbuild.core.secrets import delete_bastion_password, get_bastion_password, set_bastion_password


class _FakeSecretsBackend:
    def __init__(self):
        self._store: dict[tuple[str, str], str] = {}

    def get(self, namespace, key, *, backend="keyring"):
        return self._store.get((namespace, key))

    def set(self, namespace, key, value, *, backend="keyring"):
        self._store[(namespace, key)] = value

    def delete(self, namespace, key, *, backend="keyring"):
        self._store.pop((namespace, key), None)


def test_get_bastion_password_absent_returns_none():
    secrets = _FakeSecretsBackend()
    assert get_bastion_password(secrets, "bastion01.lab.example.com") is None


def test_set_then_get_bastion_password_round_trips():
    secrets = _FakeSecretsBackend()
    set_bastion_password(secrets, "bastion01.lab.example.com", "s3cret")
    assert get_bastion_password(secrets, "bastion01.lab.example.com") == "s3cret"


def test_bastion_password_is_namespaced_per_host():
    secrets = _FakeSecretsBackend()
    set_bastion_password(secrets, "bastion01.lab.example.com", "pw-one")
    set_bastion_password(secrets, "bastion02.lab.example.com", "pw-two")
    assert get_bastion_password(secrets, "bastion01.lab.example.com") == "pw-one"
    assert get_bastion_password(secrets, "bastion02.lab.example.com") == "pw-two"
    assert secrets._store[("bastion:bastion01.lab.example.com", "ssh_password")] == "pw-one"


def test_delete_bastion_password_clears_stored_value():
    secrets = _FakeSecretsBackend()
    set_bastion_password(secrets, "bastion01.lab.example.com", "s3cret")
    delete_bastion_password(secrets, "bastion01.lab.example.com")
    assert get_bastion_password(secrets, "bastion01.lab.example.com") is None


def test_delete_bastion_password_is_a_noop_when_nothing_stored():
    secrets = _FakeSecretsBackend()
    delete_bastion_password(secrets, "bastion01.lab.example.com")  # must not raise
    assert get_bastion_password(secrets, "bastion01.lab.example.com") is None
