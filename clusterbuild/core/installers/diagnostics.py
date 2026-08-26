"""Self-test job handler.

Doubles as (a) the thing `clusterbuild doctor --background` uses to prove the
detached-process job runner actually works end-to-end on this machine before
someone trusts it with a real 40-minute bootstrap, and (b) the integration
test fixture for `core.jobs` (see tests/test_jobs.py) -- since job handlers
only run inside the detached subprocess, exercising the real mechanism needs
a handler that subprocess can actually import, not one registered in the
test process's memory.
"""

from __future__ import annotations

import time

from clusterbuild.core.installers.base import log
from clusterbuild.core.jobs import register_job_handler


@register_job_handler("self_test")
def run(params: dict, job_dir) -> None:  # noqa: ARG001
    log("self-test job starting")
    for line in params.get("echo_lines", []):
        log(line)
    time.sleep(params.get("sleep_seconds", 0))
    if params.get("should_fail"):
        raise RuntimeError("self-test job intentionally failed (should_fail=true)")
    log("self-test job finished OK")
