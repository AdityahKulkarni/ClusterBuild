"""`clusterbuild credentials` -- per-platform secrets in the OS keyring (Phase 1)."""

from __future__ import annotations

from typing import Optional

import questionary
import typer
from rich.console import Console

from clusterbuild.core import audit
from clusterbuild.core.secrets import SecretsBackend, VaultConfig

app = typer.Typer(help="Store/retrieve per-platform credentials (OS keyring by default, optional Vault).")
console = Console()
err_console = Console(stderr=True)

KNOWN_PLATFORMS = ["vsphere", "nutanix", "aws", "azure", "gcp", "assisted_saas"]


def _backend(vault_address: Optional[str], vault_token: Optional[str]) -> SecretsBackend:
    if vault_address and vault_token:
        return SecretsBackend(VaultConfig(address=vault_address, token=vault_token))
    return SecretsBackend()


@app.command("set")
def set_credentials(
    platform: str = typer.Option(..., "--platform", help="e.g. vsphere, nutanix, aws, azure, gcp"),
    backend: str = typer.Option("keyring", "--backend", help="keyring (default) or vault"),
    vault_address: Optional[str] = typer.Option(None, "--vault-address"),
    vault_token: Optional[str] = typer.Option(None, "--vault-token"),
) -> None:
    """Interactively prompt for and store this platform's credentials."""
    secrets = _backend(vault_address, vault_token)

    if platform == "vsphere":
        username = questionary.text("vCenter username (e.g. administrator@vsphere.local):").ask()
        password = questionary.password("vCenter password:").ask()
        secrets.set(platform, "username", username, backend=backend)
        secrets.set(platform, "password", password, backend=backend)
    elif platform == "assisted_saas":
        offline_token = questionary.password("Red Hat offline token (from console.redhat.com/openshift/token):").ask()
        secrets.set(platform, "offline_token", offline_token, backend=backend)
    elif platform in ("aws", "azure", "gcp", "nutanix"):
        for key, prompt in _cloud_fields(platform):
            value = questionary.password(prompt).ask() if "secret" in key or "key" in key else questionary.text(prompt).ask()
            secrets.set(platform, key, value, backend=backend)
    else:
        err_console.print(f"[bold red]Unknown platform[/bold red] {platform!r}. Known: {', '.join(KNOWN_PLATFORMS)}")
        raise typer.Exit(code=1)

    pull_secret_wanted = questionary.confirm("Also store your Red Hat pull secret now?", default=True).ask()
    if pull_secret_wanted:
        pull_secret = questionary.password("Paste pull secret JSON (from console.redhat.com/openshift/install/pull-secret):").ask()
        secrets.set(platform, "pull_secret", pull_secret, backend=backend)

    audit.record("credentials.set", detail=f"platform={platform} backend={backend}")
    console.print(f"[green]Stored credentials for {platform} in {backend}.[/green]")


def _cloud_fields(platform: str) -> list[tuple[str, str]]:
    return {
        "nutanix": [("prism_central_username", "Prism Central username:"), ("prism_central_password", "Prism Central password:")],
        "aws": [("access_key_id", "AWS access key ID:"), ("secret_access_key", "AWS secret access key:")],
        "azure": [("client_id", "Azure client ID:"), ("client_secret", "Azure client secret:"), ("tenant_id", "Azure tenant ID:"), ("subscription_id", "Azure subscription ID:")],
        "gcp": [("service_account_json", "Paste GCP service-account JSON:")],
    }[platform]


@app.command("check")
def check_credentials(
    platform: str = typer.Option(..., "--platform"),
    backend: str = typer.Option("keyring", "--backend"),
) -> None:
    """Confirm whether credentials are present, without printing the secret values."""
    secrets = _backend(None, None)
    keys = {
        "vsphere": ["username", "password"],
        "nutanix": ["prism_central_username", "prism_central_password"],
        "aws": ["access_key_id", "secret_access_key"],
        "azure": ["client_id", "client_secret", "tenant_id", "subscription_id"],
        "gcp": ["service_account_json"],
        "assisted_saas": ["offline_token"],
    }.get(platform, [])
    if not keys:
        err_console.print(f"[bold red]Unknown platform[/bold red] {platform!r}")
        raise typer.Exit(code=1)
    for key in keys:
        present = secrets.get(platform, key, backend=backend) is not None
        mark = "[green]present[/green]" if present else "[red]missing[/red]"
        console.print(f"  {platform}.{key}: {mark}")


@app.command("delete")
def delete_credentials(
    platform: str = typer.Option(..., "--platform"),
) -> None:
    """Remove stored keyring credentials for a platform."""
    secrets = _backend(None, None)
    for key in ("username", "password", "pull_secret"):
        secrets.delete(platform, key)
    audit.record("credentials.delete", detail=f"platform={platform}")
    console.print(f"[green]Deleted keyring credentials for {platform} (if present).[/green]")
