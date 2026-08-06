from datetime import date

from dashboard import display_data
from dividend_tracker import (
    audit_dividends,
    dividend_events,
    dividend_summary,
    print_dividend_report,
)
from factor_analysis import factor_analysis, print_factor_report
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
    end = min(dates[-1], date.today())  # noqa: DTZ011 - local calendar date is the intended "today"

    priced_holdings = dense_priced_holdings_in_window(
        start, end, holdings_calendar, dates
    )

    audit_splits(holdings_calendar, dates, end)

    events = dividend_events(normalized_rows)
    summary = dividend_summary(normalized_rows, events, min(end, date.today()))  # noqa: DTZ011 - local calendar date is the intended "today"
    audit_dividends(holdings_calendar, dates, events, end)
    print_dividend_report(events, summary)

    comparisons = {
        ticker: compare_to_market(priced_holdings, ticker) for ticker in ("SPY", "QQQ")
    }

    # Factor exposure analysis on the portfolio's time-weighted return curve.
    # Best-effort: a cold/blocked French download or too-short history should
    # never sink the rest of the dashboard.
    factor_result = None
    try:
        cmp_dates, port_curve, _ = comparisons["SPY"]
        factor_result = factor_analysis(cmp_dates, port_curve)
        print_factor_report(factor_result)
    except Exception as exc:  # noqa: BLE001 - best-effort: any failure here must not sink the dashboard
        print(f"Factor analysis skipped: {exc}")

    display_data(
        priced_holdings,
        market_comparisons=comparisons,
        dividend_events=events,
        dividend_summary=summary,
        factor_result=factor_result,
    )


if __name__ == "__main__":
    main()
