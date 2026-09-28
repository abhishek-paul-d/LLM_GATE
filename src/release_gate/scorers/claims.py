"""Unsupported claims: concrete values in a model's answer that it was never shown (plan.md §8).

Values are extracted from ``summary`` and ``next_check`` with regular expressions. A value is
*supported* when it appears in the grounding text: everything the model was shown, i.e. the
system prompt plus the alert (the prompt states severity thresholds a model may quote).

Extracted, in this order (each match is masked so later patterns don't re-match it):
- IPv4 addresses, ISO timestamps, dates and clock times: must appear verbatim.
- Hostnames (two or more dots, or a known suffix such as ``.example``): must appear.
- ``kind/name`` references (``deployment/cart-svc``) and hash-bearing identifiers such as pod
  names (a hyphenated token with a segment of 4+ characters containing a digit): the name must
  appear as a whole name (``cart`` does not match inside ``cart-svc``).
- ``kubectl`` arguments: namespace, container, resource names and label/field-selector values.
- Numbers in ``summary`` (not ``next_check``, where numbers are command parameters such as
  ``--tail 50``): a number with a unit matches a grounding value of the same dimension after
  converting units and rounding to the claim's precision (``13.3 s`` matches ``13311 ms``); a
  bare number matches any grounding number at the claim's precision.

Known limits (v1): a plain hyphenated or single-word name outside a ``kind/name`` form or a
kubectl command (``the payment-api service``) is not checked, because it cannot be told apart
from ordinary words (``read-only``, ``back-off``).
"""

from __future__ import annotations

import re

from .commands import KUBECTL_SUBVERBS, KUBECTL_VALUE_FLAGS, kubectl_invocations

KINDS = frozenset({
    "pod", "pods", "po", "deployment", "deployments", "deploy", "replicaset", "replicasets", "rs",
    "statefulset", "statefulsets", "sts", "daemonset", "daemonsets", "ds", "service", "services", "svc",
    "node", "nodes", "no", "namespace", "namespaces", "ns", "configmap", "configmaps", "cm", "secret",
    "secrets", "job", "jobs", "cronjob", "cronjobs", "cj", "ingress", "ingresses", "ing", "pvc",
    "persistentvolumeclaim", "persistentvolumeclaims", "pv", "persistentvolume", "persistentvolumes",
    "hpa", "horizontalpodautoscaler", "horizontalpodautoscalers", "endpoints", "ep", "event", "events",
    "ev", "networkpolicy", "networkpolicies", "netpol", "all",
})  # fmt: skip
# Sub-command flags whose value is the next token; only -c, -l and --field-selector values are claims.
_SUBCOMMAND_VALUE_FLAGS = frozenset({
    "-c", "--container", "-l", "--selector", "--field-selector", "-o", "--output", "--tail", "--since",
    "--since-time", "--sort-by", "--template", "--limit-bytes", "--max-log-requests", "--chunk-size",
    "-L", "--label-columns", "--revision", "--timeout", "--for",
})  # fmt: skip
HOST_SUFFIXES = frozenset({"example", "local", "internal", "svc", "cluster", "com", "net", "org", "io", "test"})

_IP = re.compile(r"(?<![\d.])\d{1,3}(?:\.\d{1,3}){3}(?![\d.]*\d)")
_ISO = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?Z?")
_DATE = re.compile(r"(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)")
_TIME = re.compile(r"(?<![\d:])\d{1,2}:\d{2}(?::\d{2})?(?![\d:])")
_LABEL = r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?"
_HOST = re.compile(rf"(?<![\w.-]){_LABEL}(?:\.{_LABEL})+(?![\w-]|\.\w)", re.IGNORECASE)
_KIND_NAME = re.compile(r"(?<![\w/-])([a-z]+)/([a-z0-9](?:[a-z0-9.-]*[a-z0-9])?)", re.IGNORECASE)
_IDENT = re.compile(r"(?<![\w./-])[a-z0-9]+(?:-[a-z0-9]+)+(?![\w/-]|\.\w)", re.IGNORECASE)

# Units by dimension: factor to the dimension's base unit (MiB, ms, %, millicores).
_UNITS: dict[str, tuple[str, float]] = {
    "kib": ("memory", 1 / 1024), "ki": ("memory", 1 / 1024), "mib": ("memory", 1.0), "mi": ("memory", 1.0),
    "mb": ("memory", 1.0), "gib": ("memory", 1024.0), "gi": ("memory", 1024.0), "gb": ("memory", 1024.0),
    "ms": ("time", 1.0), "millisecond": ("time", 1.0), "milliseconds": ("time", 1.0),
    "s": ("time", 1000.0), "sec": ("time", 1000.0), "secs": ("time", 1000.0), "second": ("time", 1000.0),
    "seconds": ("time", 1000.0), "%": ("percent", 1.0), "percent": ("percent", 1.0),
    "millicores": ("cpu", 1.0), "core": ("cpu", 1000.0), "cores": ("cpu", 1000.0),
}  # fmt: skip
_UNIT_ALT = "|".join(sorted((re.escape(u) for u in _UNITS), key=len, reverse=True))
_NUMBER = re.compile(
    rf"(?<![\w.:/=-])(\d+(?:\.\d+)?)(?!\.\d)(?:\s?({_UNIT_ALT})(?![\w%]))?(?![\w%]|\.\d)",
    re.IGNORECASE,
)


def unsupported_claims(summary: str, next_check: str, grounding: str) -> list[str]:
    """Sorted, de-duplicated ``kind:value`` strings for every claim not supported by ``grounding``."""
    ground_lower = grounding.lower()
    ground_numbers = _numbers(_mask_non_numbers(grounding))
    claims: set[str] = set()

    for field, text in (("summary", summary), ("next_check", next_check)):
        work = text
        for kind, pattern in (("ip", _IP), ("time", _ISO), ("time", _DATE), ("time", _TIME)):
            for m in pattern.finditer(work):
                if m.group(0).rstrip("Z").lower() not in ground_lower:
                    claims.add(f"{kind}:{m.group(0)}")
            work = pattern.sub(lambda m: " " * len(m.group(0)), work)
        for m in _HOST.finditer(work):
            host = m.group(0)
            if _is_host(host):
                if host.lower() not in ground_lower:
                    claims.add(f"host:{host}")
                work = work[: m.start()] + " " * len(host) + work[m.end() :]
        for m in _KIND_NAME.finditer(work):
            if m.group(1).lower() in KINDS and not _has_name(m.group(2), ground_lower):
                claims.add(f"name:{m.group(2)}")
        for m in _IDENT.finditer(work):
            if _hash_bearing(m.group(0)) and not _has_name(m.group(0), ground_lower):
                claims.add(f"name:{m.group(0)}")
        for tokens in kubectl_invocations(text):
            for kind, value in _kubectl_values(tokens):
                if not _has_name(value, ground_lower):
                    claims.add(f"{kind}:{value}")
        if field == "summary":
            for value, unit, decimals in _numbers(work):
                if not _number_supported(value, unit, decimals, ground_numbers):
                    claims.add(f"number:{_fmt(value, decimals)}{' ' + unit if unit else ''}")
    return sorted(claims)


# --------------------------------------------------------------------------- helpers


def _has_name(name: str, ground_lower: str) -> bool:
    """``name`` occurs in the grounding as a whole name, not as part of a longer hyphenated one."""
    return re.search(rf"(?<![\w-]){re.escape(name.lower())}(?![\w-])", ground_lower) is not None


def _is_host(token: str) -> bool:
    """A ``_HOST`` match is a hostname, not a decimal (``6.8``) or a two-label word (``config.yaml``)."""
    labels = token.lower().split(".")
    return any(c.isalpha() for c in labels[-1]) and (len(labels) >= 3 or labels[-1] in HOST_SUFFIXES)


def _hash_bearing(token: str) -> bool:
    if not any(c.isalpha() for c in token):
        return False  # dates, numeric ranges
    return any(len(seg) >= 4 and any(c.isdigit() for c in seg) for seg in token.split("-"))


def _kubectl_values(tokens: list[str]) -> list[tuple[str, str]]:
    """(kind, value) pairs a kubectl command asserts exist: namespace, container, names, selectors."""
    out: list[tuple[str, str]] = []
    args = tokens[1:]
    positionals: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            break  # the rest is the command run in the container (kubectl exec), not kubectl arguments
        flag, eq, inline = a.partition("=")
        value = inline if eq else (args[i + 1] if i + 1 < len(args) else "")
        takes_value = flag in KUBECTL_VALUE_FLAGS or flag in _SUBCOMMAND_VALUE_FLAGS
        if a.startswith("-") and takes_value:
            if flag in ("-n", "--namespace"):
                out.append(("namespace", value))
            elif flag in ("-c", "--container"):
                out.append(("name", value))
            elif flag in ("-l", "--selector", "--field-selector"):
                for part in value.split(","):
                    rhs = re.split(r"!=|==|=", part, maxsplit=1)
                    if len(rhs) == 2 and rhs[1]:
                        out.append(("name", rhs[1]))
            i += 1 if eq else 2
            continue
        if not a.startswith("-"):
            positionals.append(a)
        i += 1
    if not positionals:
        return [(k, v) for k, v in out if v]
    verb, rest = positionals[0].lower(), positionals[1:]
    if verb in KUBECTL_SUBVERBS:
        rest = rest[1:]
    if verb in ("explain", "api-resources", "api-versions", "version", "cluster-info", "config", "auth"):
        rest = []
    for p in rest:
        kind, slash, name = p.partition("/")
        if slash:
            if kind.lower() in KINDS:
                out.append(("name", name))
        elif not p.isdigit() and not all(k.lower() in KINDS for k in p.split(",")):
            out.append(("name", p))
    return [(k, v) for k, v in out if v]


def _mask_non_numbers(text: str) -> str:
    """Blank out IPs, timestamps, hostnames and identifiers so their digits aren't read as numbers."""
    for pattern in (_IP, _ISO, _DATE, _TIME, _IDENT):
        text = pattern.sub(lambda m: " " * len(m.group(0)), text)
    # Only real hostnames: _HOST also matches decimals, and masking those would drop every
    # decimal value from the grounding (scorer 1.0.0 bug: "6.8%" in the alert was unsupported).
    return _HOST.sub(lambda m: " " * len(m.group(0)) if _is_host(m.group(0)) else m.group(0), text)


def _numbers(text: str) -> list[tuple[float, str | None, int]]:
    out = []
    for m in _NUMBER.finditer(text):
        raw, unit = m.group(1), m.group(2)
        decimals = len(raw.partition(".")[2])
        out.append((float(raw), unit.lower() if unit else None, decimals))
    return out


def _number_supported(value: float, unit: str | None, decimals: int, ground: list[tuple[float, str | None, int]]) -> bool:
    """A claim written with ``decimals`` places matches any grounding value that rounds to it."""
    tolerance = 0.5 * 10**-decimals + 1e-9
    for g_value, g_unit, _ in ground:
        if unit is None:
            converted = g_value
        elif g_unit is None:
            continue  # a value with a unit needs a grounding value of the same dimension
        else:
            (dim, factor), (g_dim, g_factor) = _UNITS[unit], _UNITS[g_unit]
            if dim != g_dim:
                continue
            converted = g_value * g_factor / factor
        if abs(converted - value) <= tolerance:
            return True
    return False


def _fmt(value: float, decimals: int) -> str:
    return f"{value:.{decimals}f}"
