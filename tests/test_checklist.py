from clusterbuild.core.catalog_loader import Catalog
from clusterbuild.core.checklist import generate_checklist


def test_ipi_checklist_has_no_mandatory_lb():
    md = generate_checklist(Catalog(), platform="vsphere", install_method="ipi")
    assert "Not required" in md


def test_upi_checklist_has_mandatory_lb_and_full_dns():
    md = generate_checklist(Catalog(), platform="vsphere", install_method="upi")
    assert "**Required.**" in md
    assert "api-int" in md
    assert "bootstrap" in md


def test_none_upi_checklist_flags_capability_loss():
    md = generate_checklist(Catalog(), platform="none", install_method="upi")
    assert "Capability trade-offs" in md
    assert "Machine API" in md


def test_general_vcenter_checklist_is_appended():
    md = generate_checklist(Catalog(), platform="vsphere", install_method="ipi")
    assert "vCenter privileges" in md
