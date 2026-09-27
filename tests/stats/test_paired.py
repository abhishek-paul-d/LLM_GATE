"""Paired bootstrap and McNemar: determinism, A/A behaviour, known values."""

from __future__ import annotations

import pytest

from release_gate.stats import PairedSeries, derive_seed, mcnemar_exact, paired_bootstrap, quantile, statistic

B = [float(i % 3 == 0) for i in range(60)]
C = [float(i % 4 == 0) for i in range(60)]


def boot(series, seed=1234, resamples=2000, confidence=0.95):
    return paired_bootstrap(series, resamples=resamples, seed=seed, confidence=confidence)


def test_quantile_matches_linear_interpolation():
    assert quantile([1, 2, 3, 4], 0.5) == 2.5
    assert quantile([10.0], 0.95) == 10.0
    assert quantile(list(range(101)), 0.95) == 95.0
    assert statistic([1, 2, 3, 4, 100], "p50") == 3
    with pytest.raises(ValueError):
        quantile([], 0.5)


def test_identical_models_have_a_zero_width_interval():
    """A/A: every paired difference is zero, so the CI is exactly [0, 0] and never fails a drop limit."""
    values = [1.0, 0.0, 1.0, 1.0, 0.0] * 6
    latency = [100.0 + i for i in range(30)]
    out = boot({"accuracy": PairedSeries(values, values), "p95": PairedSeries(latency, latency, "p95")})
    assert out["accuracy"].delta_ci == (0.0, 0.0) and out["p95"].delta_ci == (0.0, 0.0)
    assert out["accuracy"].baseline == out["accuracy"].candidate == 0.6 and out["accuracy"].n == 30


def test_same_seed_same_interval_and_seeds_matter():
    s = {"m": PairedSeries(B, C)}
    assert boot(s) == boot(s)
    assert boot(s)["m"].delta_ci != boot(s, seed=99)["m"].delta_ci


def test_golden_interval():
    """Pins the resampling scheme: changing RNG use or the quantile method would change saved decisions."""
    out = boot({"m": PairedSeries(B, C)}, resamples=1000, seed=7)["m"]
    assert (out.baseline, out.candidate, out.n) == (pytest.approx(20 / 60), 0.25, 60)
    assert out.delta_ci == GOLDEN_CI


GOLDEN_CI = (-0.25, 0.066666666667)


def test_clear_regression_is_detected():
    ci = boot({"m": PairedSeries([1.0] * 50, [1.0] * 30 + [0.0] * 20)})["m"].delta_ci
    assert ci[1] < 0


def test_p95_shift_is_detected():
    b = [100.0 + i for i in range(40)]
    out = boot({"p95": PairedSeries(b, [x * 2 for x in b], "p95")})["p95"]
    assert out.candidate == pytest.approx(2 * out.baseline) and out.delta_ci[0] > 0


def test_empty_case_set_has_no_interval():
    out = boot({"m": PairedSeries([], [])})["m"]
    assert out.n == 0 and out.delta_ci is None


def test_input_errors():
    with pytest.raises(ValueError, match="length"):
        PairedSeries([1.0], [1.0, 0.0])
    with pytest.raises(ValueError, match="different case sets"):
        boot({"a": PairedSeries([1.0], [1.0]), "b": PairedSeries([1.0, 0.0], [1.0, 0.0])})
    with pytest.raises(ValueError, match="statistic"):
        PairedSeries([1.0], [1.0], "p42")  # type: ignore[arg-type]


def test_derive_seed_is_stable_and_label_specific():
    assert derive_seed(1234, "all") == derive_seed(1234, "all")
    assert derive_seed(1234, "all") != derive_seed(1234, "slice:missing_evidence")


def test_mcnemar_exact():
    assert mcnemar_exact([True] * 5, [True] * 5).p_value == 1.0
    m = mcnemar_exact([False] * 5, [True] * 5)
    assert (m.baseline_only, m.candidate_only) == (0, 5) and m.p_value == pytest.approx(0.0625)
    assert mcnemar_exact([True, False, True, False], [False, True, False, True]).p_value == 1.0
    with pytest.raises(ValueError):
        mcnemar_exact([True], [True, False])
