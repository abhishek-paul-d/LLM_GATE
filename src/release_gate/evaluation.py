"""Score, gate and replay a saved run directory (``runs/<run_id>/``).

``evaluate_run`` reads the run, re-checks its inputs against the hashes in its manifest, scores
every response, builds ``RunMetrics`` and applies the gate. ``write_evaluation`` saves the
evidence next to the run, including a copy of the policy, so ``replay_run`` can recompute
everything from the run directory alone and confirm the saved decision is reproduced exactly.

Files added to the run directory:
- ``scores.jsonl``     one ``CaseScore`` per response
- ``metrics.json``     ``RunMetrics`` (input to the gate)
- ``decision.json``    ``Decision`` (output of the gate)
- ``report.md``        Markdown report
- ``policy.yaml``      byte-for-byte copy of the policy used
- ``evaluation.json``  versions and hashes of everything the decision depends on
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

from .gate import GATE_VERSION, Decision, Policy, RunMetrics, evaluate
from .generator import SuiteCase, read_cases
from .metrics import STATS_METHOD, build_metrics
from .prompts import load_prompt
from .report import render_markdown
from .runner.manifest import MANIFEST_FILE, RESULTS_FILE, CaseResult, RunManifest
from .scorers import SCORER_VERSION, CaseScore, score_case

SCORES_FILE = "scores.jsonl"
METRICS_FILE = "metrics.json"
DECISION_FILE = "decision.json"
REPORT_FILE = "report.md"
POLICY_FILE = "policy.yaml"
EVALUATION_FILE = "evaluation.json"

Mode = Literal["pre_deploy", "canary"]


class EvaluationError(RuntimeError):
    """The run cannot be scored: missing files, unreadable inputs, or inconsistent results."""


class EvaluationRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    mode: Mode
    scorer_version: str
    gate_version: str
    stats_method: str
    policy_version: int
    policy_sha256: str
    suite_path: str
    prompt_path: str


@dataclass(frozen=True)
class Evaluation:
    record: EvaluationRecord
    scores: list[CaseScore]
    metrics: RunMetrics
    decision: Decision
    policy_bytes: bytes

    def files(self) -> dict[str, str]:
        """Every file ``write_evaluation`` saves, as text. Deterministic for the same inputs."""
        scores = "".join(_json_line(s.model_dump(mode="json")) for s in self.scores)
        return {
            SCORES_FILE: scores,
            METRICS_FILE: _json(self.metrics.model_dump(mode="json")),
            DECISION_FILE: _json(self.decision.model_dump(mode="json")),
            REPORT_FILE: render_markdown(self.decision, self.metrics),
            EVALUATION_FILE: _json(self.record.model_dump(mode="json")),
        }


def load_run(run_dir: str | Path) -> tuple[RunManifest, list[CaseResult]]:
    run_dir = Path(run_dir)
    try:
        manifest = RunManifest.model_validate_json((run_dir / MANIFEST_FILE).read_text(encoding="utf-8"))
        lines = (run_dir / RESULTS_FILE).read_text(encoding="utf-8").splitlines()
        results = [CaseResult.model_validate_json(line) for line in lines if line.strip()]
    except (OSError, ValidationError) as exc:
        raise EvaluationError(f"{run_dir} is not a readable run: {exc}") from exc
    return manifest, results


def evaluate_run(
    run_dir: str | Path,
    policy_path: str | Path,
    *,
    mode: Mode = "pre_deploy",
    suite_dir: str | Path | None = None,
    prompt_path: str | Path | None = None,
) -> Evaluation:
    """Score and gate a run. Inputs whose hashes differ from the manifest make the run INVALID."""
    manifest, results = load_run(run_dir)
    policy_bytes, policy = _load_policy(policy_path)
    suite_dir = Path(suite_dir or manifest.suite.path)
    prompt_path = Path(prompt_path or manifest.prompt.path)

    try:
        suite_sha = hashlib.sha256((suite_dir / "cases.jsonl").read_bytes()).hexdigest()
        cases = {c.case_id: c for c in read_cases(suite_dir)}
        prompt, prompt_sha = load_prompt(prompt_path)
    except (OSError, ValueError) as exc:
        raise EvaluationError(f"cannot load the run's inputs: {exc}") from exc

    selected = [c.case_id for c in cases.values() if c.split == manifest.suite.split]
    mismatches = []
    if suite_sha != manifest.suite.cases_sha256:
        mismatches.append(f"suite {suite_dir.as_posix()}: cases.jsonl sha256 differs from the run manifest")
    if hashlib.sha256("\n".join(selected).encode("utf-8")).hexdigest() != manifest.suite.case_ids_sha256:
        mismatches.append(f"suite {suite_dir.as_posix()}: selected case ids differ from the run manifest")
    if prompt_sha != manifest.prompt.sha256:
        mismatches.append(f"prompt {prompt_path.as_posix()}: sha256 differs from the run manifest")
    if len(results) != manifest.n_results or manifest.n_results != 2 * manifest.suite.n_cases:
        mismatches.append(
            f"results: {len(results)} responses; manifest expects {manifest.n_results} for {manifest.suite.n_cases} cases"
        )

    unknown = sorted({r.case_id for r in results} - cases.keys())
    if unknown:
        raise EvaluationError(f"results reference {len(unknown)} case(s) not in {suite_dir}: {unknown[:3]}")
    scores = [score_case(cases[r.case_id], r.role, r.completion, _grounding(prompt, cases[r.case_id])) for r in results]
    try:
        metrics = build_metrics(scores, run_id=manifest.run_id, policy=policy, mode=mode, manifest_mismatches=mismatches)
    except ValueError as exc:
        raise EvaluationError(f"cannot build metrics: {exc}") from exc

    record = EvaluationRecord(
        run_id=manifest.run_id,
        mode=mode,
        scorer_version=SCORER_VERSION,
        gate_version=GATE_VERSION,
        stats_method=STATS_METHOD,
        policy_version=policy.policy_version,
        policy_sha256=hashlib.sha256(policy_bytes).hexdigest(),
        suite_path=suite_dir.as_posix(),
        prompt_path=prompt_path.as_posix(),
    )
    return Evaluation(record, scores, metrics, evaluate(metrics, policy), policy_bytes)


def write_evaluation(run_dir: str | Path, evaluation: Evaluation) -> None:
    """Save the evaluation into the run directory. An existing evaluation is never overwritten."""
    run_dir = Path(run_dir)
    if (run_dir / EVALUATION_FILE).exists():
        raise EvaluationError(f"{run_dir} is already scored; use replay to recompute it")
    for name, text in evaluation.files().items():
        (run_dir / name).write_text(text, encoding="utf-8", newline="\n")
    (run_dir / POLICY_FILE).write_bytes(evaluation.policy_bytes)


def replay_run(
    run_dir: str | Path,
    policy_path: str | Path | None = None,
    *,
    suite_dir: str | Path | None = None,
    prompt_path: str | Path | None = None,
) -> tuple[Evaluation, list[str]]:
    """Recompute a scored run from its directory. Returns the evaluation and every saved file it
    does not reproduce byte for byte (empty when the replay matches).

    ``suite_dir`` and ``prompt_path`` override where the inputs are found (e.g. a run scored on
    another machine); their content is still checked against the run manifest's hashes, and the
    saved paths are kept in the record. With ``policy_path``, the run is re-gated under another
    policy (a what-if); nothing is compared, and the second value is empty.
    """
    run_dir = Path(run_dir)
    try:
        saved = EvaluationRecord.model_validate_json((run_dir / EVALUATION_FILE).read_text(encoding="utf-8"))
    except (OSError, ValidationError) as exc:
        raise EvaluationError(f"{run_dir} has no saved evaluation to replay (run `gate score` first): {exc}") from exc
    what_if = policy_path is not None
    evaluation = evaluate_run(
        run_dir,
        policy_path if what_if else run_dir / POLICY_FILE,
        mode=saved.mode,
        suite_dir=suite_dir or saved.suite_path,
        prompt_path=prompt_path or saved.prompt_path,
    )
    if what_if:
        return evaluation, []
    record = evaluation.record.model_copy(update={"suite_path": saved.suite_path, "prompt_path": saved.prompt_path})
    evaluation = dataclasses.replace(evaluation, record=record)
    differences = []
    for name, text in evaluation.files().items():
        try:
            old = (run_dir / name).read_text(encoding="utf-8")
        except OSError:
            old = None
        if old != text:
            differences.append(name)
    return evaluation, differences


# --------------------------------------------------------------------------- helpers


def _grounding(prompt, case: SuiteCase) -> str:
    """Everything the model was shown for this case."""
    return "\n".join(m["content"] for m in prompt.messages(case.input_text))


def _load_policy(path: str | Path) -> tuple[bytes, Policy]:
    try:
        raw = Path(path).read_bytes()
        return raw, Policy.model_validate(yaml.safe_load(raw.decode("utf-8")))
    except (OSError, UnicodeDecodeError, yaml.YAMLError, ValidationError) as exc:
        raise EvaluationError(f"cannot load policy {path}: {exc}") from exc


def _json(data: object) -> str:
    return json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _json_line(data: object) -> str:
    return json.dumps(data, sort_keys=True, ensure_ascii=False) + "\n"
