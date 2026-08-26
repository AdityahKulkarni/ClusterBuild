"""Validate the detached-process job runner mechanism end-to-end, using the
built-in `self_test` handler (core.installers.diagnostics) since job
handlers only run inside the real detached subprocess -- a handler
registered in the test process's memory would never be visible there.
"""

import sys
import time

from clusterbuild.core import jobs
from clusterbuild.core.state import Cluster, get_session


def test_reentry_command_uses_module_flag_for_normal_interpreter(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert jobs._reentry_command() == [sys.executable, "-m", "clusterbuild.cli.main"]


def test_reentry_command_invokes_binary_directly_when_frozen(monkeypatch):
    """Under PyInstaller (see scripts/build_binary.sh), sys.executable *is*
    the clusterbuild binary -- it doesn't understand `-m`."""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert jobs._reentry_command() == [sys.executable]


def _make_cluster() -> int:
    session = get_session()
    try:
        cluster = Cluster(
            name=f"test-{time.time_ns()}",
            base_domain="lab.example.com",
            ocp_version="4.18",
            install_config_platform="vsphere",
            infra_provisioning_target="vsphere",
            install_method="ipi",
            status="created",
        )
        session.add(cluster)
        session.commit()
        return cluster.id
    finally:
        session.close()


def _wait_for_terminal(job_id: str, timeout: float = 20.0) -> str:
    deadline = time.time() + timeout
    status = jobs.get_status(job_id)
    while status == "running" and time.time() < deadline:
        time.sleep(0.2)
        status = jobs.get_status(job_id)
    return status


def test_job_runs_detached_and_log_is_tailable(tmp_path, monkeypatch):
    monkeypatch.setenv("CLUSTERBUILD_HOME", str(tmp_path))
    cluster_id = _make_cluster()

    job_id = jobs.start_job(
        "self_test",
        {"echo_lines": ["hello from job", "value=42"]},
        cluster_id=cluster_id,
        phase="test",
    )

    assert _wait_for_terminal(job_id) == "succeeded"
    lines = list(jobs.tail_log(job_id, follow=False))
    assert "hello from job" in lines
    assert "value=42" in lines
    assert "self-test job finished OK" in lines


def test_failed_job_reports_failed_status(tmp_path, monkeypatch):
    monkeypatch.setenv("CLUSTERBUILD_HOME", str(tmp_path))
    cluster_id = _make_cluster()

    job_id = jobs.start_job("self_test", {"should_fail": True}, cluster_id=cluster_id, phase="test")

    assert _wait_for_terminal(job_id) == "failed"


def test_job_survives_after_parent_process_would_exit(tmp_path, monkeypatch):
    """Simulates the "closed the terminal" scenario: the job keeps writing to
    its log file on disk regardless of whether anything is still tailing it."""
    monkeypatch.setenv("CLUSTERBUILD_HOME", str(tmp_path))
    cluster_id = _make_cluster()

    job_id = jobs.start_job(
        "self_test", {"sleep_seconds": 1, "echo_lines": ["still running"]}, cluster_id=cluster_id, phase="test"
    )
    assert jobs.is_alive(job_id)
    assert _wait_for_terminal(job_id) == "succeeded"
    # The process may take a beat to fully exit after writing its terminal
    # status file (interpreter teardown), so poll briefly instead of
    # asserting immediately.
    deadline = time.time() + 5
    while jobs.is_alive(job_id) and time.time() < deadline:
        time.sleep(0.1)
    assert not jobs.is_alive(job_id)
