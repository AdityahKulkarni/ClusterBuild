"""Local, detached background Job Runner (Phase 1).

There is no server/queue: long installs (bootstrap can take 30-60+ minutes)
are launched as a *detached* subprocess of the CLI itself, re-invoking
`clusterbuild _internal run-job <job_id>` with `start_new_session=True` so it
keeps running after the parent terminal closes. Progress is communicated
purely through files on disk (`~/.clusterbuild/jobs/<job_id>/{log,status,pid}`)
plus the `jobs` row in local SQLite, so `cluster logs --follow`/`cluster status`
work whether or not the launching terminal is still open.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Callable, Iterator, Optional

from clusterbuild.core.config import job_dir
from clusterbuild.core.state import Job, get_session

JobHandler = Callable[[dict, Path], None]
_HANDLERS: dict[str, JobHandler] = {}

TERMINAL_STATUSES = {"succeeded", "failed"}


def register_job_handler(job_type: str) -> Callable[[JobHandler], JobHandler]:
    def decorator(fn: JobHandler) -> JobHandler:
        _HANDLERS[job_type] = fn
        return fn

    return decorator


def _status_path(jdir: Path) -> Path:
    return jdir / "status"


def _log_path(jdir: Path) -> Path:
    return jdir / "log"


def _pid_path(jdir: Path) -> Path:
    return jdir / "pid"


def _reentry_command() -> list[str]:
    """How the detached job subprocess re-enters the CLI to run `_internal
    run-job <id>`. Under a normal `pip`/`pipx` install, `sys.executable` is
    the Python interpreter, so re-enter via `-m`. Under a PyInstaller-frozen
    single-file binary (see scripts/build_binary.sh), `sys.executable` *is*
    the `clusterbuild` binary itself -- it doesn't understand `-m` and
    re-invoking it directly is both correct and required (there's no
    separate interpreter to `-m` into)."""
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [sys.executable, "-m", "clusterbuild.cli.main"]


def start_job(job_type: str, params: dict, *, cluster_id: int, phase: str) -> str:
    job_id = uuid.uuid4().hex
    jdir = job_dir(job_id)
    (jdir / "params.json").write_text(json.dumps({"job_type": job_type, "params": params}), encoding="utf-8")
    _status_path(jdir).write_text("running", encoding="utf-8")
    log_path = _log_path(jdir)
    log_path.touch()

    session = get_session()
    try:
        session.add(Job(id=job_id, cluster_id=cluster_id, phase=phase, status="running", log_path=str(log_path)))
        session.commit()
    finally:
        session.close()

    with open(log_path, "ab", buffering=0) as log_fh:
        proc = subprocess.Popen(
            _reentry_command() + ["_internal", "run-job", job_id],
            stdout=log_fh,
            stderr=log_fh,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    _pid_path(jdir).write_text(str(proc.pid), encoding="utf-8")

    session = get_session()
    try:
        job = session.get(Job, job_id)
        if job:
            job.pid = proc.pid
            session.commit()
    finally:
        session.close()

    return job_id


def run_job_in_process(job_id: str) -> None:
    """Entry point invoked inside the detached subprocess -- do not call directly from the CLI."""
    # Import lazily so `_HANDLERS` is populated before we look one up, without
    # creating an import cycle between jobs.py and the installer modules.
    import clusterbuild.core.installers  # noqa: F401  (side-effect: registers handlers)

    jdir = job_dir(job_id)
    spec = json.loads((jdir / "params.json").read_text(encoding="utf-8"))
    job_type = spec["job_type"]
    params = spec["params"]

    handler = _HANDLERS.get(job_type)
    final_status = "failed"
    try:
        if handler is None:
            raise RuntimeError(f"No job handler registered for job_type={job_type!r}")
        handler(params, jdir)
        final_status = "succeeded"
    except Exception as exc:  # noqa: BLE001 -- top-level job boundary, must not crash silently
        print(f"[clusterbuild] job {job_id} failed: {exc}", file=sys.stderr)
        raise
    finally:
        _status_path(jdir).write_text(final_status, encoding="utf-8")
        session = get_session()
        try:
            job = session.get(Job, job_id)
            if job:
                job.status = final_status
                from datetime import datetime, timezone

                job.finished_at = datetime.now(timezone.utc)
                session.commit()
        finally:
            session.close()


def get_status(job_id: str) -> str:
    jdir = job_dir(job_id)
    path = _status_path(jdir)
    if not path.exists():
        return "unknown"
    return path.read_text(encoding="utf-8").strip()


def _is_zombie(pid: int) -> bool:
    """On Linux, a job whose parent hasn't reaped it yet still answers
    os.kill(pid, 0) even after it has finished -- check /proc for the real
    state so `is_alive` reflects reality instead of zombie leftovers."""
    proc_status = Path(f"/proc/{pid}/status")
    if not proc_status.exists():
        return False
    try:
        for line in proc_status.read_text(encoding="utf-8").splitlines():
            if line.startswith("State:"):
                return "Z" in line
    except OSError:
        pass
    return False


def is_alive(job_id: str) -> bool:
    jdir = job_dir(job_id)
    pid_path = _pid_path(jdir)
    if not pid_path.exists():
        return False
    try:
        pid = int(pid_path.read_text().strip())
        os.kill(pid, 0)
        return not _is_zombie(pid)
    except (ValueError, ProcessLookupError, PermissionError):
        return False


def tail_log(job_id: str, *, follow: bool = False, poll_interval: float = 1.0) -> Iterator[str]:
    jdir = job_dir(job_id)
    path = _log_path(jdir)
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        while True:
            line = fh.readline()
            if line:
                yield line.rstrip("\n")
                continue
            if not follow or get_status(job_id) in TERMINAL_STATUSES:
                break
            time.sleep(poll_interval)


def list_job_ids() -> list[str]:
    from clusterbuild.core.config import get_paths

    jobs_dir = get_paths().jobs_dir
    if not jobs_dir.exists():
        return []
    return sorted(p.name for p in jobs_dir.iterdir() if p.is_dir())
