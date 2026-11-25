"""AC-M14-03 (SC-07, SEC-15), the production half that needs no stand: ContextForge on Railway keeps SSRF on, and
the only private ranges it may reach are Railway's own. The refusals themselves (169.254.169.254, 127.0.0.1, RFC1918, a
name that resolves to them, a redirect) run on the stand: stand/tests/test_sc.py `test_sc07_*`."""

from __future__ import annotations

import ipaddress
import json

import pytest

from scripts.deploy import railway_deploy as rd

ENV = rd.load_spec()["services"]["contextforge"]["env"]
NETWORKS = [ipaddress.ip_network(n) for n in json.loads(ENV["SSRF_ALLOWED_NETWORKS"])]
BLOCKED = [
    "169.254.169.254",  # cloud metadata
    "169.254.170.2",  # container credentials endpoint
    "fd00:ec2::254",  # AWS metadata, IPv6
    "100.100.100.200",  # Alibaba metadata
    "127.0.0.1",
    "::1",
    "0.0.0.0",  # noqa: S104 (an address under test)
    "10.0.0.1",  # RFC1918 outside Railway's 10.128.0.0/9
    "172.16.0.1",
    "192.168.7.7",
    "fe80::1",
    "100.64.0.7",  # a tailnet address: the Hub's own nodes are not a fetch target
]


def test_protection_is_on_and_the_switches_that_open_it_are_off() -> None:
    assert ENV["SSRF_PROTECTION_ENABLED"] == "true"
    assert ENV["SSRF_ALLOW_PRIVATE_NETWORKS"] == "false" and ENV["SSRF_ALLOW_LOCALHOST"] == "false"


@pytest.mark.parametrize("address", BLOCKED)
def test_no_blocked_address_is_inside_the_allowed_networks(address: str) -> None:
    assert not any(ipaddress.ip_address(address) in n for n in NETWORKS), address


def test_the_allowed_networks_are_only_railways_private_ranges() -> None:
    assert sorted(map(str, NETWORKS)) == ["10.128.0.0/9", "fd12::/16"]
