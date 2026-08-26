from clusterbuild.core.catalog_loader import Catalog, CatalogError
import pytest


def test_default_ga_version_is_422():
    assert Catalog().default_ga_version() == "4.22"


def test_minimum_supported_version_is_418():
    assert Catalog().minimum_supported_version() == "4.18"


@pytest.mark.parametrize(
    "platform,method",
    [
        ("vsphere", "ipi"),
        ("vsphere", "upi"),
        ("vsphere", "agent"),
        ("vsphere", "assisted"),
        ("none", "upi"),
        ("none", "agent"),
    ],
)
def test_all_baseline_entries_load(platform, method):
    entry = Catalog().load_entry("4.18", platform, method)
    assert entry.platform == platform
    assert entry.install_method == method
    assert entry.doc_source.startswith("https://docs.redhat.com/") or entry.doc_source.startswith(
        "https://github.com/"
    )
    assert entry.manifests, "every entry should define at least one manifest"


def test_4_22_inherits_4_18_schema():
    """4.19-4.22 have no dedicated catalog dir; schema_ref falls back to 4.18."""
    entry_418 = Catalog().load_entry("4.18", "vsphere", "ipi")
    entry_422 = Catalog().load_entry("4.22", "vsphere", "ipi")
    assert entry_418.manifests == entry_422.manifests
    assert entry_422.ocp_version == "4.22"
    assert entry_422.schema_ref == "4.18"


def test_5_0_is_marked_preview():
    version_info = Catalog().resolve_version("5.0")
    assert version_info.is_preview
    assert version_info.doc_base is None
    assert version_info.github_source


def test_4_23_is_marked_preview_pending_ga_docs():
    """Added via the ocp5-readiness version-watch workflow: a release-4.23
    branch exists on github.com/openshift/installer before docs.redhat.com
    publishes the 4.23 doc tree -- same "GitHub-sourced preview" treatment
    as 5.0, promotable to `ga` once real docs exist."""
    version_info = Catalog().resolve_version("4.23")
    assert version_info.is_preview
    assert version_info.doc_base is None
    assert version_info.github_source
    assert version_info.schema_ref == "4.18"


def test_unknown_version_raises():
    with pytest.raises(CatalogError):
        Catalog().resolve_version("3.11")


def test_none_platform_upi_has_no_vsphere_fields_but_targets_vsphere_infra():
    entry = Catalog().load_entry("4.18", "none", "upi")
    assert entry.infra_provisioning_target == "vsphere"
    manifest_fields = entry.manifests[0]["fields"]
    paths = [f.get("path") for f in manifest_fields]
    assert not any("vsphere" in str(p) for p in paths)
    assert any(f.get("path") == "platform" for f in manifest_fields)
