"""Cached loader for the Ken French Data Library monthly factor files.

A caching layer over the French library the way stock_data_cache is one over
yfinance: it fetches the factor/momentum zips over the network, keeps the
extracted CSV text under factor_data/, refreshes at most daily (French updates
monthly), and falls back to a stale cache when the download fails — so an
otherwise-offline run still returns factors.

The public surface is load_fama_french_factors(), which returns the merged
monthly factors as decimals ready for regression; parse_monthly() is exposed
for callers that already hold the raw CSV text (and for testing the parser
without a network round-trip).
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
