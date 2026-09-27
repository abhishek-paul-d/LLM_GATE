"""Synthetic name pools. Nothing here refers to a real system.

- Hostnames use the reserved ``.example`` TLD (RFC 2606).
- IP addresses come only from the documentation ranges (RFC 5737 TEST-NET-1/2/3).
The validator enforces both rules on every generated case.
"""

from __future__ import annotations

import ipaddress

from .rng import Rng

CLUSTERS = ("synth-east-1", "synth-east-2", "synth-west-1", "synth-central-1")
NAMESPACES = ("payments", "catalog", "identity", "search", "fulfillment", "notifications", "billing", "reporting")
SERVICES = (
    "checkout-api", "cart-svc", "ledger-svc", "invoice-worker", "catalog-api", "pricing-svc",
    "auth-gateway", "session-svc", "search-indexer", "query-api", "order-router", "shipment-tracker",
    "notify-dispatch", "email-renderer", "report-builder", "export-worker",
)  # fmt: skip
TEAMS = ("team-alpha", "team-bravo", "team-charlie", "team-delta")
TIERS = ("frontend", "backend", "batch")

# Dependencies a service may call. Split-agnostic: they are names, not phrasing.
UPSTREAMS = (
    "payments-gateway", "fraud-scorer", "tax-engine", "inventory-api", "profile-store",
    "geo-lookup", "rate-limiter", "feature-flags",
)  # fmt: skip
DATASTORES = (("postgres", 5432), ("redis", 6379), ("kafka", 9092), ("mongodb", 27017))

TEST_NETS = tuple(ipaddress.ip_network(n) for n in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24"))
SYNTH_TLD = ".example"


def pod_name(rng: Rng, workload: str) -> str:
    return f"{workload}-{rng.hex(9)}-{rng.hex(5)}"


def test_net_ip(rng: Rng) -> str:
    net = rng.pick(TEST_NETS)
    return str(net.network_address + rng.between(10, 250))


def host(name: str, namespace: str) -> str:
    return f"{name}.{namespace}.svc.synth{SYNTH_TLD}"


def is_test_net(ip: str) -> bool:
    addr = ipaddress.ip_address(ip)
    return any(addr in net for net in TEST_NETS)
