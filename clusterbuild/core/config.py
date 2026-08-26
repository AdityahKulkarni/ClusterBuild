"""Local runtime paths and config-dir bootstrap (Phase 1 foundation).

Everything ClusterBuild writes for a given user lives under ``~/.clusterbuild``.
No server, no shared database -- this directory is the entire "install"
footprint on a team member's machine. Directories that may contain generated
manifests or credentials-adjacent metadata are created with owner-only
permissions (0700 / 0600) since install-config.yaml/agent-config.yaml backups
can contain sensitive cluster topology data.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

APP_DIR_ENV_VAR = "CLUSTERBUILD_HOME"
_OWNER_ONLY_DIR = stat.S_IRWXU  # 0700
_OWNER_ONLY_FILE = stat.S_IRUSR | stat.S_IWUSR  # 0600


def _chmod_owner_only(path: Path, *, is_dir: bool) -> None:
    try:
        path.chmod(_OWNER_ONLY_DIR if is_dir else _OWNER_ONLY_FILE)
    except OSError:
        # Best-effort: some filesystems (e.g. some CI sandboxes) don't
        # support chmod bits; never fail startup over this.
        pass


@dataclass(frozen=True)
class Paths:
    home: Path
    db_path: Path
    jobs_dir: Path
    backups_dir: Path
    logs_dir: Path
    config_file: Path
    environments_dir: Path

    def ensure(self) -> "Paths":
        for d in (self.home, self.jobs_dir, self.backups_dir, self.logs_dir, self.environments_dir):
            d.mkdir(parents=True, exist_ok=True)
            _chmod_owner_only(d, is_dir=True)
        return self


def get_paths() -> Paths:
    home = Path(os.environ.get(APP_DIR_ENV_VAR, "~/.clusterbuild")).expanduser()
    paths = Paths(
        home=home,
        db_path=home / "state.db",
        jobs_dir=home / "jobs",
        backups_dir=home / "backups",
        logs_dir=home / "logs",
        config_file=home / "config.toml",
        environments_dir=home / "environments",
    )
    return paths.ensure()


def bundled_catalog_dir() -> Path:
    """Directory of the catalog shipped inside the installed package."""
    return Path(str(resources.files("clusterbuild") / "catalog"))


def bundled_environments_dir() -> Path:
    """Directory of the environment profiles shipped inside the package."""
    return Path(str(resources.files("clusterbuild") / "environments"))


def user_catalog_override_dir() -> Path:
    """Optional per-user catalog override/refresh location.

    `clusterbuild catalog update` writes here; if present, entries here take
    precedence over the bundled catalog so a `git pull`-based refresh doesn't
    require reinstalling the package.
    """
    return get_paths().home / "catalog"


def user_environments_dir() -> Path:
    """Per-user environment profile overrides (`~/.clusterbuild/environments`).

    The package ships placeholder profiles (e.g. `nutanix-lab.yaml`,
    `aws-lab.yaml`) that mostly need real per-team-member/per-lab values
    filled in. Rather than editing the installed package in place (fragile,
    and lost on every upgrade), drop a same-named YAML file here -- it takes
    precedence over the bundled copy, same pattern as `catalog update`'s
    override directory."""
    return get_paths().environments_dir


def job_dir(job_id: str) -> Path:
    d = get_paths().jobs_dir / job_id
    d.mkdir(parents=True, exist_ok=True)
    _chmod_owner_only(d, is_dir=True)
    return d


def backup_path_for(cluster_name: str, filename: str, timestamp: str) -> Path:
    # `filename` may itself contain a subdirectory (e.g. "auth/kubeconfig"),
    # so create all the way down to its parent, not just the timestamp dir.
    base = get_paths().backups_dir / cluster_name / timestamp
    target = base / filename
    target.parent.mkdir(parents=True, exist_ok=True)
    _chmod_owner_only(base, is_dir=True)
    _chmod_owner_only(target.parent, is_dir=True)
    return target
