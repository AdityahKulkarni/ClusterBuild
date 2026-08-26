"""`clusterbuild cluster` -- create/monitor/inspect clusters (Phases 2-8).

`create` dispatches to the job handler registered for the selected
install_method (see core/installers/*.py); everything else (logs/status/
list/kubeconfig) is method-agnostic since it only reads local state/log
files.
"""

from __future__ import annotations

from typing import Optional

import questionary
import typer
from rich.console import Console
from rich.table import Table

from clusterbuild.core import audit
from clusterbuild.core.catalog_loader import Catalog, CatalogError
from clusterbuild.core.jobs import get_status, is_alive, list_job_ids, start_job, tail_log
from clusterbuild.core.state import Bastion, Cluster, ClusterConfig, get_session
from clusterbuild.core.wizard import collect_answers

app = typer.Typer(help="Create and monitor OpenShift cluster installs.")
console = Console()
err_console = Console(stderr=True)

# install_method -> job handler name registered via @register_job_handler(...)
JOB_TYPE_BY_METHOD = {
    "ipi": "ipi_install",
    "agent": "agent_install",
    "upi": "upi_install",
    "assisted": "assisted_install",
    "hcp": "hcp_install",
}

DEFAULT_ENV_PROFILE_BY_PLATFORM = {
    "vsphere": "vsphere-pnq2",
    "none": "vsphere-pnq2",  # infra_provisioning_target is vsphere for the platform-agnostic entries
    "nutanix": "nutanix-lab",
    "aws": "aws-lab",
    "azure": "azure-lab",
    "gcp": "gcp-lab",
}


def _select_bastion() -> Bastion:
    session = get_session()
    try:
        bastions = session.query(Bastion).all()
    finally:
        session.close()
    if not bastions:
        err_console.print("[bold red]No bastions registered.[/bold red] Run `clusterbuild bastion register` first.")
        raise typer.Exit(code=1)
    if len(bastions) == 1:
        return bastions[0]
    choice = questionary.select("Which bastion?", choices=[b.host for b in bastions]).ask()
    return next(b for b in bastions if b.host == choice)


def _select_management_cluster(management_cluster: Optional[str]) -> tuple[str, str]:
    """Resolve --management-cluster (or prompt for one) to a
    (cluster_name, local_kubeconfig_backup_path) pair -- HCP only. The
    management cluster must be a ClusterBuild-tracked cluster with an
    already-backed-up auth/kubeconfig, i.e. any prior IPI/UPI/Agent/Assisted
    install that completed successfully."""
    session = get_session()
    try:
        candidates = (
            session.query(Cluster)
            .filter(Cluster.status == "installed")
            .filter(Cluster.install_method != "hcp")
            .all()
        )
        if not candidates:
            err_console.print(
                "[bold red]No installed management-cluster candidates found.[/bold red] "
                "HCP requires an already-installed OpenShift cluster (4.14+) with MCE + "
                "OpenShift Virtualization configured -- install one first."
            )
            raise typer.Exit(code=1)

        if management_cluster:
            cluster = next((c for c in candidates if c.name == management_cluster), None)
            if cluster is None:
                err_console.print(f"[bold red]No installed cluster named[/bold red] {management_cluster!r}.")
                raise typer.Exit(code=1)
        elif len(candidates) == 1:
            cluster = candidates[0]
        else:
            choice = questionary.select(
                "Which installed cluster is the HyperShift management cluster?",
                choices=[c.name for c in candidates],
            ).ask()
            cluster = next(c for c in candidates if c.name == choice)

        kubeconfig_config = (
            session.query(ClusterConfig)
            .filter_by(cluster_id=cluster.id, filename="auth/kubeconfig")
            .order_by(ClusterConfig.created_at.desc())
            .first()
        )
        if kubeconfig_config is None:
            err_console.print(
                f"[bold red]No backed-up auth/kubeconfig found for {cluster.name!r}.[/bold red] "
                "It must have completed an install through ClusterBuild."
            )
            raise typer.Exit(code=1)
        return cluster.name, kubeconfig_config.backup_path
    finally:
        session.close()


@app.command("create")
def create(
    platform: str = typer.Option(..., "--platform"),
    method: str = typer.Option(..., "--method"),
    ocp_version: Optional[str] = typer.Option(None, "--ocp-version"),
    environment_profile: Optional[str] = typer.Option(None, "--environment-profile"),
    lb_host: Optional[str] = typer.Option(None, "--lb-host", help="External load balancer hostname/IP (UPI only)"),
    worker_vm_count: Optional[int] = typer.Option(None, "--worker-vm-count", help="Worker VM count (UPI `platform: none`, where compute.replicas is fixed at 0)"),
    skip_preflight: bool = typer.Option(False, "--skip-preflight", help="Skip DNS/LB pre-flight checks (UPI only)"),
    management_cluster: Optional[str] = typer.Option(
        None, "--management-cluster", help="Name of an already-installed ClusterBuild cluster to host this cluster's control plane on (HCP only)"
    ),
) -> None:
    """Run the interactive wizard for PLATFORM/METHOD and kick off the install as a background job."""
    catalog = Catalog()
    ocp_version = ocp_version or catalog.default_ga_version()
    try:
        entry = catalog.load_entry(ocp_version, platform, method)
    except CatalogError as exc:
        err_console.print(f"[bold red]Catalog error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc

    job_type = JOB_TYPE_BY_METHOD.get(method)
    if job_type is None:
        err_console.print(f"[bold red]No job handler registered for install_method={method!r} yet.[/bold red]")
        raise typer.Exit(code=1)

    if entry.is_preview:
        proceed = questionary.confirm(
            f"{platform}/{method} @ OCP {ocp_version} is a PREVIEW catalog entry "
            "(GitHub-sourced, not yet on docs.redhat.com). Continue anyway?",
            default=False,
        ).ask()
        if not proceed:
            raise typer.Exit(code=1)

    console.print(f"[bold]{entry.description}[/bold]\n")
    bastion = _select_bastion()
    env_profile = environment_profile or DEFAULT_ENV_PROFILE_BY_PLATFORM.get(entry.infra_provisioning_target or platform)

    answers = collect_answers(entry)
    cluster_name = answers.get("metadata.name")
    if not cluster_name:
        err_console.print("[bold red]metadata.name is required.[/bold red]")
        raise typer.Exit(code=1)

    session = get_session()
    try:
        cluster = Cluster(
            name=cluster_name,
            base_domain=answers.get("baseDomain", ""),
            ocp_version=ocp_version,
            install_config_platform=platform,
            infra_provisioning_target=entry.infra_provisioning_target or platform,
            install_method=method,
            network_mode="static" if any("hosts" == p.split(".")[-1] for p in answers) else "dhcp",
            bastion_id=bastion.id,
            status="created",
        )
        session.add(cluster)
        session.commit()
        cluster_id = cluster.id
    finally:
        session.close()

    params = {
        "cluster_id": cluster_id,
        "cluster_name": cluster_name,
        "bastion_id": bastion.id,
        "platform": platform,
        "install_method": method,
        "ocp_version": ocp_version,
        "environment_profile": env_profile,
        "answers": answers,
    }
    if method == "upi":
        if lb_host is None and not skip_preflight:
            lb_host = questionary.text("Load balancer hostname/IP (for DNS/LB pre-flight checks):").ask()
        if worker_vm_count is None:
            worker_vm_count = int(questionary.text("Worker VM count to provision:", default="2").ask())
        params.update({"lb_host": lb_host, "worker_vm_count": worker_vm_count, "skip_preflight": skip_preflight})
    elif method == "assisted":
        backend = questionary.select("Assisted Installer backend:", choices=entry.backend_options).ask()
        worker_vm_count = worker_vm_count if worker_vm_count is not None else int(
            questionary.text("Worker VM count to provision:", default="2").ask()
        )
        params.update({"backend": backend, "worker_vm_count": worker_vm_count})
    elif method == "hcp":
        mgmt_name, mgmt_kubeconfig_path = _select_management_cluster(management_cluster)
        params.update({
            "management_cluster_name": mgmt_name,
            "management_cluster_kubeconfig_local_path": mgmt_kubeconfig_path,
        })
    job_id = start_job(job_type, params, cluster_id=cluster_id, phase="install")
    audit.record("cluster.create", detail=f"cluster={cluster_name} platform={platform} method={method} job={job_id}")

    console.print(f"\n[green]Started install job {job_id} for cluster '{cluster_name}'.[/green]")
    console.print(f"Follow progress with: [bold]clusterbuild cluster logs {job_id} --follow[/bold]")


@app.command("logs")
def logs(job_id: str = typer.Argument(...), follow: bool = typer.Option(False, "--follow", "-f")) -> None:
    for line in tail_log(job_id, follow=follow):
        console.print(line)
    status = get_status(job_id)
    console.print(f"\n[dim]job status: {status}[/dim]")


@app.command("status")
def status(cluster_name: str = typer.Argument(...)) -> None:
    session = get_session()
    try:
        cluster = session.query(Cluster).filter_by(name=cluster_name).one_or_none()
    finally:
        session.close()
    if cluster is None:
        err_console.print(f"[bold red]No such cluster:[/bold red] {cluster_name}")
        raise typer.Exit(code=1)
    console.print(f"cluster: {cluster.name}")
    console.print(f"  platform: {cluster.install_config_platform} (infra: {cluster.infra_provisioning_target})")
    console.print(f"  method: {cluster.install_method}")
    console.print(f"  ocp_version: {cluster.ocp_version}")
    console.print(f"  status: {cluster.status}")


@app.command("list")
def list_clusters() -> None:
    session = get_session()
    try:
        clusters = session.query(Cluster).all()
    finally:
        session.close()
    table = Table(title="Clusters")
    for col in ("Name", "Platform", "Method", "OCP", "Status"):
        table.add_column(col)
    for c in clusters:
        table.add_row(c.name, c.install_config_platform, c.install_method, c.ocp_version, c.status)
    console.print(table)

    console.print("\n[dim]Background jobs:[/dim]")
    for job_id in list_job_ids():
        alive = "running" if is_alive(job_id) else get_status(job_id)
        console.print(f"  {job_id}: {alive}")


@app.command("kubeconfig")
def kubeconfig(cluster_name: str = typer.Argument(...)) -> None:
    session = get_session()
    try:
        cluster = session.query(Cluster).filter_by(name=cluster_name).one_or_none()
        if cluster is None:
            err_console.print(f"[bold red]No such cluster:[/bold red] {cluster_name}")
            raise typer.Exit(code=1)
        config = (
            session.query(ClusterConfig)
            .filter_by(cluster_id=cluster.id, filename="auth/kubeconfig")
            .order_by(ClusterConfig.created_at.desc())
            .first()
        )
    finally:
        session.close()
    if config is None:
        err_console.print(f"[bold red]No kubeconfig backed up yet for {cluster_name}.[/bold red]")
        raise typer.Exit(code=1)
    console.print(config.backup_path)
