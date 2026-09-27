"""Schemas for structured model outputs."""

from .triage import Category, Severity, TriageRecord, parse_triage_record

__all__ = ["TriageRecord", "Category", "Severity", "parse_triage_record"]
