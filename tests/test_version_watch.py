"""ocp5-readiness: `version_watch.check_versions` compares the local
version_matrix.yaml against docs.redhat.com's landing page and
openshift/installer's release branches. All network calls are mocked --
this only tests the parsing/diffing logic, never hits the real network."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests

from clusterbuild.core import version_watch
from clusterbuild.core.catalog_loader import Catalog


def _fake_response(*, text: str = "", json_data=None, ok: bool = True):
    resp = MagicMock()
    resp.text = text
    resp.json.return_value = json_data or []
    if ok:
        resp.raise_for_status.return_value = None
    else:
        resp.raise_for_status.side_effect = requests.RequestException("boom")
    return resp


def _branch_payload(names: list[str]) -> list[dict]:
    return [{"name": n} for n in names]


def test_reports_docs_up_to_date_when_landing_page_matches_catalog_ga():
    docs_resp = _fake_response(text="OpenShift Container Platform | 4.22 | Red Hat Documentation")
    branches_resp = _fake_response(json_data=_branch_payload(["release-4.18", "release-4.22", "main"]))

    with patch.object(version_watch.requests, "get", side_effect=[docs_resp, branches_resp]):
        result = version_watch.check_versions(Catalog())

    assert result.latest_ga_on_docs == "4.22"
    assert result.latest_ga_in_catalog == "4.22"
    assert result.docs_ahead_of_catalog is False
    assert result.errors == []


def test_flags_docs_ahead_of_catalog_when_a_newer_version_is_published():
    docs_resp = _fake_response(text="OpenShift Container Platform | 4.99 | Red Hat Documentation")
    branches_resp = _fake_response(json_data=_branch_payload([]))

    with patch.object(version_watch.requests, "get", side_effect=[docs_resp, branches_resp]):
        result = version_watch.check_versions(Catalog())

    assert result.latest_ga_on_docs == "4.99"
    assert result.docs_ahead_of_catalog is True


def test_flags_new_release_branches_not_yet_in_catalog():
    docs_resp = _fake_response(text="OpenShift Container Platform | 4.22 | Red Hat Documentation")
    branches_resp = _fake_response(
        json_data=_branch_payload(["release-4.18", "release-4.22", "release-4.30", "release-5.5", "main"])
    )

    with patch.object(version_watch.requests, "get", side_effect=[docs_resp, branches_resp]):
        result = version_watch.check_versions(Catalog())

    assert "4.30" in result.branches_missing_from_catalog
    assert "5.5" in result.branches_missing_from_catalog
    assert "4.18" not in result.branches_missing_from_catalog  # already known


def test_filters_out_ancient_eol_branches_below_the_supported_floor():
    """release-4.0 etc. are real branches in openshift/installer's history
    but long EOL and below minimum_supported_version -- not useful noise for
    'should I add a version_matrix.yaml entry?'."""
    docs_resp = _fake_response(text="OpenShift Container Platform | 4.22 | Red Hat Documentation")
    branches_resp = _fake_response(
        json_data=_branch_payload(["release-4.0", "release-4.5", "release-4.10", "release-4.23"])
    )

    with patch.object(version_watch.requests, "get", side_effect=[docs_resp, branches_resp]):
        result = version_watch.check_versions(Catalog())

    assert "4.0" not in result.branches_missing_from_catalog
    assert "4.5" not in result.branches_missing_from_catalog
    assert "4.10" not in result.branches_missing_from_catalog
    # 4.23 is already in the catalog as a preview entry by this point.
    assert "4.23" not in result.branches_missing_from_catalog


def test_records_error_and_returns_none_when_docs_unreachable():
    docs_resp = _fake_response(ok=False)
    branches_resp = _fake_response(json_data=_branch_payload(["release-4.22"]))

    with patch.object(version_watch.requests, "get", side_effect=[docs_resp, branches_resp]):
        result = version_watch.check_versions(Catalog())

    assert result.latest_ga_on_docs is None
    assert result.docs_ahead_of_catalog is False
    assert any("docs.redhat.com" in err for err in result.errors)


def test_records_error_and_returns_empty_when_github_unreachable():
    docs_resp = _fake_response(text="OpenShift Container Platform | 4.22 | Red Hat Documentation")
    branches_resp = _fake_response(ok=False)

    with patch.object(version_watch.requests, "get", side_effect=[docs_resp, branches_resp]):
        result = version_watch.check_versions(Catalog())

    assert result.release_branches_seen == []
    assert any("GitHub" in err for err in result.errors)


def test_records_error_when_landing_page_html_has_no_parseable_version():
    docs_resp = _fake_response(text="<html>totally unrelated content</html>")
    branches_resp = _fake_response(json_data=_branch_payload([]))

    with patch.object(version_watch.requests, "get", side_effect=[docs_resp, branches_resp]):
        result = version_watch.check_versions(Catalog())

    assert result.latest_ga_on_docs is None
    assert any("could not parse" in err for err in result.errors)


@pytest.mark.parametrize(
    "value,expected",
    [
        ("4.22", (4, 22)),
        ("5.0", (5, 0)),
        ("4", (4, 0)),
        ("not-a-version", (0, 0)),
    ],
)
def test_version_tuple_parsing(value, expected):
    assert version_watch._version_tuple(value) == expected
