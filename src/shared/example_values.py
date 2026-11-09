# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""What a safe example value is (#2053): the one definition.

A schema designer's sample documents and example keys reach the deliverables and
the CI run uploads, so their values must be obvious placeholders. The designer
skills state the rule (:data:`EXAMPLE_VALUE_RULE`, word for word), and
``ci/llm/private_scan.py`` allows exactly what this module allows and nothing else:

- an angle-bracket placeholder such as ``<token>`` (:func:`is_placeholder`);
- an IP address, or a network no wider than its range, in a documentation range:
  192.0.2.0/24, 198.51.100.0/24, 203.0.113.0/24 (RFC 5737) and 2001:db8::/32
  (RFC 3849) (:func:`is_documentation_address`);
- an email at a domain RFC 2606 reserves: ``example.com``, ``example.org``,
  ``example.net`` or a subdomain of one, or any name under the ``.example``,
  ``.test``, ``.invalid`` or ``.localhost`` top-level domains
  (:func:`is_example_domain`).
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Iterator

#: The rule as the designer skills state it, word for word
#: (``tests/unit/test_example_value_skills.py``).
EXAMPLE_VALUE_RULE = (
    "Example values are obvious placeholders, never real-looking data: `<token>`, `<email>` "
    "or `user@example.com` for a secret or personal value, and IP addresses only from the "
    "documentation ranges 192.0.2.0/24, 198.51.100.0/24, 203.0.113.0/24 and 2001:db8::/32."
)

#: The same rule for the columns that hold a credential, stated right after it.
EXAMPLE_CREDENTIAL_RULE = (
    "A column that holds a credential (a `*_token`, `token_hash`, `*_password`, `*_secret` or "
    "`*_key` column) gets a placeholder such as `<token>`, `<password>` or `<secret>`, never a "
    "made-up value; a number, a date or a flag keeps its own type."
)

#: The documentation address ranges, the only IPs an example may use.
DOCUMENTATION_NETWORKS = tuple(
    ipaddress.ip_network(net)
    for net in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24", "2001:db8::/32")
)

#: An angle-bracket placeholder: ``<token>``, ``<email>``, ``<API_KEY>``. Letters,
#: spaces, dashes and underscores only, so a real token in brackets is no placeholder.
PLACEHOLDER_RE = re.compile(r"<[A-Za-z][A-Za-z _-]{0,39}>")

_EXAMPLE_DOMAIN_RE = re.compile(
    r"(?:[a-z0-9-]+\.)*(?:example\.(?:com|org|net)|[a-z0-9-]+\.(?:example|test|invalid|localhost))",
    re.IGNORECASE,
)

# A run of hex digits and colons with at least two colons, optionally ending in a
# dotted IPv4 (``::ffff:192.0.2.1``): an IPv6 candidate.
# A JSON escape (``\\n``, ``\\t``, ``\\u2014``) right before it does not hide it.
IPV6_CANDIDATE_RE = re.compile(
    r"(?:(?<![\w:.])|(?<=\\[ntr])|(?<=\\u[0-9A-Fa-f]{4}))"
    r"(?:[0-9A-Fa-f]{0,4}:){2,7}(?:[0-9A-Fa-f]{1,4}|(?:\d{1,3}\.){3}\d{1,3})?"
    r"(?![\w:.])"
)
_PREFIX_RE = re.compile(r"/\d{1,3}")
# The IPv6 global unicast block (2000::/3), the only one allocated to public networks.
_GLOBAL_UNICAST = ipaddress.ip_network("2000::/3")


def is_placeholder(value: str) -> bool:
    """True when ``value`` is one angle-bracket placeholder (``<token>``)."""
    return PLACEHOLDER_RE.fullmatch(value) is not None


def is_documentation_address(value: str) -> bool:
    """True when ``value`` is an IP address, or an ``address/prefix`` network, that lies
    wholly inside a documentation range (``192.0.2.0/16`` does not)."""
    try:
        if "/" in value:
            net = ipaddress.ip_network(value, strict=False)
            return any(
                doc.version == net.version and net.subnet_of(doc)  # type: ignore[arg-type]
                for doc in DOCUMENTATION_NETWORKS
            )
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return any(address in net for net in DOCUMENTATION_NETWORKS)


def is_example_domain(domain: str) -> bool:
    """True when ``domain`` is one RFC 2606 reserves (the module docstring): the whole
    name, so ``example.com.evil.org`` is not."""
    return _EXAMPLE_DOMAIN_RE.fullmatch(domain) is not None


# An IPv4 address, also right after a JSON escape (``\\n``, ``\\t``).
IPV4_CANDIDATE_RE = re.compile(
    r"(?:(?<![\w.])|(?<=\\[ntr])|(?<=\\u[0-9A-Fa-f]{4}))"
    r"(?:\d{1,3}\.){3}\d{1,3}(?::\d{1,5})?(?![\w.])"
)


def public_ipv4(text: str) -> Iterator[re.Match[str]]:
    """Each IPv4 address in ``text`` (with an optional ``:port``) that names a public
    network and is not in a documentation range (with its ``/prefix``, if any)."""
    for m in IPV4_CANDIDATE_RE.finditer(text):
        address = m.group(0).split(":")[0]
        try:
            parsed = ipaddress.IPv4Address(address)
        except ValueError:
            continue
        prefix = _PREFIX_RE.match(text, m.end())
        if parsed.is_global and not is_documentation_address(
            address + (prefix.group(0) if prefix else "")
        ):
            yield m


def public_ipv6(text: str) -> Iterator[re.Match[str]]:
    """Each IPv6 address in ``text`` that names a public network: in 2000::/3 and not
    in 2001:db8::/32 (with its ``/prefix``, if any, no wider than that). An IPv4-mapped
    address is left to the IPv4 check."""
    for m in IPV6_CANDIDATE_RE.finditer(text):
        try:
            address = ipaddress.IPv6Address(m.group(0))
        except ValueError:
            continue
        prefix = _PREFIX_RE.match(text, m.end())
        network = m.group(0) + (prefix.group(0) if prefix else "")
        if address in _GLOBAL_UNICAST and not is_documentation_address(network):
            yield m
