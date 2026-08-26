"""`clusterbuild catalog` -- refresh/inspect the doc-grounded catalog.

Distribution model (see plan "Suggested initial repo layout" +
"Distribution/update mechanism"): the catalog ships bundled inside the
package, but a team can also point ClusterBuild at a shared git repo so
catalog updates (new OCP versions, corrected fields) don't require a full
package reinstall. `catalog update` does a `git clone`/`git pull` of that
repo into `~/.clusterbuild/catalog`, which `catalog_loader.py` already
prefers over the bundled copy.
"""

from __future__ import annotations

import os
import subprocess
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from clusterbuild.core.catalog_loader import Catalog, CatalogError
from clusterbuild.core.config import user_catalog_override_dir
from clusterbuild.core.version_watch import check_versions

app = typer.Typer(help="Inspect and refresh the doc-grounded catalog.")
console = Console()
err_console = Console(stderr=True)

CATALOG_REPO_ENV_VAR = "CLUSTERBUILD_CATALOG_REPO"


@app.command("update")
def update(
    repo_url: Optional[str] = typer.Option(
        None, "--repo", help=f"Git URL to sync the catalog from (or set {CATALOG_REPO_ENV_VAR})"
    ),
) -> None:
    """Refresh the local catalog override from a shared git repo, if configured."""
    repo_url = repo_url or os.environ.get(CATALOG_REPO_ENV_VAR)
    override_dir = user_catalog_override_dir()

    if not repo_url:
        console.print(
            "[yellow]No catalog repo configured.[/yellow] Set --repo or "
            f"{CATALOG_REPO_ENV_VAR} to a git URL to enable `catalog update`.\n"
            "Until then, ClusterBuild uses the catalog bundled with the installed package "
            "(upgrade the package itself to get catalog changes)."
        )
        return

    if (override_dir / ".git").exists():
        console.print(f"Pulling latest catalog into {override_dir} ...")
        result = subprocess.run(["git", "-C", str(override_dir), "pull", "--ff-only"], capture_output=True, text=True)
    else:
        override_dir.parent.mkdir(parents=True, exist_ok=True)
        console.print(f"Cloning catalog repo into {override_dir} ...")
        result = subprocess.run(["git", "clone", repo_url, str(override_dir)], capture_output=True, text=True)

    if result.returncode != 0:
        err_console.print(f"[bold red]catalog update failed:[/bold red]\n{result.stderr}")
        raise typer.Exit(code=1)
    console.print("[green]Catalog updated.[/green]")
    console.print(result.stdout.strip())


@app.command("check-versions")
def check_versions_cmd() -> None:
    """Compare the local version_matrix.yaml against docs.redhat.com and openshift/installer branches."""
    catalog = Catalog()
    result = check_versions(catalog)

    console.print(f"Catalog default GA version: [bold]{result.latest_ga_in_catalog}[/bold]")
    console.print(f"Latest version advertised on docs.redhat.com: [bold]{result.latest_ga_on_docs or 'unknown'}[/bold]")
    if result.docs_ahead_of_catalog:
        console.print(
            "[yellow]docs.redhat.com is ahead of the catalog's default_ga_version -- "
            "add a version_matrix.yaml entry (and validate the schema_ref) for the new release.[/yellow]"
        )
    else:
        console.print("[green]Catalog default GA version is up to date with docs.redhat.com.[/green]")

    if result.branches_missing_from_catalog:
        console.print(
            "\n[yellow]openshift/installer release branches not yet in version_matrix.yaml "
            f"(candidates for a `preview` entry): {', '.join(result.branches_missing_from_catalog)}[/yellow]"
        )

    for err in result.errors:
        err_console.print(f"[dim]warning: {err}[/dim]")


@app.command("show")
def show(
    platform: str = typer.Option(..., "--platform"),
    method: str = typer.Option(..., "--method"),
    ocp_version: Optional[str] = typer.Option(None, "--ocp-version"),
) -> None:
    """Print the raw catalog entry (fields, docSource, provisioning steps) for review."""
    catalog = Catalog()
    ocp_version = ocp_version or catalog.default_ga_version()
    try:
        entry = catalog.load_entry(ocp_version, platform, method)
    except CatalogError as exc:
        err_console.print(f"[bold red]Catalog error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc

    table = Table(title=f"{entry.platform}/{entry.install_method} @ OCP {entry.ocp_version}")
    table.add_column("Field")
    table.add_column("Value")
    table.add_row("status", entry.status)
    table.add_row("version status", entry.version_status)
    table.add_row("doc_source", entry.doc_source)
    table.add_row("infra_provisioning_target", str(entry.infra_provisioning_target))
    table.add_row("manifests", ", ".join(m.get("filename", "?") for m in entry.manifests))
    console.print(table)
    if entry.is_preview:
        console.print("[bold yellow]PREVIEW entry -- not yet confirmed against docs.redhat.com.[/bold yellow]")
