"""Distribution: a per-user `~/.clusterbuild/environments/<id>.yaml` override
takes precedence over the bundled placeholder, so team members can fill in
their own lab's real values without editing the installed package (same
override pattern as `catalog update`'s override directory)."""

from __future__ import annotations

import pytest

from clusterbuild.core.config import user_environments_dir
from clusterbuild.core.installers.base import resolve_environment_profile_path


@pytest.fixture()
def _fake_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CLUSTERBUILD_HOME", str(tmp_path))


def test_falls_back_to_bundled_profile_when_no_override_exists(_fake_env):
    path = resolve_environment_profile_path("vsphere-pnq2")
    assert path.exists()
    assert "environments" in str(path)
    assert str(user_environments_dir()) not in str(path)


def test_user_override_takes_precedence_over_bundled(_fake_env):
    override_dir = user_environments_dir()
    override_path = override_dir / "aws-lab.yaml"
    override_path.write_text("id: aws-lab\nplatform: aws\nregion: us-east-2\narchitecture: x86_64\n")

    resolved = resolve_environment_profile_path("aws-lab")
    assert resolved == override_path
    assert "us-east-2" in resolved.read_text()


def test_raises_with_helpful_message_when_profile_unknown(_fake_env):
    with pytest.raises(RuntimeError, match="Unknown environment profile"):
        resolve_environment_profile_path("does-not-exist")


def test_returns_none_when_no_profile_id_given(_fake_env):
    assert resolve_environment_profile_path(None) is None
