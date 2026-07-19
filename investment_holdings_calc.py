import os

from import_transactions import NormalizedRow, import_csv
import pandas as pd

import yfinance as yf

from datetime import date, timedelta
import math
from decimal import Decimal
import bisect

CASH_SYMBOLS = {"SPAXX", "FDRXX", "SWVXX", "SWVYX", "SWVZX"}


def _fetch_close_prices(ticker: str, start: date, end: date) -> dict:
    """Daily close prices for one ticker over [start, end] from yfinance.

    Returns {date: Decimal(close)} for trading days only. Network/lookup failures
    return {} so the caller can fall back rather than abort the whole backfill.
    """
    try:
        # yfinance's `end` is exclusive, so push it out a day to include `end`.
        data = yf.Ticker(ticker).history(
            start=start.isoformat(),
            end=(end + timedelta(days=1)).isoformat(),
            auto_adjust=False,
        )
    except Exception as e:
        print(f"  yfinance fetch failed for {ticker}: {e}")
        return {}

    if data is None or data.empty or "Close" not in data:
        return {}

    prices = {}
    for ts, close in data["Close"].items():
        if close is None or (isinstance(close, float) and math.isnan(close)):
            continue
        prices[ts.date()] = Decimal(str(round(float(close), 6)))
    return prices


def get_investment_holdings_calendar(
    normalized_rows: list[NormalizedRow],
) -> dict[str, dict[str, Decimal]]:

    date_dict = {}
    running_dict = {}
    for ind in range(len(normalized_rows)):
        row = normalized_rows[ind]

        date_str = row.date
        symbol = row.symbol

        transaction_quantity = row.quantity

        if symbol in CASH_SYMBOLS or symbol.strip() == "":
            symbol = "CASH"

        date_obj = date_dict.setdefault(date_str, running_dict.copy())

        # Every row's Cash Balance reflects cash after that transaction
        # (buys/sells included), so keep CASH current on every row.
        date_obj["CASH"] = row.cash_balance
        running_dict["CASH"] = row.cash_balance

        if symbol == "CASH":
            continue

        previous_quantity = running_dict.get(symbol, 0)

        date_obj[symbol] = previous_quantity + transaction_quantity
        running_dict[symbol] = date_obj[symbol]

        if running_dict[symbol] == 0:
            del running_dict[symbol]

    return date_dict


def print_holdings_calendar(holdings_calendar: dict[str, dict[str, Decimal]]):
    """Prints the holdings calendar in a readable format."""

    holdings_calendar = {k: v for k, v in sorted(holdings_calendar.items())}

    for date_str in sorted(holdings_calendar.keys()):
        # print(f"Date: {date_str}")
        for symbol, quantity in holdings_calendar[date_str].items():
            print(f"  {symbol}: {quantity}")
        # print()  # Add an empty line between dates


def holdings_on_date(
    target_date: date, holdings_calendar: dict[str, dict[str, Decimal]], sorted_dates
):
    i = bisect.bisect_right(sorted_dates, target_date) - 1
    if i < 0:
        return None
    return holdings_calendar[sorted_dates[i]]


def dense_priced_holdings_in_window(
    start_date: date,
    end_date: date,
    holdings_calendar: dict[str, dict[str, Decimal]],
    sorted_dates,
):
    """Returns a list of valued holdings for each day in the window [start_date, end_date]."""
    current_holdings = None

    i = bisect.bisect_right(sorted_dates, start_date) - 1
    if i >= 0:
        current_holdings = holdings_calendar[sorted_dates[i]]

    search_dict = {}  # {symbol: [(start_date, end_date), ...]}

    result = []
    current_date = start_date
    while current_date <= end_date:
        if current_date in holdings_calendar:
            current_holdings = holdings_calendar[current_date]
            prev_holdings = (
                result[-1][1] if result and result[-1][1] is not None else {}
            )

            new_holdings = current_holdings.keys() - prev_holdings.keys()
            sold_holdings = prev_holdings.keys() - current_holdings.keys()

            for symbol in new_holdings:
                if symbol in search_dict and search_dict[symbol][-1][1] is None:
                    continue
                search_dict.setdefault(symbol, []).append((current_date, None))

            for symbol in sold_holdings:
                if symbol in search_dict:
                    if search_dict[symbol][-1][1] is None:
                        search_dict[symbol][-1] = (
                            search_dict[symbol][-1][0],
                            current_date,
                        )
                        break
                    print("Something is very wrong!")

        result.append((current_date, current_holdings))
        current_date += timedelta(days=1)

    loaded_data = {}
    ticker_windows = {}
    for ticker, periods in search_dict.items():
        if ticker == "CASH":
            continue
        for start, end in periods:
            if end is None:
                end = end_date

            unique_key = f"{ticker}_{start}_{end}"
            file_path = f"stock_data/{unique_key}.csv"

            if not os.path.exists(file_path):
                print(f"Downloading {ticker}...")
                df = yf.download(
                    ticker, start=start, end=end, interval="1d", multi_level_index=False
                )
                if df is None:
                    print(
                        f"Failed to download data for {ticker} from {start} to {end}."
                    )
                    continue

                df.to_csv(file_path)
            else:
                print(f"Loading {ticker} from local storage...")
                df = pd.read_csv(file_path, parse_dates=["Date"], index_col="Date")

            loaded_data[unique_key] = df
            ticker_windows.setdefault(ticker, []).append(
                (pd.Timestamp(start), pd.Timestamp(end), unique_key)
            )

    def get_price(ticker, current_date, column="Close"):
        current_date = pd.Timestamp(current_date)

        for start, end, key in ticker_windows.get(ticker, []):
            if start <= date <= end:
                df = loaded_data[key]
                price = df[column].asof(
                    current_date
                )  # last available price on/before date
                if pd.isna(price):
                    raise ValueError(
                        f"No data for {ticker} at or before {current_date.date()} in window {key}"
                    )
                return price

        raise KeyError(
            f"{current_date.date()} not within any loaded window for {ticker}"
        )

    for i, (current_date, holdings) in enumerate(result):
        if holdings is None:
            continue
        priced = {}
        for symbol, quantity in holdings.items():
            if symbol == "CASH":
                priced[symbol] = quantity
                continue
            price = Decimal(str(get_price(symbol, current_date)))
            priced[symbol] = (quantity, price, quantity * price)
        result[i] = (current_date, priced)

    return result


def _load_benchmark_closes(ticker: str, start: date, end: date) -> pd.Series:
    """Daily Close series for a benchmark ticker, cached in stock_data/.

    Starts the download a week early so `asof` has a price even when the
    window opens on a weekend or holiday.
    """
    fetch_start = start - timedelta(days=7)
    unique_key = f"{ticker}_{fetch_start}_{end}"
    file_path = f"stock_data/{unique_key}.csv"

    if not os.path.exists(file_path):
        print(f"Downloading {ticker}...")
        df = yf.download(
            ticker,
            start=fetch_start,
            end=end + timedelta(days=1),  # yfinance `end` is exclusive
            interval="1d",
            multi_level_index=False,
        )
        if df is None or df.empty:
            raise ValueError(f"Failed to download benchmark data for {ticker}")
        df.to_csv(file_path)
    else:
        print(f"Loading {ticker} from local storage...")
        df = pd.read_csv(file_path, parse_dates=["Date"], index_col="Date")

    return df["Close"]


def compare_to_market(priced_holdings, benchmark_ticker: str = "SPY"):
    """Compare the portfolio's time-weighted return against a buy-and-hold benchmark.

    `priced_holdings` is the output of dense_priced_holdings_in_window:
    a list of (date, {"CASH": Decimal, ticker: (qty, price, value), ...}).

    Each day's portfolio return is the previous day's asset weights times each
    asset's price change, so cash contributions and trades only reshuffle the
    weights — they never count as gain or loss. CASH earns 0%.

    Returns (dates, portfolio_curve, benchmark_curve): cumulative growth
    factors starting at 1.0, aligned to `dates`.
    """
    snaps = [(d, h) for d, h in priced_holdings if h]
    if len(snaps) < 2:
        raise ValueError("Need at least two priced snapshots to compute returns.")

    def total_value(holdings):
        return sum(
            payload if symbol == "CASH" else payload[2]
            for symbol, payload in holdings.items()
        )

    dates = [snaps[0][0]]
    portfolio_curve = [1.0]
    growth = 1.0
    for (_, prev), (cur_date, cur) in zip(snaps, snaps[1:]):
        prev_total = total_value(prev)
        day_return = 0.0
        if prev_total > 0:
            for symbol, payload in prev.items():
                if symbol == "CASH":
                    continue  # cash earns 0%
                _, prev_price, value = payload
                # A symbol missing from `cur` was sold today; without a price
                # for it today, count its final day as 0%.
                if symbol in cur and prev_price:
                    cur_price = cur[symbol][1]
                    weight = float(value / prev_total)
                    day_return += weight * (float(cur_price / prev_price) - 1.0)
        growth *= 1.0 + day_return
        dates.append(cur_date)
        portfolio_curve.append(growth)

    closes = _load_benchmark_closes(benchmark_ticker, dates[0], dates[-1])
    base = closes.asof(pd.Timestamp(dates[0]))
    benchmark_curve = [float(closes.asof(pd.Timestamp(d)) / base) for d in dates]

    port_return = (portfolio_curve[-1] - 1) * 100
    bench_return = (benchmark_curve[-1] - 1) * 100
    print(f"\nTime-weighted return {dates[0]} -> {dates[-1]} (contributions excluded):")
    print(f"  Portfolio:        {port_return:+.2f}%")
    print(f"  {benchmark_ticker:<16}  {bench_return:+.2f}%")
    print(f"  vs benchmark:     {port_return - bench_return:+.2f} pts")

    return dates, portfolio_curve, benchmark_curve


if __name__ == "__main__":
    normalized_rows = import_csv(".\\csvs\\fidelity\\roth")
    holdings_calendar = get_investment_holdings_calendar(normalized_rows)

    # print_holdings_calendar(holdings_calendar)

    dates = sorted(holdings_calendar.keys())
    # res = holdings_on_date(date(2026, 7, 18), holdings_calendar, dates)

    start = date(2023, 5, 22)
    end = date(2026, 7, 18)

    priced_holdings = dense_priced_holdings_in_window(
        start, end, holdings_calendar, dates
    )

    for item in priced_holdings:
        print(item)
