from import_transactions import NormalizedRow, import_csv
from stock_data_cache import get_history, get_price
import pandas as pd

from datetime import date, timedelta
from typing import cast
from decimal import Decimal
import bisect

CASH_SYMBOLS = {"SPAXX", "FDRXX", "SWVXX", "SWVYX", "SWVZX"}


def get_investment_holdings_calendar(
    normalized_rows: list[NormalizedRow],
) -> dict[date, dict[str, Decimal]]:

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

        # A row's Cash Balance reflects cash after that transaction
        # (buys/sells included), so keep CASH current on every row that has
        # one. Rows without a balance either carry a signed cash amount
        # (banks with no running-balance column) or are pure share moves
        # like split distributions, which must not touch CASH.
        if row.cash_balance is not None:
            new_cash = row.cash_balance
        elif row.amount:
            new_cash = running_dict.get("CASH", Decimal(0)) + row.amount
        else:
            new_cash = None
        if new_cash is not None:
            date_obj["CASH"] = new_cash
            running_dict["CASH"] = new_cash

        if symbol == "CASH":
            continue

        previous_quantity = running_dict.get(symbol, 0)

        date_obj[symbol] = previous_quantity + transaction_quantity
        running_dict[symbol] = date_obj[symbol]

        if running_dict[symbol] == 0:
            del running_dict[symbol]

    return date_dict


def print_holdings_calendar(holdings_calendar: dict[date, dict[str, Decimal]]):
    """Prints the holdings calendar in a readable format."""

    holdings_calendar = {k: v for k, v in sorted(holdings_calendar.items())}

    for date_str in sorted(holdings_calendar.keys()):
        # print(f"Date: {date_str}")
        for symbol, quantity in holdings_calendar[date_str].items():
            print(f"  {symbol}: {quantity}")
        # print()  # Add an empty line between dates


def holdings_on_date(
    target_date: date,
    holdings_calendar: dict[date, dict[str, Decimal]],
    sorted_dates: list[date],
):
    i = bisect.bisect_right(sorted_dates, target_date) - 1
    if i < 0:
        return None
    return holdings_calendar[sorted_dates[i]]


def dense_priced_holdings_in_window(
    start_date: date,
    end_date: date,
    holdings_calendar: dict[date, dict[str, Decimal]],
    sorted_dates: list[date],
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
                    else:
                        print("Something is very wrong!")

        result.append((current_date, current_holdings))
        current_date += timedelta(days=1)

    # Warm the per-ticker cache over each ticker's full ownership span so the
    # per-day pricing loop below never triggers a download.
    for ticker, periods in search_dict.items():
        if ticker == "CASH":
            continue
        # Periods are chronological; a None end means still held at end_date.
        first_owned = periods[0][0]
        last_owned = periods[-1][1] or end_date
        # A week of padding matches get_price's asof lookback.
        get_history(ticker, first_owned - timedelta(days=7), last_owned)

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
    """Daily Close series for a benchmark ticker, from the shared price cache.

    Starts a week early so `asof` has a price even when the window opens on a
    weekend or holiday.
    """
    return get_history(ticker, start - timedelta(days=7), end)["Close"]


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

    def close_at(d: date) -> float:
        # asof is typed as returning any pandas scalar; Close is always numeric.
        return float(cast(float, closes.asof(pd.Timestamp(d))))

    base = close_at(dates[0])
    benchmark_curve = [close_at(d) / base for d in dates]

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
