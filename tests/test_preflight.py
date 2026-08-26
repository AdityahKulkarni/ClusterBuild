from clusterbuild.core.catalog_loader import Catalog
from clusterbuild.core.preflight import run_preflight


def test_preflight_reports_unresolvable_dns_and_unreachable_lb(monkeypatch):
    entry = Catalog().load_entry("4.18", "vsphere", "upi")
    monkeypatch.setattr("clusterbuild.core.preflight.resolve_dns", lambda h, timeout=5: None)
    monkeypatch.setattr("clusterbuild.core.preflight.check_tcp_port", lambda h, p, timeout=3: False)

    problems = run_preflight(entry, cluster_name="qe1", base_domain="lab.example.com", load_balancer_host="lb.lab.example.com")

    assert any("api.qe1.lab.example.com" in p for p in problems)
    assert any("api-int.qe1.lab.example.com" in p for p in problems)
    assert any(":6443" in p for p in problems)
    assert any(":22623" in p for p in problems)


def test_preflight_passes_when_everything_resolves_and_is_reachable(monkeypatch):
    entry = Catalog().load_entry("4.18", "vsphere", "upi")
    monkeypatch.setattr("clusterbuild.core.preflight.resolve_dns", lambda h, timeout=5: "10.0.0.1")
    monkeypatch.setattr("clusterbuild.core.preflight.check_tcp_port", lambda h, p, timeout=3: True)

    problems = run_preflight(entry, cluster_name="qe1", base_domain="lab.example.com", load_balancer_host="lb.lab.example.com")
    assert problems == []


def test_preflight_ipi_has_no_lb_requirement(monkeypatch):
    entry = Catalog().load_entry("4.18", "vsphere", "ipi")
    monkeypatch.setattr("clusterbuild.core.preflight.resolve_dns", lambda h, timeout=5: "10.0.0.1")
    problems = run_preflight(entry, cluster_name="qe1", base_domain="lab.example.com", load_balancer_host=None)
    assert problems == []
