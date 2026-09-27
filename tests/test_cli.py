import json
from pathlib import Path

import pytest

from release_gate.cli import main

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/gate/pass_metrics.json"
POLICY = ROOT / "policies/policy_v1.yaml"


def write_metrics(tmp_path: Path, mutate) -> Path:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    mutate(data)
    path = tmp_path / "metrics.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_promote_writes_decision_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    output = tmp_path / "nested" / "decision.json"

    result = main(["decide", "--metrics", str(FIXTURE), "--policy", str(POLICY), "--out", str(output)])

    assert result == 0
    assert json.loads(output.read_text(encoding="utf-8"))["outcome"] == "PROMOTE"
    assert "PROMOTE (exit 0):" in capsys.readouterr().err


def test_reject_returns_exit_code_2(tmp_path: Path) -> None:
    metrics = write_metrics(tmp_path, lambda data: data["serving"]["error_rate"].update(candidate=0.03))

    assert main(["decide", "--metrics", str(metrics), "--policy", str(POLICY)]) == 2


def test_invalid_run_returns_exit_code_3(tmp_path: Path) -> None:
    metrics = write_metrics(tmp_path, lambda data: data["validity"].update(baseline_healthy=False))

    assert main(["decide", "--metrics", str(metrics), "--policy", str(POLICY)]) == 3


def test_insufficient_protected_slice_returns_hold(tmp_path: Path) -> None:
    metrics = write_metrics(
        tmp_path,
        lambda data: data["slices"]["prompt_injection"]["accuracy"].update(n=5),
    )

    assert main(["decide", "--metrics", str(metrics), "--policy", str(POLICY)]) == 1


def test_missing_metrics_returns_usage_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    missing = tmp_path / "missing.json"

    assert main(["decide", "--metrics", str(missing), "--policy", str(POLICY)]) == 4
    assert capsys.readouterr().err.startswith("error:")


def test_unknown_policy_field_returns_usage_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    policy = tmp_path / "policy.yaml"
    policy.write_text(POLICY.read_text(encoding="utf-8") + "\nunknown_field: true\n", encoding="utf-8")

    assert main(["decide", "--metrics", str(FIXTURE), "--policy", str(policy)]) == 4
    assert "unknown_field" in capsys.readouterr().err


@pytest.mark.parametrize("arguments", [["decide"], ["unknown"]])
def test_argparse_errors_use_exit_code_4(arguments: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as error:
        main(arguments)

    assert error.value.code == 4
    assert "usage:" in capsys.readouterr().err


def test_unexpected_failure_uses_exit_code_4_not_hold(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken_render(*_args: object) -> str:
        raise RuntimeError("template exploded")

    monkeypatch.setattr("release_gate.cli.render_markdown", broken_render)
    report = tmp_path / "report.md"

    result = main(["decide", "--metrics", str(FIXTURE), "--policy", str(POLICY), "--report", str(report)])

    assert result == 4
    assert "no decision produced" in capsys.readouterr().err
    assert not report.exists()


def test_unwritable_output_path_uses_exit_code_4(tmp_path: Path) -> None:
    blocker = tmp_path / "file.txt"
    blocker.write_text("x", encoding="utf-8")

    result = main(["decide", "--metrics", str(FIXTURE), "--policy", str(POLICY), "--out", str(blocker / "decision.json")])

    assert result == 4


def test_metrics_directory_instead_of_file_uses_exit_code_4(tmp_path: Path) -> None:
    assert main(["decide", "--metrics", str(tmp_path), "--policy", str(POLICY)]) == 4


def test_report_writes_markdown(tmp_path: Path) -> None:
    report = tmp_path / "nested" / "report.md"

    result = main(["decide", "--metrics", str(FIXTURE), "--policy", str(POLICY), "--report", str(report)])

    assert result == 0
    assert "Recommendation" in report.read_text(encoding="utf-8")
