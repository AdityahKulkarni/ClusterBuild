"""`clusterbuild doctor` -- verify the local foundation is healthy (Phase 1).

Checks config-dir bootstrap, keyring availability, SQLite state, and
(optionally) the detached background job runner end-to-end -- useful to run
once after installing the CLI, before trusting it with a real cluster build.
"""

from __future__ import annotations

import time

import keyring
import typer
from rich.console import Console

from clusterbuild.core.config import get_paths
from clusterbuild.core.jobs import get_status, start_job, tail_log
from clusterbuild.core.state import get_session

app = typer.Typer(help="Verify ClusterBuild's local foundation is healthy.")
console = Console()


@app.command("run")
def run(background_job_check: bool = typer.Option(True, "--job-check/--no-job-check")) -> None:
    paths = get_paths()
    console.print(f"config dir: {paths.home} [green]ok[/green]")

    try:
        keyring.get_keyring().name
        console.print(f"keyring backend: {keyring.get_keyring().name} [green]ok[/green]")
    except Exception as exc:  # noqa: BLE001
        console.print(f"keyring backend: [red]unavailable[/red] ({exc})")

    try:
        session = get_session()
        session.close()
        console.print(f"local SQLite state: {paths.db_path} [green]ok[/green]")
    except Exception as exc:  # noqa: BLE001
        console.print(f"local SQLite state: [red]failed[/red] ({exc})")

    if background_job_check:
        console.print("running a background self-test job ...")
        from clusterbuild.core.state import Cluster

        session = get_session()
        try:
            cluster = Cluster(
                name=f"doctor-{int(time.time())}",
                base_domain="doctor.local",
                ocp_version="4.18",
                install_config_platform="vsphere",
                infra_provisioning_target="vsphere",
                install_method="ipi",
                status="doctor-check",
            )
            session.add(cluster)
            session.commit()
            cluster_id = cluster.id
        finally:
            session.close()

        job_id = start_job("self_test", {"echo_lines": ["doctor self-test"]}, cluster_id=cluster_id, phase="doctor")
        deadline = time.time() + 15
        while get_status(job_id) == "running" and time.time() < deadline:
            time.sleep(0.2)
        status = get_status(job_id)
        mark = "[green]ok[/green]" if status == "succeeded" else f"[red]{status}[/red]"
        console.print(f"detached job runner: {mark} (job {job_id})")
        for line in tail_log(job_id):
            console.print(f"  {line}")
