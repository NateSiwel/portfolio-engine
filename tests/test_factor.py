"""Factor exposure analysis: parsing, monthly returns, and the regression.

These are offline — they build synthetic factors/returns rather than hitting
the Ken French library, so the regression math is checked deterministically.
"""

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from factor_analysis import (
    DEFAULT_FACTORS,
    SIGNIFICANT_T,
    parse_monthly,
    portfolio_monthly_returns,
    run_regression,
)

# --- French CSV parsing ----------------------------------------------------
SAMPLE_FRENCH_CSV = """This file was created using the 202606 CRSP database.
Some more prose about the T-bill rate.

,Mkt-RF,SMB,HML,RF
192607,   2.89,  -2.55,  -2.39,   0.22
192608,   2.64,  -1.14,   3.81,   0.25
202605,   1.10,   0.40,  -0.30,   0.35

  Annual Factors: January-December
1927,  29.47,  -2.44,  -4.34,   3.12
1928,  35.39,   4.36,  -6.29,   3.56

Copyright 2026 Kenneth R. French
"""


def test_parse_monthly_takes_only_the_monthly_block():
    df = parse_monthly(SAMPLE_FRENCH_CSV)
    assert list(df.columns) == ["Mkt-RF", "SMB", "HML", "RF"]
    # Six-digit YYYYMM rows only; the four-digit annual rows are excluded.
    assert len(df) == 3
    assert str(df.index[0]) == "1926-07"
    assert df.loc[pd.Period("2026-05", "M"), "Mkt-RF"] == 1.10
    # Sorted ascending regardless of file order.
    assert df.index.is_monotonic_increasing


# --- Monthly return resampling ---------------------------------------------
def test_portfolio_monthly_returns_from_growth_curve():
    # Dense daily growth curve across three month boundaries.
    start = date(2024, 1, 15)
    days = [start + timedelta(days=i) for i in range(75)]  # into late March
    # Constant 0.1%/day compounding so month-end values are known.
    curve = [(1.001) ** i for i in range(len(days))]

    monthly = portfolio_monthly_returns(days, curve)

    # Jan is a partial stub (dropped); Feb and Mar are full months.
    assert list(monthly.index.astype(str)) == ["2024-02", "2024-03"]

    # Rebuild the expected month-over-month change directly from the curve.
    s = pd.Series(curve, index=pd.to_datetime(days))
    expected = s.resample("ME").last().pct_change().dropna()
    np.testing.assert_allclose(monthly.values, expected.values, rtol=1e-12)


# --- Regression ------------------------------------------------------------
def _synthetic_factors(n_months: int, seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    idx = pd.period_range("2015-01", periods=n_months, freq="M")
    return pd.DataFrame(
        {
            "Mkt-RF": rng.normal(0.005, 0.04, n_months),
            "SMB": rng.normal(0.0, 0.02, n_months),
            "HML": rng.normal(0.0, 0.02, n_months),
            "Mom": rng.normal(0.0, 0.03, n_months),
            "RF": np.full(n_months, 0.002),
        },
        index=idx,
    )


def test_regression_recovers_known_betas():
    factors = _synthetic_factors(60)
    true_beta = {"Mkt-RF": 0.9, "SMB": 0.3, "HML": 0.2, "Mom": 0.1}
    true_alpha_monthly = 0.001

    rng = np.random.default_rng(42)
    excess = (
        true_alpha_monthly
        + sum(true_beta[c] * factors[c] for c in true_beta)
        + rng.normal(0, 1e-4, len(factors))  # tiny noise -> near-exact recovery
    )
    port = excess + factors["RF"]  # regression re-subtracts RF

    result = run_regression(port, factors, DEFAULT_FACTORS)

    for col, expected in true_beta.items():
        assert result["betas"][col]["beta"] == pytest.approx(expected, abs=0.02)
    assert result["alpha_monthly"] == pytest.approx(true_alpha_monthly, abs=5e-4)
    assert result["alpha_annual"] == pytest.approx(true_alpha_monthly * 12, abs=6e-3)
    assert result["r_squared"] > 0.99
    assert result["n"] == 60
    assert result["k"] == 4
    assert result["residual_dof"] == 55
    assert result["underpowered"] is False
    # Market beta is unmistakably significant at this signal level.
    assert abs(result["betas"]["Mkt-RF"]["t"]) > SIGNIFICANT_T


def test_underpowered_flag_on_short_history():
    factors = _synthetic_factors(34)
    port = 0.9 * factors["Mkt-RF"] + factors["RF"]
    result = run_regression(port, factors, DEFAULT_FACTORS)
    # 34 - 4 - 1 = 29 < 30
    assert result["residual_dof"] == 29
    assert result["underpowered"] is True


def test_too_few_months_raises():
    factors = _synthetic_factors(4)
    port = factors["Mkt-RF"] + factors["RF"]
    with pytest.raises(ValueError):
        run_regression(port, factors, DEFAULT_FACTORS)


def test_attribution_reconstructs_average_return():
    factors = _synthetic_factors(48, seed=7)
    rng = np.random.default_rng(1)
    excess = 0.001 + 0.8 * factors["Mkt-RF"] + 0.2 * factors["SMB"]
    excess = excess + rng.normal(0, 1e-4, len(factors))
    port = excess + factors["RF"]
    result = run_regression(port, factors, DEFAULT_FACTORS)

    # alpha + sum(factor contributions) + risk-free ~= portfolio avg annual return.
    rebuilt = (
        result["alpha_annual"]
        + sum(result["betas"][c]["contribution"] for c in result["factor_cols"])
        + result["rf_annual"]
    )
    assert rebuilt == pytest.approx(result["portfolio_annual"], rel=1e-9, abs=1e-9)
