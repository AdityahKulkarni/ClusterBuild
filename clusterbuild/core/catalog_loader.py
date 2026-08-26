"""Doc-grounded Catalog loader (Phase 0 / ocp5-readiness).

Loads the versioned Field & Prerequisite Catalog described in the plan:
every selectable field/checklist item is data (YAML) sourced from
docs.redhat.com or github.com/openshift, never invented at runtime. This
module only *reads* that data -- see manifest_builder.py for turning it into
actual install-config.yaml/agent-config.yaml content.

Version resolution: `version_matrix.yaml` maps each OCP version to a
`schema_ref` (the catalog directory whose field definitions actually apply)
and a `status` of `ga` or `preview`. Adding support for a new OCP release is
therefore a matter of adding one entry to that file -- see
docs/adr/0002-deterministic-catalog-over-llm-generation.md.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from clusterbuild.core.config import bundled_catalog_dir, user_catalog_override_dir


class CatalogError(RuntimeError):
    """Raised when a catalog entry is missing, malformed, or unknown."""


@dataclass(frozen=True)
class VersionInfo:
    ocp_version: str
    status: str  # "ga" | "preview"
    doc_base: str | None
    schema_ref: str
    note: str
    github_source: str | None = None

    @property
    def is_preview(self) -> bool:
        return self.status == "preview"


@dataclass(frozen=True)
class CatalogEntry:
    platform: str
    install_method: str
    ocp_version: str
    schema_ref: str
    version_status: str
    status: str
    doc_source: str
    description: str
    manifests: list[dict[str, Any]] = field(default_factory=list)
    networking: dict[str, Any] = field(default_factory=dict)
    provisioning: dict[str, Any] = field(default_factory=dict)
    infra_provisioning_target: str | None = None
    backend_options: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_preview(self) -> bool:
        return self.version_status == "preview" or self.status == "preview"


def _catalog_roots() -> list[Path]:
    """User overrides win over the bundled catalog (see `catalog update`)."""
    roots = []
    override = user_catalog_override_dir()
    if override.exists():
        roots.append(override)
    roots.append(bundled_catalog_dir())
    return roots


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


class Catalog:
    def __init__(self) -> None:
        self._version_matrix: dict[str, Any] | None = None

    # -- version matrix -----------------------------------------------
    def _version_matrix_data(self) -> dict[str, Any]:
        if self._version_matrix is None:
            last_err: Exception | None = None
            for root in _catalog_roots():
                vm_path = root / "version_matrix.yaml"
                if vm_path.exists():
                    self._version_matrix = _load_yaml(vm_path)
                    return self._version_matrix
            raise CatalogError("version_matrix.yaml not found in any catalog root") from last_err
        return self._version_matrix

    def default_ga_version(self) -> str:
        return str(self._version_matrix_data().get("default_ga_version", "4.18"))

    def minimum_supported_version(self) -> str:
        return str(self._version_matrix_data().get("minimum_supported_version", "4.18"))

    def known_versions(self) -> list[str]:
        return list(self._version_matrix_data().get("versions", {}).keys())

    def resolve_version(self, ocp_version: str) -> VersionInfo:
        versions = self._version_matrix_data().get("versions", {})
        entry = versions.get(ocp_version)
        if entry is None:
            known = ", ".join(sorted(versions.keys()))
            raise CatalogError(
                f"OCP version {ocp_version!r} is not in the catalog's version_matrix.yaml. "
                f"Known versions: {known}. Run `clusterbuild catalog check-versions` or add an "
                f"entry (see docs/adr/0002) before using this version."
            )
        return VersionInfo(
            ocp_version=ocp_version,
            status=entry.get("status", "preview"),
            doc_base=entry.get("doc_base"),
            schema_ref=str(entry.get("schema_ref", ocp_version)),
            note=entry.get("note", ""),
            github_source=entry.get("github_source"),
        )

    # -- catalog entries ------------------------------------------------
    def _find_entry_file(self, schema_ref: str, platform: str, install_method: str) -> Path:
        rel = Path(schema_ref) / platform / f"{install_method}.yaml"
        for root in _catalog_roots():
            candidate = root / rel
            if candidate.exists():
                return candidate
        raise CatalogError(
            f"No catalog entry for platform={platform!r} install_method={install_method!r} "
            f"schema_ref={schema_ref!r} (looked in {[str(r) for r in _catalog_roots()]})"
        )

    def load_entry(self, ocp_version: str, platform: str, install_method: str) -> CatalogEntry:
        version_info = self.resolve_version(ocp_version)
        path = self._find_entry_file(version_info.schema_ref, platform, install_method)
        raw = _load_yaml(path)
        return CatalogEntry(
            platform=raw.get("platform", platform),
            install_method=raw.get("install_method", install_method),
            ocp_version=ocp_version,
            schema_ref=version_info.schema_ref,
            version_status=version_info.status,
            status=raw.get("status", "ga"),
            doc_source=raw.get("doc_source", ""),
            description=str(raw.get("description", "")).strip(),
            manifests=raw.get("manifests", []),
            networking=raw.get("networking", {}),
            provisioning=raw.get("provisioning", {}),
            infra_provisioning_target=raw.get("infra_provisioning_target", raw.get("platform", platform)),
            backend_options=raw.get("backend_options", []),
            raw=raw,
        )

    def general_checklist(self, platform: str) -> dict[str, Any] | None:
        for root in _catalog_roots():
            candidate = root / "checklists" / f"{platform}_general.yaml"
            if candidate.exists():
                return _load_yaml(candidate)
        return None

    def available_methods(self, schema_ref: str, platform: str) -> list[str]:
        methods = []
        for root in _catalog_roots():
            d = root / schema_ref / platform
            if d.exists():
                methods.extend(p.stem for p in d.glob("*.yaml"))
        return sorted(set(methods))

    def available_platforms(self, schema_ref: str) -> list[str]:
        platforms = []
        for root in _catalog_roots():
            d = root / schema_ref
            if d.exists():
                platforms.extend(p.name for p in d.iterdir() if p.is_dir())
        return sorted(set(platforms))


def get_catalog() -> Catalog:
    return Catalog()
