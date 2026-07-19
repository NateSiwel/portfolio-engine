"""Per-ticker daily price cache backed by CSV files in stock_data/.

Each ticker gets one CSV (stock_data/<TICKER>.csv) plus a meta file recording
the contiguous calendar range the CSV covers. Queries outside that range
download only the missing head/tail and merge it in, so callers can ask for a
price on any date without knowing what windows were fetched before.

Prices are stored UNADJUSTED: Close is what actually traded that day
(reconstructed from Yahoo's split-adjusted bars and the full split history),
so cached rows stay valid when a ticker later splits or pays a dividend.
Adj Close, Dividends, and Stock Splits columns are kept alongside for return
math. Because Yahoo restates Adj Close after every corporate action,
head/tail downloads deliberately overlap the cached span by a week; if the
overlap disagrees with the cache, the whole file is refetched.

Files written before the switch to unadjusted prices (no Adj Close/actions
columns) hold adjusted closes and are rebuilt automatically on first use.

Old-style files named <TICKER>_<start>_<end>.csv are ignored and can be
deleted.
"""

import json
import os
from datetime import date, timedelta
from typing import cast
from urllib.parse import quote

import pandas as pd

CACHE_DIR = "stock_data"

# Windows device names that would otherwise survive percent-encoding untouched
# and resolve to devices instead of files (e.g. NUL.csv).
_RESERVED_NAMES = (
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)


def filename_key(ticker: str) -> str:
    """Deterministic, filesystem-safe filename stem for a ticker.

    Percent-encodes everything outside [A-Za-z0-9_.~-], so path separators,
    drive prefixes, and ".." can never escape CACHE_DIR. Real tickers (AAPL,
    BRK.B) pass through unchanged, keeping existing cache files valid.
    """
    key = quote(ticker, safe="")
    if key.split(".")[0].upper() in _RESERVED_NAMES:
        key = f"%{ord(key[0]):02X}{key[1:]}"
    return key


# In-process caches keyed by ticker: loaded CSVs, covered ranges, and column
# Series handed out by get_price (invalidated whenever the frame changes).
_frames: dict[str, pd.DataFrame] = {}
_metas: dict[str, tuple[date, date] | None] = {}
_series: dict[tuple[str, str], pd.Series] = {}


def _csv_path(ticker: str) -> str:
    return os.path.join(CACHE_DIR, f"{filename_key(ticker)}.csv")


def _meta_path(ticker: str) -> str:
    return os.path.join(CACHE_DIR, f"{filename_key(ticker)}.meta.json")


def _set_frame(ticker: str, df: pd.DataFrame) -> None:
    _frames[ticker] = df
    for key in [k for k in _series if k[0] == ticker]:
        del _series[key]


def _unadjust_splits(df: pd.DataFrame, splits: pd.Series) -> pd.DataFrame:
    """Restore actual traded prices from Yahoo's split-adjusted bars.

    Yahoo's OHLC is split-adjusted at the source (auto_adjust only controls
    dividend adjustment), so each bar is multiplied back by the ratio of
    every split that happened after it. `splits` must be the ticker's full
    split history as of the same fetch, which makes the result invariant to
    when the download happened. Volume is left on Yahoo's basis (unused).
    """
    if splits is None or splits.empty:
        return df
    if cast(pd.DatetimeIndex, splits.index).tz is not None:
        splits = splits.tz_localize(None)
    factor = pd.Series(1.0, index=df.index)
    days = cast(pd.DatetimeIndex, df.index).normalize()
    for ts, ratio in splits.items():
        if ratio:
            # The bar on the split day itself is already post-split; event
            # timestamps carry a time-of-day, so compare whole days.
            factor.loc[days < cast(pd.Timestamp, ts).normalize()] *= float(ratio)
    for col in ("Open", "High", "Low", "Close"):
        if col in df.columns:
            df[col] = df[col] * factor
    return df


def _download(ticker: str, start: date, end: date) -> pd.DataFrame:
    """Daily bars for [start, end] (inclusive) from yfinance, unadjusted.

    Close is the price that actually traded that day (see _unadjust_splits);
    Adj Close stays split+dividend adjusted for return math.

    Empty means Yahoo answered that no bars exist in the range (weekend or
    holiday gap, pre-IPO, delisted). Failed requests raise instead, so a
    network hiccup is never mistaken for an empty range.
    """
    # Imported lazily: loading yfinance costs ~0.3s, and fully cached runs
    # never need it.
    import yfinance as yf
    from yfinance.exceptions import YFPricesMissingError, YFTzMissingError

    yf.config.debug.hide_exceptions = False

    print(f"Downloading {ticker} {start}..{end}...")
    tk = yf.Ticker(ticker)
    try:
        df = tk.history(
            start=start,
            end=end + timedelta(days=1),  # yfinance `end` is exclusive
            interval="1d",
            auto_adjust=False,  # keep Yahoo's Close/Adj Close distinction
        )
    except (YFPricesMissingError, YFTzMissingError):
        return pd.DataFrame()
    if df is None or df.empty:
        return pd.DataFrame()
    return _unadjust_splits(df.tz_localize(None), tk.splits)


# Columns every cache file carries since the switch to unadjusted prices.
# A file missing any of them was written on the adjusted basis and its Close
# values are wrong for any pre-split date, so it must be rebuilt.
_REQUIRED_COLUMNS = ("Close", "Adj Close", "Dividends", "Stock Splits")


def _history_restated(cached: pd.DataFrame, fresh: pd.DataFrame) -> bool:
    """True when rows both frames cover disagree, i.e. Yahoo restated history.

    Adj Close is restated after every dividend/split; Close only on data
    corrections. Either way the cached rows are stale as a whole.
    """
    common = cached.index.intersection(fresh.index)
    if common.empty:
        return False
    for col in ("Close", "Adj Close"):
        a, b = cached.loc[common, col], fresh.loc[common, col]
        if ((a - b).abs() > b.abs() * 1e-4 + 1e-6).any():
            return True
    return False


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
    csv_path = _csv_path(ticker)
    df.to_csv(csv_path + ".tmp", index_label="Date")
    os.replace(csv_path + ".tmp", csv_path)
    meta_path = _meta_path(ticker)
    with open(meta_path + ".tmp", "w") as f:
        json.dump({"start": start.isoformat(), "end": end.isoformat()}, f)
    os.replace(meta_path + ".tmp", meta_path)
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

    legacy_span = None
    if covered is not None and any(c not in df.columns for c in _REQUIRED_COLUMNS):
        # Adjusted-basis file from before the switch to unadjusted prices.
        legacy_span = covered
        covered = None

    if covered is None or df.empty:
        cov_start, cov_end = start, end
        if legacy_span is not None:
            # Rebuild the whole previously covered span, not just the query.
            cov_start = min(cov_start, legacy_span[0])
            cov_end = min(max(cov_end, legacy_span[1]), today)
        df = _download(ticker, cov_start, cov_end)
        if df.empty:
            raise ValueError(f"No price data for {ticker} in {cov_start}..{cov_end}")
        downloaded = True
    else:
        cov_start, cov_end = covered
        # Overlap downloads a week into the covered span so a restatement
        # (split/dividend since the file was written) is caught by comparing
        # the overlap rows against the cache.
        overlap = timedelta(days=7)
        head = tail = None
        if start < cov_start:
            head = _download(ticker, start, min(cov_start + overlap, cov_end))
        if end > cov_end:
            tail = _download(ticker, max(cov_end - overlap, cov_start), end)
        if (head is not None and _history_restated(df, head)) or (
            tail is not None and _history_restated(df, tail)
        ):
            # Every cached row is on a stale basis: refetch the whole span.
            cov_start = min(start, cov_start)
            cov_end = max(end, cov_end)
            df = _download(ticker, cov_start, cov_end)
            if df.empty:
                raise ValueError(
                    f"No price data for {ticker} in {cov_start}..{cov_end}"
                )
            downloaded = True
        else:
            pieces = [p for p in (head, df, tail) if p is not None and not p.empty]
            downloaded = head is not None or tail is not None
            if downloaded:
                df = pd.concat(pieces).sort_index()
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
