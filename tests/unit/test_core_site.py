"""A site is a host and a port; it covers its host with or without 'www.', never subdomains."""

from __future__ import annotations

import pytest

from evidence_judge.core.site import InvalidSiteError, Site, on_any


@pytest.mark.parametrize(
    ("url", "site"),
    [
        ("https://Shop.Test./cart", Site("shop.test")),
        ("http://shop.test:80/", Site("shop.test")),
        ("https://shop.test:443/", Site("shop.test")),
        ("http://shop.test:443/", Site("shop.test")),
        ("https://shop.test:8443/", Site("shop.test", 8443)),
        ("https://shöp.test/", Site("xn--shp-tna.test")),
        ("http://[::1]:8000/", Site("::1", 8000)),
        ("ws://shop.test/socket", Site("shop.test")),
    ],
)
def test_site_of_a_recorded_url(url: str, site: Site) -> None:
    assert Site.of_url(url) == site


@pytest.mark.parametrize(
    "url", [None, "", "about:blank", "data:text/html,x", "https://shop.test:99999/"]
)
def test_urls_without_a_site(url: str | None) -> None:
    assert Site.of_url(url) is None


def test_a_host_idna_cannot_encode_is_kept_lower_cased() -> None:
    label = "ö" * 70
    assert Site.of_url(f"https://{label}.test/") == Site(f"{label}.test")


@pytest.mark.parametrize(
    ("site", "url", "covered"),
    [
        ("shop.test", "https://shop.test/", True),
        ("shop.test", "http://www.shop.test/", True),
        ("www.shop.test", "https://shop.test/", True),
        ("shop.test", "https://api.shop.test/", False),
        ("shop.test", "https://wwwshop.test/", False),
        ("shop.test", "https://shop.test:8443/", False),
        ("shop.test:8443", "https://shop.test:8443/", True),
        ("shop.test:8443", "https://shop.test/", False),
        ("*.shop.test", "https://api.shop.test/", True),
        ("*.shop.test", "https://a.b.shop.test/", True),
        ("*.shop.test", "https://shop.test/", False),
        ("*.shop.test", "https://evilshop.test/", False),
        ("*.shop.test", "https://api.shop.test:8443/", False),
        ("127.0.0.1", "http://127.0.0.1/", True),
        ("127.0.0.1", "http://127.0.0.1:8000/", False),
        ("localhost:3000", "http://localhost:3000/", True),
        ("[::1]:8000", "http://[::1]:8000/", True),
    ],
)
def test_what_a_site_covers(site: str, url: str, covered: bool) -> None:
    named = Site.parse(site, wildcard=True)
    visited = Site.of_url(url)
    assert visited is not None and named.covers(visited) is covered
    assert on_any(url, [Site("other.test"), named]) is covered


def test_nothing_covers_a_url_without_a_host() -> None:
    assert Site.of_url("about:blank") is None
    assert not on_any("about:blank", [Site("shop.test")])


def test_sites_print_as_host_and_port() -> None:
    assert [str(s) for s in (Site("shop.test"), Site("shop.test", 8443))] == [
        "shop.test",
        "shop.test:8443",
    ]
    assert str(Site("shop.test", wildcard=True)) == "*.shop.test"
    assert str(Site("::1", 8000)) == "[::1]:8000"
    assert Site("::1").is_ip
    assert not Site("::1").is_public_suffix


@pytest.mark.parametrize("value", ["https://./", f"{'ö' * 70}.test"])
def test_hosts_that_are_empty_or_not_encodable_are_refused(value: str) -> None:
    with pytest.raises(InvalidSiteError, match="is not an http\\(s\\) URL or a host name"):
        Site.parse(value)
