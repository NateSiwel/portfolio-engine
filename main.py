from datetime import date

from dashboard import display_data
from import_transactions import import_csv
from investment_holdings_calc import (
    compare_to_market,
    dense_priced_holdings_in_window,
    get_investment_holdings_calendar,
)


def main():
    normalized_rows = import_csv("csvs/fidelity/roth")
    holdings_calendar = get_investment_holdings_calendar(normalized_rows)

    dates = sorted(holdings_calendar.keys())

    start = date(2023, 1, 18)
    end = date(2026, 7, 18)

    priced_holdings = dense_priced_holdings_in_window(
        start, end, holdings_calendar, dates
    )

    comparisons = {
        ticker: compare_to_market(priced_holdings, ticker) for ticker in ("SPY", "QQQ")
    }

    display_data(priced_holdings, market_comparisons=comparisons)


if __name__ == "__main__":
    main()
