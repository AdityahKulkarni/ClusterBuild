"""Render the networking/DNS/load-balancer prerequisite checklist (Phase 0).

Pure function of the Catalog data -- no network calls, no credentials
needed. This is intentionally the very first thing ClusterBuild can do,
since it delivers value even before Vault/keyring/bastion/driver code exists.
"""

from __future__ import annotations

from clusterbuild.core.catalog_loader import Catalog, CatalogEntry


def _render_dns(networking: dict) -> list[str]:
    lines = ["### DNS records", ""]
    records = networking.get("required_dns", [])
    if not records:
        lines.append("_None specified._")
        return lines
    for rec in records:
        lines.append(f"- `{rec.get('record')}` -> {rec.get('points_to')} ({rec.get('scope')})")
    return lines


def _render_lb(networking: dict) -> list[str]:
    lb = networking.get("load_balancer", {})
    lines = ["", "### Load balancer", ""]
    if not lb.get("required"):
        lines.append(f"Not required. {lb.get('note', '')}".strip())
        return lines
    lines.append(f"**Required.** {lb.get('note', '')}".strip())
    lines.append("")
    for pool in lb.get("pools", []):
        lines.append(f"- Port `{pool.get('port')}` -> {pool.get('backends')} -- {pool.get('purpose')}")
    return lines


def _render_ntp_firewall(networking: dict) -> list[str]:
    ntp = networking.get("ntp", {})
    lines = ["", "### NTP", ""]
    lines.append("Required." if ntp.get("required") else "Optional.")
    if ntp.get("note"):
        lines.append(ntp["note"])
    lines += ["", "### Firewall ports", ""]
    for port in networking.get("firewall_ports", []):
        lines.append(f"- {port}")
    return lines


def _render_management_cluster_prerequisites(networking: dict) -> list[str]:
    items = networking.get("management_cluster_prerequisites")
    if not items:
        return []
    lines = ["", "### Management cluster prerequisites (HCP only)", ""]
    for item in items:
        lines.append(f"- {item}")
    return lines


def _render_capability_notes(networking: dict) -> list[str]:
    notes = networking.get("capability_notes")
    if not notes:
        return []
    lines = ["", "### Capability trade-offs", ""]
    for note in notes:
        lines.append(f"- {note}")
    return lines


def _render_general(general: dict | None) -> list[str]:
    if not general:
        return []
    lines = ["", "## vCenter privileges", ""]
    priv = general.get("vcenter_privileges", {})
    if priv.get("note"):
        lines.append(priv["note"].strip())
        lines.append("")
    for scope in ("vcenter_global", "cluster", "datastore", "port_group", "vm_folder"):
        items = priv.get(scope)
        if not items:
            continue
        lines.append(f"**{scope.replace('_', ' ').title()}**")
        for item in items:
            lines.append(f"- {item}")
        lines.append("")
    reminders = general.get("general_reminders")
    if reminders:
        lines.append("## General reminders")
        lines.append("")
        for r in reminders:
            lines.append(f"- {r}")
    return lines


def render_markdown(entry: CatalogEntry, general: dict | None = None) -> str:
    lines = [
        f"# Prerequisite checklist: {entry.platform} / {entry.install_method} (OCP {entry.ocp_version})",
        "",
    ]
    if entry.is_preview:
        lines += [
            "> **PREVIEW**: this OCP version/platform/method combination is sourced from "
            "github.com/openshift, not yet from docs.redhat.com. Re-verify before relying on it. "
            f"{entry.raw.get('preview_note', '')}".strip(),
            "",
        ]
    lines.append(entry.description or "")
    lines.append("")
    lines.append(f"Doc source: {entry.doc_source}")
    lines.append("")
    lines += _render_management_cluster_prerequisites(entry.networking)
    lines += _render_dns(entry.networking)
    lines += _render_lb(entry.networking)
    lines += _render_ntp_firewall(entry.networking)
    lines += _render_capability_notes(entry.networking)
    lines += _render_general(general)
    return "\n".join(lines) + "\n"


def generate_checklist(
    catalog: Catalog,
    *,
    platform: str,
    install_method: str,
    ocp_version: str | None = None,
) -> str:
    ocp_version = ocp_version or catalog.default_ga_version()
    entry = catalog.load_entry(ocp_version, platform, install_method)
    general = catalog.general_checklist(platform)
    return render_markdown(entry, general)
