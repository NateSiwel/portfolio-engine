"""Per-ticker daily price cache backed by CSV files in stock_data/.

Each ticker gets one CSV (stock_data/<TICKER>.csv) plus a meta file recording
the contiguous calendar range the CSV covers. Queries outside that range
download only the missing head/tail and merge it in, so callers can ask for a
price on any date without knowing what windows were fetched before.

Old-style files named <TICKER>_<start>_<end>.csv are ignored and can be
deleted.
"""

import json
import os
from datetime import date, timedelta

import pandas as pd

CACHE_DIR = "stock_data"

# In-process caches keyed by ticker: loaded CSVs, covered ranges, and column
# Series handed out by get_price (invalidated whenever the frame changes).
_frames: dict[str, pd.DataFrame] = {}
_metas: dict[str, tuple[date, date] | None] = {}
_series: dict[tuple[str, str], pd.Series] = {}


def _csv_path(ticker: str) -> str:
    return os.path.join(CACHE_DIR, f"{ticker}.csv")


def _meta_path(ticker: str) -> str:
    return os.path.join(CACHE_DIR, f"{ticker}.meta.json")


def _set_frame(ticker: str, df: pd.DataFrame) -> None:
    _frames[ticker] = df
    for key in [k for k in _series if k[0] == ticker]:
        del _series[key]


def _download(ticker: str, start: date, end: date) -> pd.DataFrame:
    """Daily bars for [start, end] (inclusive) from yfinance."""
    # Imported lazily: loading yfinance costs ~0.3s, and fully cached runs
    # never need it.
    import yfinance as yf

    print(f"Downloading {ticker} {start}..{end}...")
    df = yf.download(
        ticker,
        start=start,
        end=end + timedelta(days=1),  # yfinance `end` is exclusive
        interval="1d",
        multi_level_index=False,
        progress=False,
    )
    return pd.DataFrame() if df is None else df


def _read_meta(ticker: str) -> tuple[date, date] | None:
    if ticker in _metas:
        return _metas[ticker]
    path = _meta_path(ticker)
    if not os.path.exists(path):
        covered = None
    else:
        with open(path) as f:
            meta = json.load(f)
        covered = date.fromisoformat(meta["start"]), date.fromisoformat(meta["end"])
    _metas[ticker] = covered
    return covered


def _load_cached(ticker: str) -> pd.DataFrame:
    if ticker in _frames:
        return _frames[ticker]
    path = _csv_path(ticker)
    if os.path.exists(path):
        df = pd.read_csv(path, parse_dates=["Date"], index_col="Date")
    else:
        df = pd.DataFrame()
    _set_frame(ticker, df)
    return df


def _save(ticker: str, df: pd.DataFrame, start: date, end: date) -> None:
    os.makedirs(CACHE_DIR, exist_ok=True)
    df.to_csv(_csv_path(ticker), index_label="Date")
    with open(_meta_path(ticker), "w") as f:
        json.dump({"start": start.isoformat(), "end": end.isoformat()}, f)
    _set_frame(ticker, df)
    _metas[ticker] = (start, end)


def _ensure_coverage(ticker: str, start: date, end: date) -> tuple[date, date]:
    """Make [start, end] covered, downloading/merging only the missing part.

    Returns the (start, end) actually usable, with `end` clamped to today.
    """
    today = date.today()
    end = min(end, today)
    if start > end:
        raise ValueError(f"start {start} is after end {end}")

    covered = _read_meta(ticker)
    # Fast path for the per-day pricing loop: already covered, nothing to do.
    if covered is not None and ticker in _frames:
        cov_start, cov_end = covered
        if cov_start <= start and end <= cov_end:
            return start, end

    df = _load_cached(ticker)

    if covered is None or df.empty:
        df = _download(ticker, start, end)
        if df.empty:
            raise ValueError(f"No price data for {ticker} in {start}..{end}")
        cov_start, cov_end = start, end
        downloaded = True
    else:
        cov_start, cov_end = covered
        pieces = [df]
        if start < cov_start:
            pieces.insert(0, _download(ticker, start, cov_start - timedelta(days=1)))
        if end > cov_end:
            pieces.append(_download(ticker, cov_end + timedelta(days=1), end))
        downloaded = len(pieces) > 1
        if downloaded:
            df = pd.concat([p for p in pieces if not p.empty]).sort_index()
            df = df[~df.index.duplicated(keep="last")]
        cov_start = min(start, cov_start)
        cov_end = max(end, cov_end)

    # Today's bar is never marked as durably covered: its close can still
    # change, so the next run refreshes it.
    durable_end = min(cov_end, today - timedelta(days=1))
    if durable_end >= cov_start and (downloaded or (cov_start, durable_end) != covered):
        _save(ticker, df, cov_start, durable_end)
    else:
        _set_frame(ticker, df)

    return start, end


def get_history(ticker: str, start: date, end: date) -> pd.DataFrame:
    """Daily bars for [start, end], downloading only the uncovered part.

    Coverage is calendar-based, so weekends/holidays inside a covered range
    never trigger a re-download.
    """
    start, end = _ensure_coverage(ticker, start, end)
    return _frames[ticker].loc[pd.Timestamp(start) : pd.Timestamp(end)]


def get_price(
    ticker: str, on: date, column: str = "Close", lookback_days: int = 7
) -> float:
    """Price for `ticker` on `on` (last trading day at or before it)."""
    _ensure_coverage(ticker, on - timedelta(days=lookback_days), on)

    key = (ticker, column)
    series = _series.get(key)
    if series is None:
        series = _series[key] = _frames[ticker][column]

    # Last row at or before `on`, skipping NaNs, without slicing a new frame
    # per call — this runs once per ticker per day in the pricing loop.
    pos = int(series.index.searchsorted(pd.Timestamp(on), side="right")) - 1
    while pos >= 0:
        ts, value = series.index[pos], series.iloc[pos]
        if (on - ts.date()).days > lookback_days:
            break
        if not pd.isna(value):
            return float(value)
        pos -= 1

    raise ValueError(
        f"No {column} price for {ticker} within {lookback_days} days before {on}"
    )
