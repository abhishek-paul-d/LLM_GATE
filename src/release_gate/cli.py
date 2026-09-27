"""Command-line interface for evaluating saved release metrics and synthetic suites.

Suite validation exit code 1 means the suite has issues; it is not a HOLD decision.
Exit 0 from ``gate run`` means the run completed and was saved; it is not a release decision.
``gate replay`` returns 3 when the saved evaluation is not reproduced.
"""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from collections import Counter
from pathlib import Path

import yaml
from pydantic import ValidationError

from release_gate import registry
from release_gate.evaluation import EvaluationError, evaluate_run, replay_run, write_evaluation
from release_gate.gate import evaluate, load_metrics, load_policy
from release_gate.generator import (
    FrozenSuiteError,
    freeze_suite,
    load_config,
    read_cases,
    read_manifest,
    validate_suite_dir,
    write_suite,
)
from release_gate.generator import build as suite_build
from release_gate.generator.schema import ReviewRecord
from release_gate.mock import PERSONAS, MockBackend, make_server
from release_gate.report import render_markdown
from release_gate.runner import RunConfig, RunError
from release_gate.runner import run as run_evaluation

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

    models_parser = subparsers.add_parser("models", help="inspect registered model specs")
    models_subparsers = models_parser.add_subparsers(dest="models_command", required=True, parser_class=ExitUsageArgumentParser)
    models_list_parser = models_subparsers.add_parser("list", help="list model specs")
    models_show_parser = models_subparsers.add_parser("show", help="show one model spec as JSON")
    models_show_parser.add_argument("name_or_path", metavar="NAME_OR_PATH")
    models_serve_parser = models_subparsers.add_parser("serve-cmd", help="print a vLLM serve command")
    models_serve_parser.add_argument("name_or_path", metavar="NAME_OR_PATH")
    models_serve_parser.add_argument("--port", type=int, required=True)
    models_serve_parser.add_argument("--gpu-memory-utilization", type=float)
    for models_action_parser in (models_list_parser, models_show_parser, models_serve_parser):
        models_action_parser.add_argument("--models-dir", type=Path, default=registry.DEFAULT_MODELS_DIR)

    suite_parser = subparsers.add_parser("suite", help="generate, inspect, review, and freeze synthetic suites")
    suite_subparsers = suite_parser.add_subparsers(dest="suite_command", required=True, parser_class=ExitUsageArgumentParser)
    suite_generate_parser = suite_subparsers.add_parser("generate", help="generate and validate a suite")
    suite_generate_parser.add_argument("--config", type=Path, required=True)
    suite_generate_parser.add_argument("--out-root", type=Path, default=Path("suites"))
    suite_validate_parser = suite_subparsers.add_parser("validate", help="validate a generated suite")
    suite_validate_parser.add_argument("suite_dir", type=Path)
    suite_freeze_parser = suite_subparsers.add_parser("freeze", help="freeze a fully approved suite")
    suite_freeze_parser.add_argument("suite_dir", type=Path)
    suite_show_parser = suite_subparsers.add_parser("show", help="show cases in a suite")
    suite_show_parser.add_argument("suite_dir", type=Path)
    suite_show_parser.add_argument("--split", choices=("dev", "release"))
    suite_show_parser.add_argument("--case", dest="case_id")
    suite_review_parser = suite_subparsers.add_parser("review", help="record a review for one case")
    suite_review_parser.add_argument("suite_dir", type=Path)
    suite_review_parser.add_argument("--case", dest="case_id", required=True)
    suite_review_parser.add_argument("--status", choices=("approved", "rejected", "candidate"), required=True)
    suite_review_parser.add_argument("--reviewer", required=True)
    suite_review_parser.add_argument("--notes", default="")

    mock_parser = subparsers.add_parser("mock", help="run a local mock model server")
    mock_subparsers = mock_parser.add_subparsers(dest="mock_command", required=True, parser_class=ExitUsageArgumentParser)
    mock_serve_parser = mock_subparsers.add_parser("serve", help="serve one deterministic mock persona")
    mock_serve_parser.add_argument("--persona", choices=sorted(PERSONAS), required=True)
    mock_serve_parser.add_argument("--served-model", required=True)
    mock_serve_parser.add_argument("--port", type=int, default=8001)
    mock_serve_parser.add_argument("--host", default="127.0.0.1")
    mock_serve_parser.add_argument("--latency-scale", type=_nonnegative_float, default=1.0)

    run_parser = subparsers.add_parser("run", help="run a suite against baseline and candidate endpoints")
    run_parser.add_argument("--suite", required=True)
    run_parser.add_argument("--split", choices=("release", "dev"), default="release")
    run_parser.add_argument("--prompt", default="prompts/triage-v1.yaml")
    run_parser.add_argument("--baseline", required=True)
    run_parser.add_argument("--candidate", required=True)
    run_parser.add_argument("--baseline-url", required=True)
    run_parser.add_argument("--candidate-url", required=True)
    run_parser.add_argument("--models-dir", type=Path, default=registry.DEFAULT_MODELS_DIR)
    run_parser.add_argument("--concurrency", type=int, default=4)
    run_parser.add_argument("--timeout", type=float, default=60.0)
    run_parser.add_argument("--max-retries", type=int, default=1)
    run_parser.add_argument("--warmup", type=int, default=2)
    run_parser.add_argument("--runs-root", type=Path, default=Path("runs"))
    run_parser.add_argument("--run-id")
    run_parser.add_argument("--env-file", type=Path)

    score_parser = subparsers.add_parser("score", help="score and gate a saved run")
    score_parser.add_argument("run_dir", type=Path)
    score_parser.add_argument("--policy", type=Path, required=True)
    score_parser.add_argument("--mode", choices=("pre_deploy", "canary"), default="pre_deploy")
    score_parser.add_argument("--suite", type=Path)
    score_parser.add_argument("--prompt", type=Path)

    replay_parser = subparsers.add_parser("replay", help="recompute a saved evaluation")
    replay_parser.add_argument("run", type=Path)
    replay_parser.add_argument("--policy", type=Path)
    replay_parser.add_argument("--suite", type=Path)
    replay_parser.add_argument("--prompt", type=Path)

    args = parser.parse_args(argv)
    if args.command in ("models", "suite", "mock", "run", "score", "replay"):
        handlers = {
            "models": _run_models_command,
            "suite": _run_suite_command,
            "mock": _run_mock_command,
            "run": _run_command,
            "score": _score_command,
            "replay": _replay_command,
        }
        # Expected failures are handled inside the handler; this keeps an unexpected one from
        # escaping with exit 1, which CI would read as HOLD.
        try:
            return handlers[args.command](args)
        except Exception as exc:  # noqa: BLE001 - exit-code contract must hold for every failure
            print(f"error: internal error: {type(exc).__name__}: {exc}", file=sys.stderr)
            return EXIT_USAGE

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


def _nonnegative_float(value: str) -> float:
    parsed = float(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be greater than or equal to zero")
    return parsed


def _run_mock_command(args: argparse.Namespace) -> int:
    persona = PERSONAS[args.persona]
    scaled_persona = persona.model_copy(
        update={
            "base_latency_ms": persona.base_latency_ms * args.latency_scale,
            "jitter_ms": persona.jitter_ms * args.latency_scale,
        }
    )
    backend = MockBackend(scaled_persona, args.served_model)
    try:
        server = make_server(backend, host=args.host, port=args.port)
    except (OSError, ValueError) as exc:  # port in use, non-loopback host
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    print(f"mock {args.persona} serving {args.served_model} on http://{args.host}:{args.port}/v1", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def _run_command(args: argparse.Namespace) -> int:
    try:
        environment_extra = {}
        if args.env_file is not None:
            loaded = json.loads(args.env_file.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict) or any(
                not isinstance(key, str) or not isinstance(value, str) for key, value in loaded.items()
            ):
                raise ValueError("environment file must be a JSON object with string keys and values")
            environment_extra = loaded
        config = RunConfig(
            suite_dir=str(args.suite),
            split=args.split,
            prompt=str(args.prompt),
            baseline=args.baseline,
            candidate=args.candidate,
            baseline_url=args.baseline_url,
            candidate_url=args.candidate_url,
            models_dir=str(args.models_dir),
            concurrency=args.concurrency,
            timeout_s=args.timeout,
            max_retries=args.max_retries,
            warmup_requests=args.warmup,
            environment_extra=environment_extra,
        )
        run_dir = run_evaluation(config, runs_root=args.runs_root, run_id=args.run_id)
        n_results = len((run_dir / "results.jsonl").read_text(encoding="utf-8").splitlines())
        print(f"wrote {run_dir} ({n_results} results)")
        return 0
    except (OSError, ValueError, yaml.YAMLError, RunError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE


def _print_decision_line(evaluation) -> None:
    decision = evaluation.decision
    print(f"{decision.outcome.value} (exit {decision.exit_code}): {decision.reason}", file=sys.stderr)


def _score_command(args: argparse.Namespace) -> int:
    try:
        evaluation = evaluate_run(
            args.run_dir,
            args.policy,
            mode=args.mode,
            suite_dir=args.suite,
            prompt_path=args.prompt,
        )
        write_evaluation(args.run_dir, evaluation)
        print(f"wrote {args.run_dir / 'decision.json'} and {args.run_dir / 'report.md'}")
        _print_decision_line(evaluation)
        return evaluation.decision.exit_code
    except (OSError, ValueError, EvaluationError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE


def _replay_command(args: argparse.Namespace) -> int:
    run_dir = args.run
    if not run_dir.exists() and (Path("runs") / run_dir).exists():
        run_dir = Path("runs") / run_dir
    try:
        evaluation, differences = replay_run(
            run_dir,
            args.policy,
            suite_dir=args.suite,
            prompt_path=args.prompt,
        )
        if args.policy is not None:
            print(f"what-if under {args.policy}: nothing was saved")
        elif differences:
            print(
                "error: replay does not reproduce the saved evaluation: " + ", ".join(differences),
                file=sys.stderr,
            )
            return 3
        else:
            print("replay reproduces the saved evaluation")
        _print_decision_line(evaluation)
        return evaluation.decision.exit_code
    except (OSError, ValueError, EvaluationError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE


def _run_models_command(args: argparse.Namespace) -> int:
    """Load and display model specs without changing the release decision path."""
    try:
        if args.models_command == "list":
            names = registry.list_model_specs(args.models_dir)
            if not names:
                print(f"no model specs in {args.models_dir}", file=sys.stderr)
                return 0
            for name in names:
                spec = registry.load_model_spec(name, args.models_dir)
                pinned = "pinned" if spec.revision_pinned else "unpinned"
                gated = "gated" if spec.model.gated else "open"
                print(f"{spec.name}\t{spec.model.id}\t{spec.model.revision}\t{pinned}\t{gated}")
            return 0

        spec = registry.load_model_spec(args.name_or_path, args.models_dir)
        if args.models_command == "show":
            print(spec.model_dump_json(indent=2))
        else:
            command = spec.vllm_serve_args(args.port, args.gpu_memory_utilization)
            print(shlex.join(command))
        return 0
    except (OSError, ValueError, yaml.YAMLError) as exc:
        # ValueError also covers pydantic.ValidationError from model spec validation.
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE


def _run_suite_command(args: argparse.Namespace) -> int:
    """Run suite operations; suite validation issues use status 1, separate from gate HOLD."""
    try:
        if args.suite_command == "generate":
            config = load_config(args.config)
            suite_dir = write_suite(config, args.out_root)
            issues = validate_suite_dir(suite_dir)
            print(f"wrote {suite_dir} ({read_manifest(suite_dir).n_cases} cases)")
            for issue in issues:
                print(str(issue), file=sys.stderr)
            return 0 if not issues else 1

        suite_dir = args.suite_dir
        if args.suite_command == "validate":
            issues = validate_suite_dir(suite_dir)
            for issue in issues:
                print(str(issue), file=sys.stderr)
            reviews = suite_build.read_reviews(suite_dir)
            counts = Counter(review.status for review in reviews.values())
            print(
                f"{len(issues)} issue(s); reviews: {counts['approved']} approved, "
                f"{counts['candidate']} candidate, {counts['rejected']} rejected"
            )
            return 0 if not issues else 1

        if args.suite_command == "freeze":
            manifest = freeze_suite(suite_dir)
            print(f"frozen {manifest.suite_version} ({manifest.n_cases} cases, sha256 {manifest.cases_sha256[:12]})")
            return 0

        if args.suite_command == "show":
            cases = read_cases(suite_dir)
            matching = [case for case in cases if (args.split is None or case.split == args.split)]
            if args.case_id is not None:
                matching = [case for case in matching if case.case_id == args.case_id]
                if not matching:
                    print(f"error: case {args.case_id} not found", file=sys.stderr)
                    return EXIT_USAGE
            for case in matching:
                print(f"=== {case.case_id} [{case.split}] {case.variant}")
                expected = case.expected
                print(
                    f"expected: category={expected.category} acceptable={','.join(expected.acceptable_categories)} "
                    f"severity={expected.severity} behavior={expected.behavior}"
                )
                print(f"facts: {json.dumps(expected.required_facts)}")
                print(f"targets: {','.join(expected.next_check_targets)}")
                sys.stdout.write(case.input_text)
                if not case.input_text.endswith("\n"):
                    sys.stdout.write("\n")
                print()
            return 0

        manifest = read_manifest(suite_dir)
        if manifest.frozen:
            print("error: suite is frozen", file=sys.stderr)
            return EXIT_USAGE
        reviews = suite_build.read_reviews(suite_dir)
        if args.case_id not in reviews:
            print(f"error: case {args.case_id} not found", file=sys.stderr)
            return EXIT_USAGE
        case = next((candidate for candidate in read_cases(suite_dir) if candidate.case_id == args.case_id), None)
        if case is None:
            print(f"error: case {args.case_id} not found", file=sys.stderr)
            return EXIT_USAGE
        reviews[args.case_id] = ReviewRecord(
            status=args.status,
            case_sha256=suite_build.case_sha256(case),
            reviewer=args.reviewer,
            notes=args.notes or "",
        )
        suite_build._write_json(
            suite_dir / suite_build.REVIEW_FILE,
            {key: review.model_dump(mode="json") for key, review in reviews.items()},
        )
        print(f"{args.case_id}: {args.status}")
        return 0
    except (OSError, ValueError, yaml.YAMLError, FrozenSuiteError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":
    raise SystemExit(main())
