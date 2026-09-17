"""ServiceCatalog.services_for_repo — the repo -> services fan-out R6 writes through.

A deployment payload knows nothing about Vigil's service names, so a deploy of a repo
counts for every service in that repo. Repo spellings drift (`https://` prefixes, a
`.git` suffix, casing), and a normalization miss here degrades silently: zero services
means zero deploy_events rows means f_deploy stays 0 forever.
"""

import pytest

from vigil.impact.catalog import ServiceCatalog

DEMO_REPO = "github.com/eriklarson12/vigil-demo-shop"


def test_all_catalog_services_share_the_demo_repo(catalog):
    assert catalog.services_for_repo(DEMO_REPO) == [
        "auth",
        "checkout",
        "inventory",
        "orders",
        "payments-db",
    ]


@pytest.mark.parametrize(
    "spelling",
    [
        DEMO_REPO,
        f"https://{DEMO_REPO}",
        f"{DEMO_REPO}.git",
        f"  https://{DEMO_REPO}.git  ",
        DEMO_REPO.upper(),
    ],
)
def test_repo_spellings_normalize_to_the_same_services(catalog, spelling):
    assert catalog.services_for_repo(spelling) == catalog.services_for_repo(DEMO_REPO)


@pytest.mark.parametrize("repo", ["", "   ", "github.com/someone/other-repo"])
def test_unknown_or_empty_repo_maps_to_nothing(catalog, repo):
    assert catalog.services_for_repo(repo) == []


def test_services_without_a_repo_are_never_matched():
    cat = ServiceCatalog({"a": {"repo": DEMO_REPO}, "b": {}}, [])
    assert cat.services_for_repo(DEMO_REPO) == ["a"]
