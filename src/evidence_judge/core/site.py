"""Sites: the host and port a page or request is on, and the hosts a spec names.

A site is a host and a port, whatever the scheme: `https://shop.test` is `shop.test` on the
default port, so `https://shop.test:8443` is another site. Ports 80 and 443 are the default
port. A site covers its own host and the same host with or without a leading `www.`, on its
port only; it never covers subdomains. `allowed_hosts` may name every subdomain of a host
with a wildcard (`*.shop.test`), which covers `api.shop.test` and `a.b.shop.test`, not
`shop.test` itself. Hosts are IDNA-encoded and lower-cased, without a trailing dot. A site
is never part of a host (no path, query or fragment) and never a public suffix, checked
against the full Public Suffix List bundled offline (`is_public_suffix`).
"""

from __future__ import annotations

import functools
import ipaddress
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final
from urllib.parse import urlsplit

from publicsuffixlist import PublicSuffixList

DEFAULT_PORTS: Final = frozenset({80, 443})
WILDCARD: Final = "*."
LOCALHOST: Final = "localhost"


@functools.cache
def _public_suffixes() -> PublicSuffixList:
    """The full Public Suffix List (ICANN and private sections, so `myshopify.com`, `fly.dev`
    and `s3.amazonaws.com` too), as bundled with the `publicsuffixlist` package: read from its
    data file, never fetched. Unknown top-level names (`intranet`, `test`) count as suffixes."""
    return PublicSuffixList()


def is_public_suffix(host: str) -> bool:
    """Whether `host` (IDNA-encoded, lower-case) is a public suffix or a single-label name other
    than `localhost`: a name anyone can register sites under, so never one site."""
    if host == LOCALHOST:
        return False
    return "." not in host or _public_suffixes().is_public(host)


class InvalidSiteError(ValueError):
    """A `site`, --site or `allowed_hosts` value that does not name one site."""


@dataclass(frozen=True, slots=True)
class Site:
    host: str
    """IDNA-encoded, lower-case, no trailing dot; for a wildcard, the part after `*.`."""
    port: int | None = None
    """None for the default port (none given, 80 or 443)."""
    wildcard: bool = False

    @classmethod
    def parse(cls, value: str, *, what: str = "site", wildcard: bool = False) -> Site:
        """The site `value` names, as a URL ('https://shop.test/') or host ('shop.test:8000').

        Raises InvalidSiteError for anything else, for user info, for a path, query or fragment
        ('github.com/myorg': a site is a whole host), for a public suffix ('com', 'co.uk',
        'myshopify.com') and for a wildcard ('*.shop.test') unless `wildcard` allows one.
        """
        text = value.strip()
        invalid = InvalidSiteError(f"{what} {value!r} is not an http(s) URL or a host name")
        if not text or any(c.isspace() for c in text):
            raise invalid
        is_wildcard = "://" not in text and text.startswith(WILDCARD)
        if is_wildcard:
            if not wildcard:
                raise InvalidSiteError(
                    f"{what} {value!r} cannot be a wildcard; list the subdomains the task uses "
                    "in allowed_hosts (a wildcard such as '*.shop.test' is allowed there)"
                )
            text = text.removeprefix(WILDCARD)
        if "*" in text:
            raise InvalidSiteError(f"{what} {value!r} has a '*' that is not a leading '*.'")
        try:
            parts = urlsplit(text if "://" in text else f"https://{text}")
            port = parts.port
            host = parts.hostname
        except ValueError:
            raise invalid from None
        if parts.scheme not in ("http", "https") or not host:
            raise invalid
        if parts.username is not None or parts.password is not None:
            raise InvalidSiteError(f"{what} {value!r} must not contain a user name or password")
        if (
            parts.path not in ("", "/")
            or parts.query
            or parts.fragment
            or text.endswith(("?", "#"))
        ):
            raise InvalidSiteError(
                f"{what} {value!r} has a path, query or fragment, but a site is a whole host "
                "(and port): every page on it can prove the task, so it cannot be narrowed to "
                f"part of one. Name the host alone ({_display(host, _port(port))}) only if all of it "
                "belongs to the site under test, and require the pages you mean with url "
                "predicates in `expect`"
            )
        try:
            host = _idna(host)
        except UnicodeError:
            raise invalid from None
        if not host:
            raise invalid
        site = cls(host, _port(port), is_wildcard)
        if is_wildcard and site.is_ip:
            raise InvalidSiteError(f"{what} {value!r} is a wildcard of an IP address")
        if site.is_public_suffix:
            raise InvalidSiteError(
                f"{what} {value!r} is a public suffix or a single-label name, not one site's "
                "host; name the site's own host (shop.example.com, or localhost)"
            )
        return site

    @classmethod
    def of_url(cls, url: str | None) -> Site | None:
        """The site a recorded URL is on, or None when it names no host."""
        try:
            parts = urlsplit(url or "")
            host = parts.hostname
            port = parts.port
        except ValueError:
            return None
        if not host:
            return None
        try:
            host = _idna(host)
        except UnicodeError:
            host = host.casefold().rstrip(".")
        return cls(host, _port(port)) if host else None

    @property
    def is_ip(self) -> bool:
        try:
            ipaddress.ip_address(self.host)
        except ValueError:
            return False
        return True

    @property
    def is_public_suffix(self) -> bool:
        if self.is_ip:
            return False
        return any(is_public_suffix(h) for h in {self.host, _base(self.host)})

    def covers(self, other: Site) -> bool:
        """Whether a page or request on `other` (never a wildcard) is on this site."""
        if other.port != self.port:
            return False
        if self.wildcard:
            return other.host.endswith(f".{self.host}")
        return _base(other.host) == _base(self.host)

    def covers_url(self, url: str) -> bool:
        site = Site.of_url(url)
        return site is not None and self.covers(site)

    def __str__(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        port = "" if self.port is None else f":{self.port}"
        return f"{WILDCARD if self.wildcard else ''}{host}{port}"


def _display(host: str, port: int | None) -> str:
    return f"{host}:{port}" if port is not None else host


def on_any(url: str, sites: Iterable[Site]) -> bool:
    """Whether `url` is on one of `sites`."""
    site = Site.of_url(url)
    return site is not None and any(s.covers(site) for s in sites)


def _idna(host: str) -> str:
    host = host.rstrip(".")
    if host.isascii():
        return host.casefold()
    return host.encode("idna").decode("ascii").casefold()


def _base(host: str) -> str:
    return host.removeprefix("www.")


def _port(port: int | None) -> int | None:
    return None if port is None or port in DEFAULT_PORTS else port
