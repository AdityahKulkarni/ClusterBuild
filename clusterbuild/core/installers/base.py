"""Shared plumbing for install-method job handlers (ipi/upi/agent/assisted/hcp).

A job handler's stdout/stderr is already redirected to the job's log file by
`jobs.start_job`, so these helpers just `print()` -- that's what
`cluster logs --follow` tails.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from clusterbuild.core.bastion_exec import BastionExecutor
from clusterbuild.core.catalog_loader import Catalog, CatalogEntry
from clusterbuild.core.config import backup_path_for, bundled_environments_dir, user_environments_dir
from clusterbuild.core.manifest_builder import build_manifests
from clusterbuild.core.secrets import SecretsBackend
from clusterbuild.core.state import Bastion, Cluster, ClusterConfig, get_session


def log(message: str) -> None:
    print(message, flush=True)


def load_cluster(cluster_id: int) -> Cluster:
    session = get_session()
    try:
        cluster = session.get(Cluster, cluster_id)
        if cluster is None:
            raise RuntimeError(f"No such cluster id={cluster_id}")
        session.refresh(cluster)
        return cluster
    finally:
        session.close()


def load_bastion(bastion_id: int) -> Bastion:
    session = get_session()
    try:
        bastion = session.get(Bastion, bastion_id)
        if bastion is None:
            raise RuntimeError(f"No such bastion id={bastion_id}")
        return bastion
    finally:
        session.close()


def set_cluster_status(cluster_id: int, status: str) -> None:
    session = get_session()
    try:
        cluster = session.get(Cluster, cluster_id)
        if cluster:
            cluster.status = status
            session.commit()
    finally:
        session.close()


def resolve_environment_profile_path(profile_id: str | None) -> Path | None:
    if not profile_id:
        return None
    override_path = user_environments_dir() / f"{profile_id}.yaml"
    if override_path.exists():
        return override_path
    path = bundled_environments_dir() / f"{profile_id}.yaml"
    if not path.exists():
        raise RuntimeError(
            f"Unknown environment profile {profile_id!r} (looked in {override_path} and {path}). "
            f"Copy the bundled placeholder into {user_environments_dir()} and fill in your lab's real values."
        )
    return path


def build_and_stage_manifests(
    *,
    entry: CatalogEntry,
    environment_profile: str | None,
    answers: dict[str, Any],
    executor: BastionExecutor,
    remote_install_dir: str,
    cluster_id: int,
) -> list[Path]:
    """Build manifests locally, write them to the bastion, then immediately back them
    up (explicit plan requirement) and record the backup in local state."""
    secrets = SecretsBackend()
    env_profile_path = resolve_environment_profile_path(environment_profile)
    results = build_manifests(
        entry,
        environment_profile_path=env_profile_path,
        answers=answers,
        secrets=secrets,
        keyring_namespace=entry.platform,
    )

    executor.ensure_dir(remote_install_dir)
    local_paths = []
    session = get_session()
    try:
        for result in results:
            remote_path = f"{remote_install_dir}/{result.filename}"
            log(f"Writing {remote_path} ...")
            executor.write_file(remote_path, result.content_yaml)

            local_backup, checksum = executor.backup_manifest(
                cluster_name=str(cluster_id), remote_install_dir=remote_install_dir, filename=result.filename
            )
            local_paths.append(local_backup)
            log(f"Backed up {result.filename} -> {local_backup} (sha256={checksum[:12]}...)")

            session.add(
                ClusterConfig(
                    cluster_id=cluster_id,
                    filename=result.filename,
                    backup_path=str(local_backup),
                    checksum_sha256=checksum,
                    catalog_ocp_version=entry.ocp_version,
                    catalog_schema_ref=entry.schema_ref,
                )
            )
        session.commit()
    finally:
        session.close()
    return local_paths


def record_local_backup(
    *, cluster_id: int, cluster_name: str, filename: str, content: bytes, entry: CatalogEntry
) -> Path:
    """Like `build_and_stage_manifests`'s backup step, but for content that
    never lives on the bastion's filesystem in the first place (e.g. the
    Assisted Installer API request/response JSON) -- still backed up and
    recorded in local state, just without an SSH round-trip."""
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    local_path = backup_path_for(cluster_name, filename, timestamp)
    local_path.write_bytes(content)
    checksum = hashlib.sha256(content).hexdigest()

    session = get_session()
    try:
        session.add(
            ClusterConfig(
                cluster_id=cluster_id,
                filename=filename,
                backup_path=str(local_path),
                checksum_sha256=checksum,
                catalog_ocp_version=entry.ocp_version,
                catalog_schema_ref=entry.schema_ref,
            )
        )
        session.commit()
    finally:
        session.close()
    return local_path


def run_remote_streaming(executor: BastionExecutor, command: str) -> int:
    log(f"$ {command}")
    with executor.run_streaming(command) as stdout:
        for line in stdout:
            log(line.rstrip("\n"))
        return stdout.channel.recv_exit_status()


def get_catalog_entry_for_job(params: dict[str, Any]) -> CatalogEntry:
    catalog = Catalog()
    return catalog.load_entry(params["ocp_version"], params["platform"], params["install_method"])
