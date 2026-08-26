"""`clusterbuild bastion` -- register and verify the RHEL bastion (Phase 1)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from clusterbuild.core import audit
from clusterbuild.core.bastion_exec import REQUIRED_TOOLS, BastionError, BastionExecutor
from clusterbuild.core.state import Bastion, get_session

app = typer.Typer(help="Register and verify RHEL bastions used as install hosts.")
console = Console()
err_console = Console(stderr=True)


@app.command("register")
def register(
    host: str = typer.Option(..., "--host", help="Bastion hostname or IP"),
    user: str = typer.Option(..., "--user", help="SSH username"),
    port: int = typer.Option(22, "--port"),
    key_filename: Optional[str] = typer.Option(None, "--key-file", help="Path to SSH private key (default: ssh-agent/default keys)"),
    install_dir: Optional[str] = typer.Option(None, "--install-dir", help="Base install directory on the bastion"),
) -> None:
    """Register a bastion and verify SSH connectivity + required tooling."""
    executor = BastionExecutor(host, user, port=port, key_filename=key_filename)
    try:
        executor.connect()
    except BastionError as exc:
        err_console.print(f"[bold red]Could not connect:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc

    try:
        verified = executor.verify_tools()
        missing = [tool for tool in REQUIRED_TOOLS if not verified.get(tool)]
        install_dir = install_dir or f"/home/{user}/clusterbuild-installs"
        executor.ensure_dir(install_dir)
    finally:
        executor.close()

    session = get_session()
    try:
        existing = session.query(Bastion).filter_by(host=host).one_or_none()
        if existing:
            existing.ssh_user = user
            existing.ssh_port = port
            existing.install_dir = install_dir
            existing.verified_tools = json.dumps(verified)
            existing.last_verified_at = datetime.now(timezone.utc)
        else:
            session.add(
                Bastion(
                    host=host,
                    ssh_user=user,
                    ssh_port=port,
                    install_dir=install_dir,
                    verified_tools=json.dumps(verified),
                    last_verified_at=datetime.now(timezone.utc),
                )
            )
        session.commit()
    finally:
        session.close()

    audit.record("bastion.register", detail=f"host={host} missing_tools={missing}")

    for tool, version in verified.items():
        mark = f"[green]{version}[/green]" if version else "[red]missing[/red]"
        console.print(f"  {tool}: {mark}")
    if missing:
        console.print(
            f"\n[yellow]Missing required tools on {host}: {', '.join(missing)}. "
            "Install them before running any install/provisioning commands against this bastion.[/yellow]"
        )
    else:
        console.print(f"\n[green]Bastion {host} registered -- all required tools present.[/green]")


@app.command("list")
def list_bastions() -> None:
    session = get_session()
    try:
        bastions = session.query(Bastion).all()
    finally:
        session.close()

    table = Table(title="Registered bastions")
    table.add_column("Host")
    table.add_column("User")
    table.add_column("Install dir")
    table.add_column("Last verified")
    for b in bastions:
        table.add_row(b.host, b.ssh_user, b.install_dir, str(b.last_verified_at or "never"))
    console.print(table)


@app.command("verify")
def verify(host: str = typer.Option(..., "--host")) -> None:
    """Re-run tool verification against an already-registered bastion."""
    session = get_session()
    try:
        bastion = session.query(Bastion).filter_by(host=host).one_or_none()
    finally:
        session.close()
    if bastion is None:
        err_console.print(f"[bold red]No such bastion registered:[/bold red] {host}")
        raise typer.Exit(code=1)

    executor = BastionExecutor(bastion.host, bastion.ssh_user, port=bastion.ssh_port)
    try:
        executor.connect()
        verified = executor.verify_tools()
    except BastionError as exc:
        err_console.print(f"[bold red]Could not connect:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
    finally:
        executor.close()

    session = get_session()
    try:
        b = session.query(Bastion).filter_by(host=host).one()
        b.verified_tools = json.dumps(verified)
        b.last_verified_at = datetime.now(timezone.utc)
        session.commit()
    finally:
        session.close()

    for tool, version in verified.items():
        mark = f"[green]{version}[/green]" if version else "[red]missing[/red]"
        console.print(f"  {tool}: {mark}")
