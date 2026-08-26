"""DNS/load-balancer pre-flight validation (Phase 4).

Runs *before* any VM gets created for UPI (and the mandatory-LB `platform:
none` variants): resolves every concrete DNS record the catalog's
`networking.required_dns` says is needed, and TCP-probes every load-balancer
port the catalog says is required. Wildcard/per-node record patterns (that
depend on hosts not chosen yet) are skipped -- this is a sanity check against
the checklist, not a full substitute for it.
"""

from __future__ import annotations

import socket

from clusterbuild.core.catalog_loader import CatalogEntry

DNS_TIMEOUT_SECONDS = 5
TCP_TIMEOUT_SECONDS = 3


def resolve_dns(hostname: str, timeout: float = DNS_TIMEOUT_SECONDS) -> str | None:
    try:
        socket.setdefaulttimeout(timeout)
        return socket.gethostbyname(hostname)
    except socket.gaierror:
        return None


def check_tcp_port(host: str, port: int, timeout: float = TCP_TIMEOUT_SECONDS) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _concrete_dns_records(entry: CatalogEntry, *, cluster_name: str, base_domain: str) -> list[str]:
    records = []
    for rec in entry.networking.get("required_dns", []):
        hostname = rec["record"].replace("<cluster_name>", cluster_name).replace("<base_domain>", base_domain)
        if "<" in hostname or "*" in hostname:
            continue  # per-node / wildcard patterns can't be resolved without a concrete hostname
        records.append(hostname)
    return records


def run_preflight(
    entry: CatalogEntry,
    *,
    cluster_name: str,
    base_domain: str,
    load_balancer_host: str | None,
) -> list[str]:
    """Returns a list of human-readable problems; empty means all checks passed."""
    problems: list[str] = []

    for hostname in _concrete_dns_records(entry, cluster_name=cluster_name, base_domain=base_domain):
        if resolve_dns(hostname) is None:
            problems.append(f"DNS record does not resolve: {hostname}")

    lb = entry.networking.get("load_balancer", {})
    if lb.get("required"):
        if not load_balancer_host:
            problems.append("Load balancer is required for this install method but no --lb-host was given.")
        else:
            for pool in lb.get("pools", []):
                port = pool["port"]
                if not check_tcp_port(load_balancer_host, port):
                    problems.append(
                        f"Load balancer {load_balancer_host}:{port} ({pool.get('purpose', 'unspecified')}) is not reachable"
                    )
    return problems
