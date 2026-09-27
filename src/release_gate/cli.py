"""Command-line interface for evaluating saved release metrics."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml
from pydantic import ValidationError

from release_gate.gate import evaluate, load_metrics, load_policy
from release_gate.report import render_markdown

EXIT_USAGE = 4


class ExitUsageArgumentParser(argparse.ArgumentParser):
    """Use the CLI's reserved usage-error exit code instead of argparse's code 2."""

    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE, f"{self.prog}: error: {message}\n")


def _write_text(path: str, content: str) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(content, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    """Evaluate one saved metrics file against a versioned policy."""
    parser = ExitUsageArgumentParser(prog="gate")
    subparsers = parser.add_subparsers(dest="command", required=True, parser_class=ExitUsageArgumentParser)
    decide_parser = subparsers.add_parser("decide", help="evaluate saved run metrics")
    decide_parser.add_argument("--metrics", required=True, help="path to the run metrics JSON")
    decide_parser.add_argument("--policy", required=True, help="path to the release policy YAML")
    decide_parser.add_argument("--out", help="write decision JSON to this path")
    decide_parser.add_argument("--report", help="write a Markdown report to this path")

    args = parser.parse_args(argv)
    try:
        metrics = load_metrics(args.metrics)
        policy = load_policy(args.policy)
    except (OSError, UnicodeDecodeError, yaml.YAMLError, ValidationError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE

    # An uncaught exception would exit with code 1, which CI reads as HOLD. Any failure
    # after loading must still map to EXIT_USAGE ("no decision was produced").
    try:
        decision = evaluate(metrics, policy)
        decision_json = decision.model_dump_json(indent=2) + "\n"
        report = render_markdown(decision, metrics) if args.report else None
        if args.out:
            _write_text(args.out, decision_json)
        if report is not None:
            _write_text(args.report, report)
    except Exception as exc:  # noqa: BLE001 - exit-code contract must hold for every failure
        print(f"error: no decision produced: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_USAGE

    if not args.out:
        print(decision_json, end="")
    print(f"{decision.outcome.value} (exit {decision.exit_code}): {decision.reason}", file=sys.stderr)
    return decision.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
