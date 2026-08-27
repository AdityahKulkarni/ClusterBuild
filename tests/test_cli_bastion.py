"""CLI-level coverage for `clusterbuild bastion register`'s SSH password
flags (--password/--ask-password/--clear-password): resolving, storing,
clearing, and threading the password into the connect() call, without
touching a real SSH connection or the real OS keyring."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from clusterbuild.cli import bastion
from clusterbuild.core.bastion_exec import REQUIRED_TOOLS
from clusterbuild.core.state import Bastion, get_session

runner = CliRunner()


class FakeBastionExecutor:
    instances: list["FakeBastionExecutor"] = []

    def __init__(self, host, user, port=22, key_filename=None):
        self.host, self.user, self.port = host, user, port
        self.connect_password = None
        FakeBastionExecutor.instances.append(self)

    def connect(self, password=None):
        self.connect_password = password

    def close(self):
        pass

    def ensure_dir(self, path):
        pass

    def verify_tools(self):
        return {tool: "1.0" for tool in REQUIRED_TOOLS}


class _FakeSecretsBackend:
    _store: dict[tuple[str, str], str] = {}

    def get(self, namespace, key, *, backend="keyring"):
        return self._store.get((namespace, key))

    def set(self, namespace, key, value, *, backend="keyring"):
        self._store[(namespace, key)] = value

    def delete(self, namespace, key, *, backend="keyring"):
        self._store.pop((namespace, key), None)


@pytest.fixture()
def _fake_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CLUSTERBUILD_HOME", str(tmp_path))
    monkeypatch.setattr(bastion, "BastionExecutor", FakeBastionExecutor)
    monkeypatch.setattr(bastion, "SecretsBackend", _FakeSecretsBackend)
    FakeBastionExecutor.instances.clear()
    _FakeSecretsBackend._store.clear()


def test_register_with_password_flag_stores_and_connects_with_it(_fake_env):
    result = runner.invoke(
        bastion.app,
        ["register", "--host", "bastion01.lab", "--user", "qe", "--password", "s3cret"],
    )

    assert result.exit_code == 0, result.output
    assert FakeBastionExecutor.instances[-1].connect_password == "s3cret"
    assert _FakeSecretsBackend._store[("bastion:bastion01.lab", "ssh_password")] == "s3cret"
    assert "stored in the OS keyring" in result.output


def test_register_with_ask_password_prompts_securely(_fake_env, monkeypatch):
    class _FakePrompt:
        def ask(self):
            return "prompted-secret"

    monkeypatch.setattr(bastion.questionary, "password", lambda *a, **k: _FakePrompt())

    result = runner.invoke(bastion.app, ["register", "--host", "bastion01.lab", "--user", "qe", "--ask-password"])

    assert result.exit_code == 0, result.output
    assert FakeBastionExecutor.instances[-1].connect_password == "prompted-secret"
    assert _FakeSecretsBackend._store[("bastion:bastion01.lab", "ssh_password")] == "prompted-secret"


def test_register_rejects_password_and_ask_password_together(_fake_env):
    result = runner.invoke(
        bastion.app,
        ["register", "--host", "bastion01.lab", "--user", "qe", "--password", "x", "--ask-password"],
    )

    assert result.exit_code == 1
    assert "only one of" in result.output
    assert FakeBastionExecutor.instances == []


def test_register_without_password_flags_falls_back_to_key_auth(_fake_env):
    result = runner.invoke(bastion.app, ["register", "--host", "bastion01.lab", "--user", "qe"])

    assert result.exit_code == 0, result.output
    assert FakeBastionExecutor.instances[-1].connect_password is None
    assert ("bastion:bastion01.lab", "ssh_password") not in _FakeSecretsBackend._store


def test_register_reuses_previously_stored_password_on_re_register(_fake_env):
    runner.invoke(bastion.app, ["register", "--host", "bastion01.lab", "--user", "qe", "--password", "s3cret"])
    FakeBastionExecutor.instances.clear()

    result = runner.invoke(bastion.app, ["register", "--host", "bastion01.lab", "--user", "qe"])

    assert result.exit_code == 0, result.output
    assert FakeBastionExecutor.instances[-1].connect_password == "s3cret"


def test_register_clear_password_removes_stored_password(_fake_env):
    runner.invoke(bastion.app, ["register", "--host", "bastion01.lab", "--user", "qe", "--password", "s3cret"])
    FakeBastionExecutor.instances.clear()

    result = runner.invoke(bastion.app, ["register", "--host", "bastion01.lab", "--user", "qe", "--clear-password"])

    assert result.exit_code == 0, result.output
    assert FakeBastionExecutor.instances[-1].connect_password is None
    assert ("bastion:bastion01.lab", "ssh_password") not in _FakeSecretsBackend._store
    assert "Cleared any stored SSH password" in result.output


def test_verify_resolves_stored_password(_fake_env):
    session = get_session()
    try:
        session.add(Bastion(host="bastion01.lab", ssh_user="qe", install_dir="/home/qe/clusterbuild-installs"))
        session.commit()
    finally:
        session.close()
    _FakeSecretsBackend._store[("bastion:bastion01.lab", "ssh_password")] = "s3cret"

    result = runner.invoke(bastion.app, ["verify", "--host", "bastion01.lab"])

    assert result.exit_code == 0, result.output
    assert FakeBastionExecutor.instances[-1].connect_password == "s3cret"
