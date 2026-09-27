"""Deterministic synthetic benchmark generator for the alert-triage demo workload."""

from .build import FrozenSuiteError, freeze_suite, generate_cases, load_config, read_cases, read_manifest, write_suite
from .schema import SuiteCase, SuiteConfig, SuiteManifest
from .validate import Issue, validate_cases, validate_suite_dir

__all__ = [
    "FrozenSuiteError",
    "Issue",
    "SuiteCase",
    "SuiteConfig",
    "SuiteManifest",
    "freeze_suite",
    "generate_cases",
    "load_config",
    "read_cases",
    "read_manifest",
    "validate_cases",
    "validate_suite_dir",
    "write_suite",
]
