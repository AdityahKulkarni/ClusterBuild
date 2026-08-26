"""Install-method orchestration modules.

Importing this package registers every install method's job handler with
`clusterbuild.core.jobs`. Keep this list in sync with the modules added per
phase (ipi -> Phase 2, agent -> Phase 3, upi -> Phase 4, assisted -> Phase 5,
hcp -> Phase 8); Nutanix/AWS/Azure/GCP reuse these same modules by branching
on the catalog's `platform`/`infra_provisioning_target`, per the plan's
"platform driver is decoupled from install_config_platform" design.
"""

from clusterbuild.core.installers import agent, assisted, diagnostics, hcp, ipi, upi  # noqa: F401
