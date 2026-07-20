from datetime import date

from dashboard import display_data
from dividend_tracker import (
    audit_dividends,
    dividend_events,
    dividend_summary,
    print_dividend_report,
)
from import_transactions import import_csv
from investment_holdings_calc import (
    audit_splits,
    compare_to_market,
    dense_priced_holdings_in_window,
    get_investment_holdings_calendar,
)


def main():
    normalized_rows = import_csv("csvs/fidelity/roth")
    holdings_calendar = get_investment_holdings_calendar(normalized_rows)

    dates = sorted(holdings_calendar.keys())

    start = dates[0]
    end = min(dates[-1], date.today())

    priced_holdings = dense_priced_holdings_in_window(
        start, end, holdings_calendar, dates
    )

    audit_splits(holdings_calendar, dates, end)

    events = dividend_events(normalized_rows)
    summary = dividend_summary(normalized_rows, events, min(end, date.today()))
    audit_dividends(holdings_calendar, dates, events, end)
    print_dividend_report(events, summary)

    comparisons = {
        ticker: compare_to_market(priced_holdings, ticker) for ticker in ("SPY", "QQQ")
    }

    display_data(
        priced_holdings,
        market_comparisons=comparisons,
        dividend_events=events,
        dividend_summary=summary,
    )


if __name__ == "__main__":
    main()
