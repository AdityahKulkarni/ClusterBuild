"""ClusterBuild CLI entry point.

`clusterbuild <group> <command> ...` -- see README.md for the full command
reference. The `_internal` group is not meant to be invoked by users
directly; it's how a detached background job re-enters the CLI to run
`core.jobs.run_job_in_process`.
"""

from __future__ import annotations

import typer

from clusterbuild.cli import bastion, catalog, checklist, credentials, cluster, doctor

app = typer.Typer(
    name="clusterbuild",
    help="Standalone CLI for doc-grounded OpenShift cluster installation automation.",
    no_args_is_help=True,
)

app.add_typer(checklist.app, name="checklist")
app.add_typer(catalog.app, name="catalog")
app.add_typer(credentials.app, name="credentials")
app.add_typer(bastion.app, name="bastion")
app.add_typer(cluster.app, name="cluster")
app.add_typer(doctor.app, name="doctor")

_internal_app = typer.Typer(hidden=True)


@_internal_app.command("run-job")
def run_job(job_id: str) -> None:
    from clusterbuild.core.jobs import run_job_in_process

    run_job_in_process(job_id)


app.add_typer(_internal_app, name="_internal")


@app.command("version")
def version() -> None:
    from clusterbuild import __version__

    typer.echo(f"clusterbuild {__version__}")


if __name__ == "__main__":
    app()
