import json
from pathlib import Path

import pytest

from release_gate.cli import main

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "suites/configs/starter-v1.yaml"


def generate_suite(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> Path:
    out_root = tmp_path / "suites"

    result = main(["suite", "generate", "--config", str(CONFIG), "--out-root", str(out_root)])

    output = capsys.readouterr().out
    assert result == 0
    assert "wrote " in output
    assert "(30 cases)" in output
    return out_root / "starter-v1"


def test_suite_generate_validate_and_show(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    suite_dir = generate_suite(tmp_path, capsys)

    result = main(["suite", "validate", str(suite_dir)])
    assert result == 0
    assert "0 issue(s); reviews: 0 approved, 30 candidate, 0 rejected" in capsys.readouterr().out

    result = main(["suite", "show", str(suite_dir), "--split", "dev"])
    shown = capsys.readouterr().out
    assert result == 0
    assert len([line for line in shown.splitlines() if line.startswith("=== ")]) == 15

    first = json.loads((suite_dir / "cases.jsonl").read_text(encoding="utf-8").splitlines()[0])
    result = main(["suite", "show", str(suite_dir), "--case", first["case_id"]])
    shown = capsys.readouterr().out
    assert result == 0
    assert shown.startswith(f"=== {first['case_id']} [")
    assert "expected: category=" in shown

    result = main(["suite", "show", str(suite_dir), "--case", "nope"])
    assert result == 4
    assert capsys.readouterr().err == "error: case nope not found\n"


def test_suite_review_freeze_and_frozen_errors(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    suite_dir = generate_suite(tmp_path, capsys)
    first_case = json.loads((suite_dir / "cases.jsonl").read_text(encoding="utf-8").splitlines()[0])["case_id"]

    result = main(["suite", "review", str(suite_dir), "--case", first_case, "--status", "approved", "--reviewer", "test"])
    assert result == 0
    assert f"{first_case}: approved" in capsys.readouterr().out
    assert main(["suite", "validate", str(suite_dir)]) == 0
    assert "1 approved" in capsys.readouterr().out

    result = main(["suite", "freeze", str(suite_dir)])
    assert result == 4
    assert "not approved" in capsys.readouterr().err

    cases = [json.loads(line) for line in (suite_dir / "cases.jsonl").read_text(encoding="utf-8").splitlines()]
    for case in cases:
        result = main(
            [
                "suite",
                "review",
                str(suite_dir),
                "--case",
                case["case_id"],
                "--status",
                "approved",
                "--reviewer",
                "test",
            ]
        )
        assert result == 0

    result = main(["suite", "freeze", str(suite_dir)])
    assert result == 0
    assert "frozen starter-v1 (30 cases, sha256 " in capsys.readouterr().out

    result = main(["suite", "generate", "--config", str(CONFIG), "--out-root", str(suite_dir.parent)])
    assert result == 4
    assert "frozen" in capsys.readouterr().err

    result = main(["suite", "review", str(suite_dir), "--case", first_case, "--status", "rejected", "--reviewer", "test"])
    assert result == 4
    assert capsys.readouterr().err == "error: suite is frozen\n"


def test_suite_generate_missing_config_returns_usage_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    result = main(["suite", "generate", "--config", str(tmp_path / "missing.yaml"), "--out-root", str(tmp_path)])

    assert result == 4
    assert capsys.readouterr().err.startswith("error:")


def test_malformed_review_file_returns_usage_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    suite_dir = generate_suite(tmp_path, capsys)
    (suite_dir / "review.json").write_text("[1, 2]", encoding="utf-8")

    assert main(["suite", "review", str(suite_dir), "--case", "x", "--status", "approved", "--reviewer", "t"]) == 4
    assert "must map case_id" in capsys.readouterr().err
    assert main(["suite", "generate", "--config", str(CONFIG), "--out-root", str(suite_dir.parent)]) == 4


def test_unexpected_suite_error_returns_4(monkeypatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    def boom(*_args, **_kwargs):
        raise KeyError("boom")

    monkeypatch.setattr("release_gate.cli.read_cases", boom)
    assert main(["suite", "show", str(tmp_path)]) == 4
    assert "internal error: KeyError" in capsys.readouterr().err


def test_suite_without_subcommand_uses_exit_code_4(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as error:
        main(["suite"])

    assert error.value.code == 4
    assert "usage:" in capsys.readouterr().err
