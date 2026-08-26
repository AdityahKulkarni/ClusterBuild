"""`clusterbuild checklist` -- Phase 0 deliverable.

No credentials, no bastion, no network access required: prints the
DNS/load-balancer/NTP/firewall prerequisite checklist for a given
platform + install method + OCP version, grounded entirely in the Catalog.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer
from rich.console import Console

from clusterbuild.core.catalog_loader import Catalog, CatalogError
from clusterbuild.core.checklist import generate_checklist

app = typer.Typer(help="Generate networking/DNS/load-balancer prerequisite checklists.")
console = Console()
err_console = Console(stderr=True)


@app.command("generate")
def generate(
    platform: str = typer.Option(..., "--platform", help="e.g. vsphere, none, nutanix, aws, azure, gcp"),
    method: str = typer.Option(..., "--method", help="ipi, upi, agent, assisted, hcp"),
    ocp_version: Optional[str] = typer.Option(None, "--ocp-version", help="e.g. 4.18 (defaults to current GA)"),
    output: Optional[Path] = typer.Option(None, "--output", "-o", help="Write to this file instead of stdout"),
) -> None:
    """Generate the prerequisite checklist for PLATFORM/METHOD."""
    catalog = Catalog()
    try:
        markdown = generate_checklist(catalog, platform=platform, install_method=method, ocp_version=ocp_version)
    except CatalogError as exc:
        err_console.print(f"[bold red]Catalog error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc

    if output:
        output.write_text(markdown, encoding="utf-8")
        console.print(f"[green]Checklist written to[/green] {output}")
    else:
        console.print(markdown)


@app.command("list")
def list_combinations(
    ocp_version: Optional[str] = typer.Option(None, "--ocp-version"),
) -> None:
    """List platform/method combinations available in the catalog for an OCP version."""
    catalog = Catalog()
    ocp_version = ocp_version or catalog.default_ga_version()
    try:
        version_info = catalog.resolve_version(ocp_version)
    except CatalogError as exc:
        err_console.print(f"[bold red]Catalog error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc

    console.print(f"OCP {ocp_version} (status: {version_info.status}, schema: {version_info.schema_ref})")
    for platform in catalog.available_platforms(version_info.schema_ref):
        methods = catalog.available_methods(version_info.schema_ref, platform)
        console.print(f"  [bold]{platform}[/bold]: {', '.join(methods)}")
