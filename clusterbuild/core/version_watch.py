"""Version-watch tooling (ocp5-readiness todo).

Compares the local `version_matrix.yaml` against what docs.redhat.com
currently publishes as the latest OCP version, and against
github.com/openshift/installer's release branches, so new y-streams (and
eventually OCP 5.x) get flagged for a catalog update instead of silently
going unsupported -- or worse, being guessed at.

Network calls only happen when the user explicitly runs
`clusterbuild catalog check-versions`; nothing here runs implicitly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import requests

from clusterbuild.core.catalog_loader import Catalog

DOCS_LANDING_URL = "https://docs.redhat.com/en/documentation/openshift_container_platform/"
GITHUB_BRANCHES_API = "https://api.github.com/repos/openshift/installer/branches?per_page=100"
_VERSION_RE = re.compile(r"OpenShift Container Platform\s*\|?\s*(\d+\.\d+)")
_RELEASE_BRANCH_RE = re.compile(r"^release-(\d+\.\d+)$")

REQUEST_TIMEOUT_SECONDS = 10


@dataclass(frozen=True)
class VersionWatchResult:
    latest_ga_on_docs: str | None
    latest_ga_in_catalog: str
    docs_ahead_of_catalog: bool
    release_branches_seen: list[str]
    branches_missing_from_catalog: list[str]
    errors: list[str]


def _fetch_latest_ga_from_docs() -> tuple[str | None, list[str]]:
    errors: list[str] = []
    try:
        resp = requests.get(DOCS_LANDING_URL, timeout=REQUEST_TIMEOUT_SECONDS)
        resp.raise_for_status()
    except requests.RequestException as exc:
        return None, [f"could not reach docs.redhat.com: {exc}"]
    match = _VERSION_RE.search(resp.text)
    if not match:
        errors.append("could not parse a version number out of the docs.redhat.com landing page")
        return None, errors
    return match.group(1), errors


def _fetch_release_branches() -> tuple[list[str], list[str]]:
    errors: list[str] = []
    try:
        resp = requests.get(GITHUB_BRANCHES_API, timeout=REQUEST_TIMEOUT_SECONDS)
        resp.raise_for_status()
    except requests.RequestException as exc:
        return [], [f"could not reach GitHub API: {exc}"]
    branches = []
    for item in resp.json():
        m = _RELEASE_BRANCH_RE.match(item.get("name", ""))
        if m:
            branches.append(m.group(1))
    return sorted(set(branches)), errors


def check_versions(catalog: Catalog) -> VersionWatchResult:
    errors: list[str] = []
    latest_docs, docs_errors = _fetch_latest_ga_from_docs()
    errors.extend(docs_errors)
    branches, branch_errors = _fetch_release_branches()
    errors.extend(branch_errors)

    catalog_ga = catalog.default_ga_version()
    known = set(catalog.known_versions())
    floor = _version_tuple(catalog.minimum_supported_version())

    docs_ahead = bool(latest_docs) and _version_tuple(latest_docs) > _version_tuple(catalog_ga)
    # Only flag branches at/after the catalog's floor -- openshift/installer's
    # repo has release branches going back years (release-4.0, ...), all
    # long EOL and irrelevant to "should I add a new version_matrix.yaml
    # entry?"; only newer-than-floor branches are actually candidates.
    missing_branches = [b for b in branches if b not in known and _version_tuple(b) >= floor]

    return VersionWatchResult(
        latest_ga_on_docs=latest_docs,
        latest_ga_in_catalog=catalog_ga,
        docs_ahead_of_catalog=docs_ahead,
        release_branches_seen=branches,
        branches_missing_from_catalog=missing_branches,
        errors=errors,
    )


def _version_tuple(v: str) -> tuple[int, int]:
    major, _, minor = v.partition(".")
    try:
        return (int(major), int(minor or 0))
    except ValueError:
        return (0, 0)
