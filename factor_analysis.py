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

import io
import os
import time
import urllib.request
import zipfile

import pandas as pd

FACTOR_CACHE_DIR = "factor_data"
_FRENCH_BASE = "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/"
_FACTORS_ZIP = "F-F_Research_Data_Factors_CSV.zip"
_MOM_ZIP = "F-F_Momentum_Factor_CSV.zip"
_MAX_AGE_SECONDS = 24 * 3600  # refresh at most daily; French updates monthly

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
# Factor data (Ken French Data Library)
# ---------------------------------------------------------------------------
def _download_french_csv(zip_name: str) -> str:
    """Raw CSV text from a French zip, fetched over the network."""
    req = urllib.request.Request(
        _FRENCH_BASE + zip_name, headers={"User-Agent": "Mozilla/5.0"}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        blob = resp.read()
    z = zipfile.ZipFile(io.BytesIO(blob))
    return z.read(z.namelist()[0]).decode("latin-1")


def _cached_french_csv(zip_name: str) -> str:
    """French CSV text, cached under factor_data/ and refreshed at most daily.

    Falls back to a stale cache if the download fails, so a network hiccup
    never breaks an otherwise-offline run.
    """
    os.makedirs(FACTOR_CACHE_DIR, exist_ok=True)
    path = os.path.join(FACTOR_CACHE_DIR, zip_name.replace("_CSV.zip", ".csv"))
    if os.path.exists(path) and time.time() - os.path.getmtime(path) < _MAX_AGE_SECONDS:
        with open(path, encoding="latin-1") as f:
            return f.read()
    try:
        text = _download_french_csv(zip_name)
    except Exception:
        if os.path.exists(path):
            with open(path, encoding="latin-1") as f:
                return f.read()
        raise
    with open(path + ".tmp", "w", encoding="latin-1") as f:
        f.write(text)
    os.replace(path + ".tmp", path)
    return text


def parse_monthly(csv_text: str) -> pd.DataFrame:
    """Monthly block of a French CSV as a PeriodIndex frame of percent values.

    A French file is header prose, then a monthly block keyed by YYYYMM, a
    blank line, then an annual block keyed by YYYY. Rows whose first field is
    exactly six digits are the monthly ones; that filter drops the prose, the
    annual block, and any trailing copyright line. Values stay in percent.
    """
    header = None
    records = {}
    for line in csv_text.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if header is None and parts[0] == "" and len(parts) > 1 and any(parts[1:]):
            header = parts[1:]  # the ",Mkt-RF,SMB,..." column row
            continue
        key = parts[0]
        if len(key) == 6 and key.isdigit():
            records[key] = [float(v) for v in parts[1:]]
    if header is None or not records:
        raise ValueError("could not locate a monthly block in the French CSV")
    idx = pd.PeriodIndex(list(records), freq="M")
    return pd.DataFrame(list(records.values()), index=idx, columns=header).sort_index()


def load_fama_french_factors() -> pd.DataFrame:
    """Monthly French factors + momentum as decimals, indexed by month.

    Columns: Mkt-RF, SMB, HML, RF (from F-F_Research_Data_Factors) and Mom
    (from F-F_Momentum_Factor). Percent figures are divided by 100 so they
    combine directly with the portfolio's decimal monthly returns.
    """
    ff = parse_monthly(_cached_french_csv(_FACTORS_ZIP))  # Mkt-RF SMB HML RF
    mom = parse_monthly(_cached_french_csv(_MOM_ZIP))  # Mom
    return ff.join(mom, how="inner") / 100.0


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
    model = sm.OLS(excess, design).fit()

    means = data[factor_cols].mean()
    betas = {
        col: {
            "beta": float(model.params[col]),
            "se": float(model.bse[col]),
            "t": float(model.tvalues[col]),
            "p": float(model.pvalues[col]),
            # Annualized contribution to average return (attribution).
            "contribution": float(model.params[col] * means[col] * 12),
        }
        for col in factor_cols
    }

    n = int(model.nobs)
    k = len(factor_cols)
    alpha_monthly = float(model.params["const"])
    return {
        "factor_cols": factor_cols,
        "betas": betas,
        "alpha_monthly": alpha_monthly,
        "alpha_annual": alpha_monthly * 12,
        "alpha_t": float(model.tvalues["const"]),
        "alpha_p": float(model.pvalues["const"]),
        "rf_annual": float(data["RF"].mean() * 12),
        "portfolio_annual": float(excess.mean() * 12 + data["RF"].mean() * 12),
        "r_squared": float(model.rsquared),
        "adj_r_squared": float(model.rsquared_adj),
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
    print(f"  {'Factor':<10}{'Beta':>9}{'t-stat':>9}{'Ann. contrib':>14}")
    for col in result["factor_cols"]:
        b = result["betas"][col]
        star = " *" if abs(b["t"]) >= SIGNIFICANT_T else ""
        print(
            f"  {FACTOR_LABELS.get(col, col):<10}{b['beta']:>9.2f}{b['t']:>9.2f}"
            f"{b['contribution'] * 100:>13.2f}%{star}"
        )
    astar = " *" if abs(result["alpha_t"]) >= SIGNIFICANT_T else ""
    print(
        f"  {'Alpha':<10}{result['alpha_annual'] * 100:>8.2f}%"
        f"{result['alpha_t']:>9.2f}{'':>14}{astar}"
    )
    print(
        f"  R-squared {result['r_squared']:.3f}"
        f"  (adj {result['adj_r_squared']:.3f})"
        f"   * = |t| >= {SIGNIFICANT_T}"
    )
