"""Thin REST client for the assisted-service API (self-hosted or SaaS).

Assisted Installer is API-driven rather than a single manifest file (see
catalog/4.18/vsphere/assisted.yaml). This wraps just the handful of
`v2/clusters`, `v2/infra-envs`, and SSO-token-exchange endpoints ClusterBuild
needs -- no vendored SDK, per the "wrap existing tools/APIs, don't
reimplement" development approach.
"""

from __future__ import annotations

import time
from typing import Any, Iterable

import requests

SSO_TOKEN_URL = "https://sso.redhat.com/auth/realms/redhat-external/protocol/openid-connect/token"
SAAS_BASE_URL = "https://api.openshift.com/api/assisted-install"
REQUEST_TIMEOUT_SECONDS = 30


class AssistedApiError(RuntimeError):
    pass


def exchange_offline_token(offline_token: str, *, session: requests.Session | None = None) -> str:
    """Red Hat SSO refresh_token grant: turns a long-lived offline token into
    a short-lived (~15 min) access token for api.openshift.com."""
    session = session or requests.Session()
    resp = session.post(
        SSO_TOKEN_URL,
        data={"grant_type": "refresh_token", "client_id": "cloud-services", "refresh_token": offline_token},
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    if not resp.ok:
        raise AssistedApiError(f"SSO token exchange failed ({resp.status_code}): {resp.text}")
    return resp.json()["access_token"]


class AssistedServiceClient:
    def __init__(self, base_url: str, *, access_token: str | None = None, session: requests.Session | None = None):
        self.base_url = base_url.rstrip("/")
        self.session = session or requests.Session()
        if access_token:
            self.session.headers["Authorization"] = f"Bearer {access_token}"

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def _request(self, method: str, path: str, **kwargs) -> Any:
        resp = self.session.request(method, self._url(path), timeout=kwargs.pop("timeout", REQUEST_TIMEOUT_SECONDS), **kwargs)
        if not resp.ok:
            raise AssistedApiError(f"{method} {path} failed ({resp.status_code}): {resp.text}")
        return resp

    def create_cluster(self, payload: dict) -> dict:
        return self._request("POST", "/v2/clusters", json=payload).json()

    def create_infra_env(self, payload: dict) -> dict:
        return self._request("POST", "/v2/infra-envs", json=payload).json()

    def get_cluster(self, cluster_id: str) -> dict:
        return self._request("GET", f"/v2/clusters/{cluster_id}").json()

    def get_infra_env(self, infra_env_id: str) -> dict:
        return self._request("GET", f"/v2/infra-envs/{infra_env_id}").json()

    def discovery_iso_url(self, infra_env_id: str) -> str:
        infra_env = self.get_infra_env(infra_env_id)
        download_url = infra_env.get("download_url")
        if not download_url:
            raise AssistedApiError(f"infra-env {infra_env_id} has no download_url yet")
        return download_url

    def install_cluster(self, cluster_id: str) -> dict:
        return self._request("POST", f"/v2/clusters/{cluster_id}/actions/install").json()

    def kubeconfig(self, cluster_id: str) -> bytes:
        return self._request("GET", f"/v2/clusters/{cluster_id}/downloads/kubeconfig").content

    def wait_for_status(
        self,
        cluster_id: str,
        target_statuses: Iterable[str],
        *,
        error_statuses: Iterable[str] = ("error",),
        timeout: float = 3600,
        poll_interval: float = 15,
        on_poll=None,
    ) -> dict:
        target = set(target_statuses)
        errors = set(error_statuses)
        deadline = time.time() + timeout
        while time.time() < deadline:
            cluster = self.get_cluster(cluster_id)
            status = cluster.get("status")
            if on_poll:
                on_poll(cluster)
            if status in target:
                return cluster
            if status in errors:
                raise AssistedApiError(f"cluster {cluster_id} entered status={status}: {cluster.get('status_info')}")
            time.sleep(poll_interval)
        raise AssistedApiError(f"cluster {cluster_id} did not reach {target} within {timeout}s")
