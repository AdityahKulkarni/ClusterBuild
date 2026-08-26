"""Phase 7: stage AWS/Azure/GCP credentials on the bastion before IPI install.

Unlike vSphere/Nutanix (whose credentials live *inside* install-config.yaml's
platform block, resolved by manifest_builder), AWS/Azure/GCP credentials are
never written into install-config.yaml -- `openshift-install`'s own
Terraform/CAPI providers read them from the ambient environment on the
machine running the installer, per each cloud's official docs:

- AWS:   ~/.aws/credentials (or AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY), see
  https://docs.redhat.com/en/documentation/openshift_container_platform/4.18/html/installing_on_aws/installer-provisioned-infrastructure
- Azure: ~/.azure/osServicePrincipal.json (subscriptionId/clientId/clientSecret/tenantId),
  written by the installer on first interactive run -- pre-staging it here
  lets ClusterBuild drive `create cluster` non-interactively -- see
  https://docs.redhat.com/en/documentation/openshift_container_platform/4.18/html/installing_on_azure/installer-provisioned-infrastructure
- GCP:   a service-account key file referenced by GOOGLE_CLOUD_KEYFILE_JSON,
  conventionally ~/.gcp/osServiceAccount.json -- see
  https://docs.redhat.com/en/documentation/openshift_container_platform/4.18/html/installing_on_google_cloud/installing-gcp-customizations

This intentionally avoids long-lived keys where the docs allow an
alternative (e.g. teams may still prefer `aws sso`/STS on the bastion
itself) -- see `stage()`'s no-op fallthrough for platforms/credentials this
module doesn't manage.
"""

from __future__ import annotations

import json
import shlex

from clusterbuild.core.bastion_exec import BastionExecutor
from clusterbuild.core.secrets import SecretsBackend

CLOUD_PLATFORMS = ("aws", "azure", "gcp")


class CloudCredentialsError(RuntimeError):
    pass


def _remote_home(executor: BastionExecutor) -> str:
    result = executor.run("echo $HOME")
    home = result.stdout.strip()
    if not result.ok or not home:
        raise CloudCredentialsError("Could not resolve $HOME on the bastion to stage cloud credentials.")
    return home


def stage(executor: BastionExecutor, platform: str, secrets: SecretsBackend) -> None:
    """Write the credential file(s) `openshift-install` expects for this
    platform onto the bastion. No-op for platforms that embed credentials
    directly in install-config.yaml instead (vsphere/nutanix/none)."""
    if platform not in CLOUD_PLATFORMS:
        return

    home = _remote_home(executor)

    if platform == "aws":
        access_key = secrets.get("aws", "access_key_id")
        secret_key = secrets.get("aws", "secret_access_key")
        if not access_key or not secret_key:
            raise CloudCredentialsError(
                "AWS credentials not found -- run `clusterbuild credentials set --platform aws`."
            )
        executor.ensure_dir(f"{home}/.aws")
        executor.run(f"chmod 700 {shlex.quote(home + '/.aws')}")
        content = f"[default]\naws_access_key_id = {access_key}\naws_secret_access_key = {secret_key}\n"
        executor.write_file(f"{home}/.aws/credentials", content)
        executor.run(f"chmod 600 {shlex.quote(home + '/.aws/credentials')}")

    elif platform == "azure":
        client_id = secrets.get("azure", "client_id")
        client_secret = secrets.get("azure", "client_secret")
        tenant_id = secrets.get("azure", "tenant_id")
        subscription_id = secrets.get("azure", "subscription_id")
        if not all([client_id, client_secret, tenant_id, subscription_id]):
            raise CloudCredentialsError(
                "Azure service principal credentials not found -- run `clusterbuild credentials set --platform azure`."
            )
        executor.ensure_dir(f"{home}/.azure")
        executor.run(f"chmod 700 {shlex.quote(home + '/.azure')}")
        payload = {
            "subscriptionId": subscription_id,
            "clientId": client_id,
            "clientSecret": client_secret,
            "tenantId": tenant_id,
        }
        executor.write_file(f"{home}/.azure/osServicePrincipal.json", json.dumps(payload))
        executor.run(f"chmod 600 {shlex.quote(home + '/.azure/osServicePrincipal.json')}")

    elif platform == "gcp":
        service_account_json = secrets.get("gcp", "service_account_json")
        if not service_account_json:
            raise CloudCredentialsError(
                "GCP service-account key not found -- run `clusterbuild credentials set --platform gcp`."
            )
        executor.ensure_dir(f"{home}/.gcp")
        executor.run(f"chmod 700 {shlex.quote(home + '/.gcp')}")
        executor.write_file(f"{home}/.gcp/osServiceAccount.json", service_account_json)
        executor.run(f"chmod 600 {shlex.quote(home + '/.gcp/osServiceAccount.json')}")
