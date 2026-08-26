"""Maps `infra_provisioning_target` (from the catalog) to a Platform Driver
module. Every driver module exposes the same function names (see
drivers/vsphere.py as the reference shape) so installers/{agent,upi}.py can
call through this registry without branching on platform themselves --
adding Nutanix, then later AWS/Azure/GCP, is "add one more entry here plus a
driver module", not touching the install-method orchestration code.
"""

from __future__ import annotations

from types import ModuleType

from clusterbuild.core.drivers import nutanix, vsphere

_DRIVERS: dict[str, ModuleType] = {
    "vsphere": vsphere,
    "nutanix": nutanix,
}


class UnknownDriverError(RuntimeError):
    pass


def driver_for(infra_provisioning_target: str) -> ModuleType:
    driver = _DRIVERS.get(infra_provisioning_target)
    if driver is None:
        raise UnknownDriverError(
            f"No platform driver implemented for infra_provisioning_target={infra_provisioning_target!r}. "
            f"Known drivers: {', '.join(_DRIVERS)}"
        )
    return driver
