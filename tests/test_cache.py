"""Price cache: unadjusted basis, legacy migration, restatement probe.

All tests use a temp cache dir (tmp_cache) and download NVDA bars from
yfinance, straddling its 2024-06-10 10:1 split.
"""

import json
from datetime import date

import pandas as pd


def _seed_legacy_nvda(sdc):
    """Write an old-format (adjusted-basis, no actions columns) cache file."""
    pd.DataFrame(
        {
            "Close": [88.30],
            "High": [92.2],
            "Low": [86.9],
            "Open": [90.2],
            "Volume": [668976000],
        },
        index=pd.DatetimeIndex([pd.Timestamp("2024-03-18")], name="Date"),
    ).to_csv(sdc._csv_path("NVDA"), index_label="Date")
    with open(sdc._meta_path("NVDA"), "w") as f:
        json.dump({"start": "2024-03-11", "end": "2024-03-22"}, f)


def test_legacy_cache_rebuilt_unadjusted(tmp_cache):
    sdc = tmp_cache
    _seed_legacy_nvda(sdc)
    price = sdc.get_price("NVDA", date(2024, 3, 18))
    assert 800 < price < 900  # actual traded close ~884.55, not 88.30 adjusted
    for col in sdc._REQUIRED_COLUMNS:
        assert col in sdc._frames["NVDA"].columns
    start, _ = sdc._read_meta("NVDA")
    assert start == date(2024, 3, 11)  # legacy covered span preserved


def test_split_day_boundary(tmp_cache):
    """Bars before a split are un-adjusted back to traded prices; the split
    day itself is already post-split."""
    sdc = tmp_cache
    sdc.get_history("NVDA", date(2024, 6, 3), date(2024, 6, 14))
    pre = sdc.get_price("NVDA", date(2024, 6, 7))
    post = sdc.get_price("NVDA", date(2024, 6, 10))
    assert 1150 < pre < 1260  # ~1208.88 traded
    assert 110 < post < 135  # ~121.79 post-split
    splits = sdc._frames["NVDA"]["Stock Splits"]
    assert date(2024, 6, 10) in [ts.date() for ts in splits[splits != 0].index]


def test_restatement_probe_triggers_full_refetch(tmp_cache, monkeypatch):
    """A cache whose Adj Close no longer matches Yahoo must be refetched
    whole, never stitched to fresh rows on a different basis."""
    sdc = tmp_cache
    sdc.get_history("NVDA", date(2024, 5, 1), date(2024, 6, 5))

    tampered = pd.read_csv(
        sdc._csv_path("NVDA"), parse_dates=["Date"], index_col="Date"
    )
    tampered["Adj Close"] *= 1.01  # simulate a stale adjustment basis
    tampered.to_csv(sdc._csv_path("NVDA"), index_label="Date")
    with open(sdc._meta_path("NVDA"), "w") as f:
        json.dump({"start": "2024-05-01", "end": "2024-05-31"}, f)
    sdc._frames.clear()
    sdc._metas.clear()
    sdc._series.clear()

    calls = []
    real = sdc._download
    monkeypatch.setattr(sdc, "_download", lambda *a: calls.append(a) or real(*a))
    sdc.get_history("NVDA", date(2024, 5, 1), date(2024, 6, 14))
    assert len(calls) == 2, f"expected probe + full refetch, got {calls}"

    fresh = sdc._frames["NVDA"]["Adj Close"]
    common = fresh.index.intersection(tampered.index)[0]
    ratio = float(fresh.loc[common]) / float(tampered.loc[common, "Adj Close"])
    assert abs(ratio - 1 / 1.01) < 1e-3  # stale rows replaced


def test_covered_query_never_downloads(tmp_cache, monkeypatch):
    sdc = tmp_cache
    sdc.get_history("NVDA", date(2024, 6, 3), date(2024, 6, 14))

    def fail(*args):
        raise AssertionError("covered query must not download")

    monkeypatch.setattr(sdc, "_download", fail)
    sdc.get_history("NVDA", date(2024, 6, 3), date(2024, 6, 14))
    sdc.get_price("NVDA", date(2024, 6, 10))
    sdc.get_price("NVDA", date(2024, 6, 10), column="Adj Close")
