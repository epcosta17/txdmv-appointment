"""Webshare proxy selection. No network: the verifier is injected."""

import pytest

from webshare import (
    NoUsableProxy,
    Proxy,
    ProxyPool,
    parse_api_results,
    parse_env_file,
    parse_proxy_line,
    parse_proxy_list,
)


def make(host, country="US"):
    return Proxy(host=host, port=8080, username="u", password="p", country_code=country)


# ------------------------------------------------------------------ proxy shape


def test_proxy_renders_a_requests_url():
    assert make("1.2.3.4").url == "http://u:p@1.2.3.4:8080"


def test_proxy_maps_both_schemes_through_the_same_ip():
    # Sticky IP is the whole point: http and https must not resolve to different exits.
    mapping = make("1.2.3.4").as_requests_proxies()
    assert mapping["http"] == mapping["https"] == "http://u:p@1.2.3.4:8080"


def test_credentials_are_url_encoded():
    proxy = Proxy(host="1.2.3.4", port=80, username="u@er", password="p:ss", country_code="US")
    assert proxy.url == "http://u%40er:p%3Ass@1.2.3.4:80"


def test_repr_does_not_leak_the_password():
    assert "p" not in repr(make("1.2.3.4")).replace("Proxy", "")


# ---------------------------------------------------------- proxies.txt parsing


def test_parses_the_webshare_download_format():
    proxy = parse_proxy_line("198.23.239.134:6540:user:pass")
    assert (proxy.host, proxy.port, proxy.username, proxy.password) == (
        "198.23.239.134",
        6540,
        "user",
        "pass",
    )


def test_parses_a_host_port_only_line():
    proxy = parse_proxy_line("198.23.239.134:6540")
    assert proxy.username is None and proxy.password is None


def test_malformed_line_is_rejected():
    with pytest.raises(ValueError):
        parse_proxy_line("not-a-proxy")


def test_list_skips_blanks_and_comments():
    text = "\n".join([
        "# my proxies",
        "1.1.1.1:80:u:p",
        "",
        "   ",
        "2.2.2.2:80:u:p",
    ])
    assert [p.host for p in parse_proxy_list(text)] == ["1.1.1.1", "2.2.2.2"]


# ------------------------------------------------------------------ API parsing


def test_parses_the_v2_list_endpoint():
    payload = {
        "count": 2,
        "results": [
            {
                "proxy_address": "1.1.1.1",
                "port": 8080,
                "username": "u1",
                "password": "p1",
                "country_code": "US",
                "valid": True,
            },
            {
                "proxy_address": "2.2.2.2",
                "port": 9090,
                "username": "u2",
                "password": "p2",
                "country_code": "US",
                "valid": True,
            },
        ],
    }
    proxies = parse_api_results(payload)
    assert [p.host for p in proxies] == ["1.1.1.1", "2.2.2.2"]
    assert proxies[0].port == 8080


def test_residential_backbone_entries_get_the_gateway_host():
    # Residential plans return proxy_address: null - the exit is selected by the
    # country suffix on the username plus the port, through a fixed gateway.
    payload = {
        "results": [
            {
                "id": "b-US-1",
                "proxy_address": None,
                "port": 10000,
                "username": "acmeuser-US-1",
                "password": "secret",
                "country_code": "US",
                "valid": True,
            }
        ]
    }
    proxy = parse_api_results(payload)[0]
    assert proxy.host == "p.webshare.io"
    assert proxy.port == 10000
    assert proxy.username == "acmeuser-US-1"


def test_datacenter_entries_keep_their_own_address():
    payload = {
        "results": [
            {"proxy_address": "1.2.3.4", "port": 80, "username": "u", "password": "p",
             "country_code": "US", "valid": True}
        ]
    }
    assert parse_api_results(payload)[0].host == "1.2.3.4"


def test_invalid_proxies_are_dropped():
    payload = {
        "results": [
            {"proxy_address": "1.1.1.1", "port": 80, "username": "u", "password": "p",
             "country_code": "US", "valid": False},
            {"proxy_address": "2.2.2.2", "port": 80, "username": "u", "password": "p",
             "country_code": "US", "valid": True},
        ]
    }
    assert [p.host for p in parse_api_results(payload)] == ["2.2.2.2"]


# ------------------------------------------------------------------- .env file


def test_reads_env_pairs():
    env = parse_env_file("WEBSHARE_API_KEY=abc123\nWEBSHARE_COUNTRY=US\n")
    assert env["WEBSHARE_API_KEY"] == "abc123"
    assert env["WEBSHARE_COUNTRY"] == "US"


def test_ignores_comments_and_export_prefix():
    env = parse_env_file("# comment\nexport WEBSHARE_API_KEY=abc\n\n")
    assert env == {"WEBSHARE_API_KEY": "abc"}


def test_strips_surrounding_quotes():
    assert parse_env_file('WEBSHARE_API_KEY="abc"')["WEBSHARE_API_KEY"] == "abc"


# ------------------------------------------------------------- pool behaviour


def test_pool_keeps_only_the_requested_country():
    pool = ProxyPool([make("1.1.1.1", "DE"), make("2.2.2.2", "US")], country="US")
    assert pool.pick(verify=lambda _: True).host == "2.2.2.2"


def test_pool_skips_proxies_that_fail_verification():
    tried = []

    def verify(proxy):
        tried.append(proxy.host)
        return proxy.host == "3.3.3.3"

    pool = ProxyPool([make("1.1.1.1"), make("2.2.2.2"), make("3.3.3.3")], country="US")
    assert pool.pick(verify=verify).host == "3.3.3.3"
    assert tried == ["1.1.1.1", "2.2.2.2", "3.3.3.3"]


def test_pool_raises_when_every_proxy_fails():
    pool = ProxyPool([make("1.1.1.1")], country="US")
    with pytest.raises(NoUsableProxy):
        pool.pick(verify=lambda _: False)


def test_pool_raises_when_no_proxy_matches_the_country():
    pool = ProxyPool([make("1.1.1.1", "DE")], country="US")
    with pytest.raises(NoUsableProxy):
        pool.pick(verify=lambda _: True)


def test_country_filter_can_be_disabled():
    pool = ProxyPool([make("1.1.1.1", "DE")], country=None)
    assert pool.pick(verify=lambda _: True).host == "1.1.1.1"


def test_discarding_a_dead_proxy_yields_a_different_one():
    pool = ProxyPool([make("1.1.1.1"), make("2.2.2.2")], country="US")
    first = pool.pick(verify=lambda _: True)
    pool.discard(first)
    second = pool.pick(verify=lambda _: True)
    assert second is not first
    assert second.host == "2.2.2.2"


def test_discarding_the_last_proxy_leaves_nothing_to_pick():
    pool = ProxyPool([make("1.1.1.1")], country="US")
    pool.discard(pool.pick(verify=lambda _: True))
    with pytest.raises(NoUsableProxy):
        pool.pick(verify=lambda _: True)


def test_pool_picks_the_same_proxy_for_the_whole_run():
    # The wizard's ASP.NET session is IP-bound, so pick() must be stable once resolved.
    pool = ProxyPool([make("1.1.1.1"), make("2.2.2.2")], country="US")
    first = pool.pick(verify=lambda _: True)
    assert pool.pick(verify=lambda _: True) is first
