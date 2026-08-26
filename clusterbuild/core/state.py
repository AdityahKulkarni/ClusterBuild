"""Local SQLite state (Phase 1) -- see plan "Local state model (SQLite, high level)".

Not shared across the team; this is this user's own record of bastions,
clusters, generated manifests/backups, and job history. SQLAlchemy is used
purely as a lightweight local ORM -- there is no database server.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Optional

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker

from clusterbuild.core.config import get_paths


class Base(DeclarativeBase):
    pass


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class Bastion(Base):
    __tablename__ = "bastions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    host: Mapped[str] = mapped_column(String, unique=True)
    ssh_user: Mapped[str] = mapped_column(String)
    ssh_port: Mapped[int] = mapped_column(Integer, default=22)
    install_dir: Mapped[str] = mapped_column(String, default="/home/{ssh_user}/clusterbuild-installs")
    verified_tools: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # JSON blob
    registered_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_utcnow)
    last_verified_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)


class Cluster(Base):
    __tablename__ = "clusters"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String, unique=True)
    base_domain: Mapped[str] = mapped_column(String)
    ocp_version: Mapped[str] = mapped_column(String)
    install_config_platform: Mapped[str] = mapped_column(String)  # vsphere/nutanix/aws/azure/gcp/none
    infra_provisioning_target: Mapped[str] = mapped_column(String)
    install_method: Mapped[str] = mapped_column(String)  # ipi/upi/agent/assisted/hcp
    network_mode: Mapped[str] = mapped_column(String, default="dhcp")  # dhcp/static
    bastion_id: Mapped[Optional[int]] = mapped_column(ForeignKey("bastions.id"), nullable=True)
    status: Mapped[str] = mapped_column(String, default="created")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_utcnow)

    bastion: Mapped[Optional["Bastion"]] = relationship()
    configs: Mapped[list["ClusterConfig"]] = relationship(back_populates="cluster")
    jobs: Mapped[list["Job"]] = relationship(back_populates="cluster")


class ClusterConfig(Base):
    __tablename__ = "cluster_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cluster_id: Mapped[int] = mapped_column(ForeignKey("clusters.id"))
    filename: Mapped[str] = mapped_column(String)  # install-config.yaml / agent-config.yaml
    backup_path: Mapped[str] = mapped_column(String)
    checksum_sha256: Mapped[str] = mapped_column(String)
    catalog_ocp_version: Mapped[str] = mapped_column(String)
    catalog_schema_ref: Mapped[str] = mapped_column(String)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_utcnow)

    cluster: Mapped["Cluster"] = relationship(back_populates="configs")


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String, primary_key=True)  # uuid4 hex
    cluster_id: Mapped[int] = mapped_column(ForeignKey("clusters.id"))
    phase: Mapped[str] = mapped_column(String)  # e.g. "create_ignition_configs", "wait_bootstrap_complete"
    status: Mapped[str] = mapped_column(String, default="running")  # running/succeeded/failed
    pid: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    log_path: Mapped[str] = mapped_column(String)
    started_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_utcnow)
    finished_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)

    cluster: Mapped["Cluster"] = relationship(back_populates="jobs")


class AuditLogEntry(Base):
    __tablename__ = "local_audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    at: Mapped[dt.datetime] = mapped_column(DateTime, default=_utcnow)
    action: Mapped[str] = mapped_column(String)
    detail: Mapped[str] = mapped_column(Text, default="")


_engine_cache: dict[str, object] = {}


def _get_engine():
    """Cache one engine per resolved db_path so repeated calls within a
    single process (or a single CLUSTERBUILD_HOME) reuse it, but a changed
    CLUSTERBUILD_HOME (as tests do per-test) gets its own engine instead of
    silently reusing a stale one."""
    db_path = str(get_paths().db_path)
    engine = _engine_cache.get(db_path)
    if engine is None:
        engine = create_engine(f"sqlite:///{db_path}", future=True)
        Base.metadata.create_all(engine)
        # Defense-in-depth: state.db tracks bastion hostnames/install
        # directories/backup paths -- lock it to owner-only even though the
        # parent ~/.clusterbuild directory is already 0700, in case that
        # directory's mode is ever loosened later (e.g. copied to a shared
        # mount for debugging).
        try:
            Path(db_path).chmod(0o600)
        except OSError:
            pass
        _engine_cache[db_path] = engine
    return engine


def get_session() -> Session:
    return sessionmaker(bind=_get_engine(), expire_on_commit=False, future=True)()
