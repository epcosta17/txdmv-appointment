"""Webshare proxy selection for the booking flow.

The wizard's ASP.NET session is pinned to the IP that started it, so this module is
deliberately *not* a rotator: it resolves ONE US proxy up front, verifies it can reach
the internet, and then that proxy is used for every request of the run. Rotating
mid-wizard would drop the session and lose the held slot.

Two sources, whichever is configured:
  * WEBSHARE_API_KEY   - the v2 list endpoint, filtered to the requested country
  * proxies.txt        - the dashboard's download format, 'host:port:user:pass'
"""

from __future__ import annotations

import os
import random
import re
from dataclasses import dataclass
from urllib.parse import quote

import requests

API_URL = "https://proxy.webshare.io/api/v2/proxy/list/"
# Any endpoint that reports the exit IP works; this one needs no key.
VERIFY_URL = "https://ipinfo.io/json"
VERIFY_TIMEOUT = 25  # residential exits are slow to first byte
DEFAULT_COUNTRY = "US"

# Residential plans answer with proxy_address: null - the exit IP is chosen by the
# country suffix on the username ('user-US-7') plus the port, through this gateway.
# Each such entry is a *sticky* IP: it stays put for the plan's idle timeout, which
# is what lets one entry carry a whole booking session.
BACKBONE_HOST = "p.webshare.io"


class NoUsableProxy(Exception):
    """Nothing in the pool matched the country filter or survived verification."""


@dataclass(frozen=True)
class Proxy:
    host: str
    port: int
    username: str = None
    password: str = None
    country_code: str = None

    @property
    def url(self):
        if self.username:
            return "http://{0}:{1}@{2}:{3}".format(
                quote(self.username, safe=""),
                quote(self.password or "", safe=""),
                self.host,
                self.port,
            )
        return "http://{0}:{1}".format(self.host, self.port)

    def as_requests_proxies(self):
        """Both schemes through the same exit - a split would break session stickiness."""
        return {"http": self.url, "https": self.url}

    def __repr__(self):
        # Never render the credentials: this ends up in logs and tracebacks.
        return "<Proxy {0}:{1} {2}>".format(self.host, self.port, self.country_code or "??")


# ------------------------------------------------------------------ parsing


def parse_proxy_line(line):
    """'host:port:user:pass' or 'host:port' -> Proxy."""
    parts = line.strip().split(":")
    if len(parts) not in (2, 4):
        raise ValueError("Cannot read {0!r} as host:port[:user:pass]".format(line))
    host, port = parts[0], parts[1]
    if not host or not port.isdigit():
        raise ValueError("Cannot read {0!r} as host:port[:user:pass]".format(line))
    username, password = (parts[2], parts[3]) if len(parts) == 4 else (None, None)
    return Proxy(host=host, port=int(port), username=username, password=password)


def parse_proxy_list(text):
    """One proxy per line; blank lines and # comments ignored."""
    proxies = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        proxies.append(parse_proxy_line(stripped))
    return proxies


def parse_api_results(payload, backbone_host=BACKBONE_HOST):
    """Proxies from a v2 /proxy/list/ response, dropping the ones marked invalid.

    Datacenter entries carry their own proxy_address; residential (backbone) ones
    leave it null and are reached through the shared gateway instead.
    """
    proxies = []
    for entry in payload.get("results", []):
        if entry.get("valid") is False:
            continue
        proxies.append(
            Proxy(
                host=entry.get("proxy_address") or backbone_host,
                port=int(entry["port"]),
                username=entry.get("username"),
                password=entry.get("password"),
                country_code=entry.get("country_code"),
            )
        )
    return proxies


def parse_env_file(text):
    """A minimal .env reader - avoids a python-dotenv dependency for four keys."""
    env = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        stripped = re.sub(r"^export\s+", "", stripped)
        if "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        env[key.strip()] = value
    return env


def read_env(path=".env"):
    """.env values merged under the real environment (os.environ wins)."""
    env = {}
    if os.path.exists(path):
        env.update(parse_env_file(open(path, encoding="utf-8").read()))
    for key in list(env) + [
        "WEBSHARE_API_KEY",
        "WEBSHARE_COUNTRY",
        "WEBSHARE_PROXY_HOST",
        "WEBSHARE_PROXY_PORT",
        "WEBSHARE_PROXY_USER",
        "WEBSHARE_PROXY_PASS",
    ]:
        if os.environ.get(key):
            env[key] = os.environ[key]
    return env


# --------------------------------------------------------------------- pool


class ProxyPool:
    """Candidate proxies, narrowed to one that works and then held fixed."""

    def __init__(self, proxies, country=DEFAULT_COUNTRY):
        self.proxies = list(proxies)
        self.country = country
        self._chosen = None

    def candidates(self):
        if not self.country:
            return list(self.proxies)
        wanted = self.country.upper()
        # An unknown country (proxies.txt carries none) is a maybe, not a no - the
        # live verification below is what actually settles it.
        return [
            proxy
            for proxy in self.proxies
            if proxy.country_code is None or proxy.country_code.upper() == wanted
        ]

    def pick(self, verify=None):
        """The proxy for this run. Stable across calls once resolved."""
        if self._chosen is not None:
            return self._chosen

        candidates = self.candidates()
        if not candidates:
            raise NoUsableProxy(
                "No proxy in the pool is in {0} ({1} loaded)".format(self.country, len(self.proxies))
            )

        check = verify if verify is not None else make_verifier(self.country)
        for proxy in candidates:
            if check(proxy):
                self._chosen = proxy
                return proxy
        raise NoUsableProxy("None of the {0} candidate proxies responded".format(len(candidates)))

    def discard(self, proxy):
        """Drop a proxy that died and let the next pick() choose another.

        Only safe before a slot is held: the wizard's session is bound to the IP
        that started it, so switching mid-flow means starting the flow over.
        """
        self.proxies = [candidate for candidate in self.proxies if candidate is not proxy]
        if self._chosen is proxy:
            self._chosen = None


def make_verifier(country=DEFAULT_COUNTRY):
    """A live check: does traffic actually exit through this proxy, in that country?"""

    def verify(proxy):
        try:
            response = requests.get(
                VERIFY_URL, proxies=proxy.as_requests_proxies(), timeout=VERIFY_TIMEOUT
            )
            response.raise_for_status()
            reported = (response.json().get("country") or "").upper()
        except Exception:
            return False
        return not country or reported == country.upper()

    return verify


def make_site_verifier(url, headers=None):
    """A check against the site we actually need.

    Better than an IP-echo service for two reasons: a 200 proves the exit is not
    geo-blocked (the target 403s non-US addresses outright), and it exercises the
    same tunnel the run will use, so dead residential exits are caught up front
    rather than halfway through the wizard.
    """

    def verify(proxy):
        try:
            response = requests.get(
                url,
                headers=headers or {},
                proxies=proxy.as_requests_proxies(),
                timeout=VERIFY_TIMEOUT,
            )
        except Exception:
            return False
        return response.status_code == 200

    return verify


# ------------------------------------------------------------------ loading


def fetch_api_proxies(api_key, country=DEFAULT_COUNTRY, page_size=50, mode=None):
    """Proxies for this account, in random order.

    'mode' is required by the API and depends on the plan: datacenter plans use
    'direct', residential ones reject it ("Cannot use direct connection mode with
    residential proxies") and want 'backbone'. We try direct first and fall back.

    The order is shuffled so repeated runs do not all leave through the same entry.
    """
    headers = {"Authorization": "Token {0}".format(api_key)}
    rejected = None

    for candidate in ([mode] if mode else ["direct", "backbone"]):
        params = {"mode": candidate, "page_size": page_size}
        if country:
            params["country_code__in"] = country.upper()
        response = requests.get(API_URL, headers=headers, params=params, timeout=30)
        if response.status_code == 400 and mode is None:
            rejected = response.text
            continue
        response.raise_for_status()
        proxies = parse_api_results(response.json())
        random.shuffle(proxies)
        return proxies

    raise NoUsableProxy("Webshare rejected the proxy list request: {0}".format(rejected))


def is_configured(env=None, proxy_file="proxies.txt"):
    """Whether a proxy *could* be used, without touching the network.

    The UI needs this to decide if the proxy toggle is meaningful; actually building
    a pool would call the Webshare API, which is too slow for a settings screen.
    """
    env = read_env() if env is None else env
    if proxy_file and os.path.exists(proxy_file):
        return True
    return bool(env.get("WEBSHARE_API_KEY") or env.get("WEBSHARE_PROXY_HOST"))


def load_pool(env=None, proxy_file="proxies.txt", proxy_spec=None, country=None):
    """Build a pool from whatever is configured, or return None for a direct connection.

    Order: an explicit --proxy, then proxies.txt, then the Webshare API key, then the
    WEBSHARE_PROXY_* variables.
    """
    env = read_env() if env is None else env
    country = country or env.get("WEBSHARE_COUNTRY") or DEFAULT_COUNTRY

    if proxy_spec:
        return ProxyPool([parse_proxy_line(proxy_spec)], country=None)

    if proxy_file and os.path.exists(proxy_file):
        proxies = parse_proxy_list(open(proxy_file, encoding="utf-8").read())
        if proxies:
            return ProxyPool(proxies, country=country)

    api_key = env.get("WEBSHARE_API_KEY")
    if api_key:
        return ProxyPool(fetch_api_proxies(api_key, country=country), country=country)

    host = env.get("WEBSHARE_PROXY_HOST")
    if host:
        return ProxyPool(
            [
                Proxy(
                    host=host,
                    port=int(env.get("WEBSHARE_PROXY_PORT", 80)),
                    username=env.get("WEBSHARE_PROXY_USER"),
                    password=env.get("WEBSHARE_PROXY_PASS"),
                )
            ],
            country=country,
        )

    return None
