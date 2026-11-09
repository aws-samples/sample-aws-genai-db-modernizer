# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""src/shared/example_values.py: the one definition of a safe example value (#2053)."""

from __future__ import annotations

import pytest

from src.shared import example_values as ev

_LOOKS_REAL = "q9Zk3vLmP0aB7cXy" + "W2rT8nE5"  # pragma: allowlist secret


@pytest.mark.parametrize(
    "value",
    [
        "192.0.2.10",
        "198.51.100.1",
        "203.0.113.255",
        "2001:db8::1",
        "::ffff:203.0.113.4",
        "192.0.2.0/24",
        "198.51.100.64/26",
        "2001:db8:1::/48",
    ],
)
def test_a_documentation_address_or_network_is_allowed(value: str) -> None:
    assert ev.is_documentation_address(value)


@pytest.mark.parametrize(
    "value",
    ["203.0.114.1", "198.51.99.255", "8.8.8.8", "10.0.0.4", "2001:db9::1", "192.0.2.0/16", "x"],
)
def test_any_other_address_or_a_wider_network_is_not(value: str) -> None:
    assert not ev.is_documentation_address(value)


@pytest.mark.parametrize(
    "domain",
    ["example.com", "example.org", "example.net", "mail.example.com", "shop.example", "a.test"],
)
def test_an_rfc_2606_domain_is_an_example(domain: str) -> None:
    assert ev.is_example_domain(domain)


@pytest.mark.parametrize(
    "domain", ["example.com.evil.org", "examples.com", "gmail.com", "example", "example.co"]
)
def test_a_look_alike_domain_is_not(domain: str) -> None:
    assert not ev.is_example_domain(domain)


def test_a_placeholder_holds_only_letters_spaces_dashes_and_underscores() -> None:
    assert ev.is_placeholder("<token>") and ev.is_placeholder("<API_KEY>")
    assert ev.is_placeholder("<db password>") and ev.is_placeholder("<user-email>")
    assert not ev.is_placeholder("<" + _LOOKS_REAL + ">")
    assert not ev.is_placeholder("<token>x") and not ev.is_placeholder("<>")


def test_public_ipv6_finds_only_global_unicast_outside_the_documentation_range() -> None:
    text = "a 2606:4700::1111 b 2001:db8::5 c ::1 d fe80::1 e Ab::Cd f ::ffff:8.8.8.8"
    assert [m.group(0) for m in ev.public_ipv6(text)] == ["2606:4700::1111"]
