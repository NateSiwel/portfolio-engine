"""Fama-French / momentum factor exposure analysis for the portfolio.

Regresses the portfolio's monthly excess returns on the Fama-French
market/size/value factors plus momentum (Ken French Data Library), following
Meketa's "Factor Exposure Analysis" note: it reports each factor beta with its
t-stat, the annualized alpha, R-squared, and a return attribution.

Both sides of the regression are simple (arithmetic) monthly returns on the
same basis: the factor returns are French's monthly percent figures (converted
to decimals here), and the portfolio return is the month-over-month change in
the time-weighted growth curve from compare_to_market — so contributions are
already excluded and dividends/splits already handled.

Scope note: the paper's Quality (QMJ) and Low Beta (BAB) factors are AQR
series, not French, and are left for a later pass. This v1 ships the four
factors that come free from the French library (Market, Size, Value,
Momentum), which is also the more robust choice for a short return history.
"""

import pandas as pd

from french_data import load_fama_french_factors

# Report labels keyed by the column names in the merged factor frame.
FACTOR_LABELS = {
    "Mkt-RF": "Market",
    "SMB": "Size",
    "HML": "Value",
    "Mom": "Momentum",
}
DEFAULT_FACTORS = ("Mkt-RF", "SMB", "HML", "Mom")

# Paper's rule of thumb (footnote 5): the residual degrees of freedom,
# n - k - 1, should be at least ~30 for the betas to be trustworthy.
MIN_RESIDUAL_DOF = 30
# |t| above this (95% confidence) is the paper's bar for statistical significance.
SIGNIFICANT_T = 1.96


# ---------------------------------------------------------------------------
# Portfolio returns
# ---------------------------------------------------------------------------
def portfolio_monthly_returns(dates, portfolio_curve) -> pd.Series:
    """Monthly simple returns from compare_to_market's daily growth curve.

    Takes the growth factor on the last available day of each month and
    returns its month-over-month change, indexed by month (PeriodIndex). The
    inception-to-first-month-end stub can't form a full-month return and is
    dropped by pct_change, which is what we want.
    """
    curve = pd.Series(
        [float(g) for g in portfolio_curve], index=pd.to_datetime(list(dates))
    ).sort_index()
    month_end = curve.resample("ME").last()
    monthly = month_end.pct_change().dropna()
    monthly.index = monthly.index.to_period("M")
    return monthly


# ---------------------------------------------------------------------------
# Regression
# ---------------------------------------------------------------------------
def _hac_maxlags(n: int) -> int:
    """Newey-West automatic lag length, floor(4 * (n/100) ** (2/9)), min 1.

    The standard rule of thumb (Newey & West 1994) for how many lags of
    autocorrelation to correct for; grows slowly with the sample.
    """
    return max(1, int(4 * (n / 100) ** (2 / 9)))


def run_regression(
    port_monthly: pd.Series,
    factors: pd.DataFrame,
    factor_cols=DEFAULT_FACTORS,
) -> dict:
    """OLS of portfolio excess return on the chosen factors.

    Model: (Rp - Rf) = alpha + sum_i beta_i * factor_i + eps, estimated over
    the months where the portfolio and every factor overlap. Returns a dict
    of the fitted parameters, fit quality, and an annualized return
    attribution (each factor's beta times its mean return, times 12).

    Standard errors (and hence t-stats, p-values, and confidence intervals)
    use the Newey-West HAC estimator, which corrects for the mild
    autocorrelation and heteroskedasticity of monthly factor returns; the
    point estimates (betas, alpha, R-squared) are unchanged from plain OLS.
    """
    import statsmodels.api as sm

    factor_cols = list(factor_cols)
    data = (
        factors[factor_cols + ["RF"]]
        .join(port_monthly.rename("port"), how="inner")
        .dropna()
    )
    if len(data) <= len(factor_cols) + 1:
        raise ValueError(
            f"need more than {len(factor_cols) + 1} overlapping months to fit "
            f"{len(factor_cols)} factors; got {len(data)}"
        )

    excess = data["port"] - data["RF"]
    design = sm.add_constant(data[factor_cols])
    maxlags = _hac_maxlags(len(data))
    model = sm.OLS(excess, design).fit(cov_type="HAC", cov_kwds={"maxlags": maxlags})
    conf = model.conf_int()  # 95% CI per parameter, rows indexed by name

    means = data[factor_cols].mean()
    betas = {
        col: {
            "beta": float(model.params[col]),
            "se": float(model.bse[col]),
            "t": float(model.tvalues[col]),
            "p": float(model.pvalues[col]),
            "ci_low": float(conf.loc[col, 0]),
            "ci_high": float(conf.loc[col, 1]),
            # Annualized contribution to average return (attribution).
            "contribution": float(model.params[col] * means[col] * 12),
        }
        for col in factor_cols
    }

    n = int(model.nobs)
    k = len(factor_cols)
    alpha_monthly = float(model.params["const"])
    # Idiosyncratic (residual) risk: the standard error of the regression,
    # annualized — the return variation the factors don't explain.
    resid_vol_annual = float((model.mse_resid**0.5) * (12**0.5))
    # Information ratio: annualized alpha per unit of that residual risk.
    alpha_annual = alpha_monthly * 12
    info_ratio = alpha_annual / resid_vol_annual if resid_vol_annual else float("nan")
    return {
        "factor_cols": factor_cols,
        "betas": betas,
        "alpha_monthly": alpha_monthly,
        "alpha_annual": alpha_annual,
        "alpha_t": float(model.tvalues["const"]),
        "alpha_p": float(model.pvalues["const"]),
        "alpha_ci_low": float(conf.loc["const", 0]),
        "alpha_ci_high": float(conf.loc["const", 1]),
        "rf_annual": float(data["RF"].mean() * 12),
        "portfolio_annual": float(excess.mean() * 12 + data["RF"].mean() * 12),
        "r_squared": float(model.rsquared),
        "adj_r_squared": float(model.rsquared_adj),
        # Overall fit: robust Wald F-test that all betas are jointly zero.
        "f_stat": float(model.fvalue),
        "f_pvalue": float(model.f_pvalue),
        "resid_vol_annual": resid_vol_annual,
        "info_ratio": info_ratio,
        "hac_maxlags": maxlags,
        "n": n,
        "k": k,
        "residual_dof": n - k - 1,
        "underpowered": (n - k - 1) < MIN_RESIDUAL_DOF,
        "start": data.index.min(),
        "end": data.index.max(),
    }


def factor_analysis(dates, portfolio_curve, factor_cols=DEFAULT_FACTORS) -> dict:
    """End-to-end: portfolio growth curve -> factor regression result dict.

    `dates` and `portfolio_curve` are the first two elements of a
    compare_to_market return value (the benchmark curve is unused here).
    """
    port_monthly = portfolio_monthly_returns(dates, portfolio_curve)
    return run_regression(port_monthly, load_fama_french_factors(), factor_cols)


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
def print_factor_report(result: dict) -> None:
    """Print the regression table à la the paper's Table 1."""
    print(
        f"\nFactor exposure analysis  ({result['start']} - {result['end']}, "
        f"{result['n']} monthly returns)"
    )
    if result["underpowered"]:
        print(
            f"  WARNING: residual dof {result['residual_dof']} < {MIN_RESIDUAL_DOF}"
            " — too little history for stable betas; treat as indicative only."
        )
    print(f"  {'Factor':<10}{'Beta':>9}{'t-stat':>9}{'95% CI':>18}{'Ann. contrib':>14}")
    for col in result["factor_cols"]:
        b = result["betas"][col]
        star = " *" if abs(b["t"]) >= SIGNIFICANT_T else ""
        ci = f"[{b['ci_low']:+.2f}, {b['ci_high']:+.2f}]"
        print(
            f"  {FACTOR_LABELS.get(col, col):<10}{b['beta']:>9.2f}{b['t']:>9.2f}"
            f"{ci:>18}{b['contribution'] * 100:>13.2f}%{star}"
        )
    astar = " *" if abs(result["alpha_t"]) >= SIGNIFICANT_T else ""
    alpha_ci = (
        f"[{result['alpha_ci_low'] * 12 * 100:+.1f}, "
        f"{result['alpha_ci_high'] * 12 * 100:+.1f}]%"
    )
    print(
        f"  {'Alpha':<10}{result['alpha_annual'] * 100:>8.2f}%"
        f"{result['alpha_t']:>9.2f}{alpha_ci:>18}{'':>14}{astar}"
    )
    print(
        f"  R-squared {result['r_squared']:.3f}"
        f"  (adj {result['adj_r_squared']:.3f})"
        f"   F({result['k']},{result['residual_dof']}) {result['f_stat']:.2f}"
        f"  (p {result['f_pvalue']:.3f})"
    )
    print(
        f"  Residual vol {result['resid_vol_annual'] * 100:.1f}%/yr"
        f"   Information ratio {result['info_ratio']:.2f}"
        f"   * = |t| >= {SIGNIFICANT_T}  (Newey-West SE, {result['hac_maxlags']} lags)"
    )
