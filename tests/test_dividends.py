"""Dividend extraction, income math, yield-on-cost, projection, and the
receipts audit, over a real year of AAPL dividends (2024: $0.24 + 3 x $0.25).

Uses the project's stock_data/ cache (created on first run).
"""

import os
from datetime import date
from decimal import Decimal

import pytest

from dividend_tracker import (
    audit_dividends,
    cost_basis_by_ticker,
    dividend_events,
    dividend_summary,
    income_by_period,
    income_by_ticker,
    ttm_dividends_per_share,
)
from import_transactions import import_csv
from investment_holdings_calc import get_investment_holdings_calendar

ASOF = date(2024, 12, 31)


@pytest.fixture(scope="module")
def rows(fixtures_dir):
    return import_csv(os.path.join(fixtures_dir, "fidelity", "dividends"))


@pytest.fixture(scope="module")
def events(rows):
    return dividend_events(rows)


def test_dividend_events_extraction(events):
    # 4 AAPL receipts + 1 SPAXX payout; the REINVESTMENT row is not income.
    assert len(events) == 5
    aapl = events[events["symbol"] == "AAPL"]
    assert float(aapl["amount"].sum()) == pytest.approx(9.90)
    # Money-market payouts group under CASH, matching the holdings ledger.
    assert set(events["symbol"]) == {"AAPL", "CASH"}
    # Only the November dividend was reinvested (same-day REINVESTMENT row).
    reinvested = events[events["reinvested"]]
    assert len(reinvested) == 1
    assert reinvested.iloc[0]["date"].date() == date(2024, 11, 14)


def test_income_by_period_and_ticker(events):
    yearly = income_by_period(events, "Y")
    assert float(yearly.loc["2024", "Total"]) == pytest.approx(13.90)

    monthly = income_by_period(events, "M")
    assert float(monthly.loc["2024-02", "AAPL"]) == pytest.approx(2.40)
    assert float(monthly.loc["2024-01", "CASH"]) == pytest.approx(4.00)
    assert float(monthly["Total"].sum()) == pytest.approx(13.90)

    by_ticker = income_by_ticker(events)
    assert by_ticker.index[0] == "AAPL"
    assert float(by_ticker["CASH"]) == pytest.approx(4.00)


def test_cost_basis_includes_reinvestment(rows):
    basis = cost_basis_by_ticker(rows)
    shares, cost = basis["AAPL"]
    assert shares == Decimal("10.011")
    assert cost == Decimal("1852.50")  # buy 1850 + reinvested 2.50


def test_cost_basis_survives_split_and_prorates_sells(fixtures_dir):
    # split_test: buy 2 NVDA for $980, 10:1 split (18 more shares), sell 5.
    basis = cost_basis_by_ticker(
        import_csv(os.path.join(fixtures_dir, "fidelity", "split_test"))
    )
    shares, cost = basis["NVDA"]
    assert shares == Decimal(15)
    # Split adds shares at no cost; selling 5 of 20 removes a quarter of it.
    assert cost == Decimal("735")


def test_summary_yield_on_cost_and_projection(rows, events):
    summary = dividend_summary(rows, events, ASOF)
    aapl = summary[summary["symbol"] == "AAPL"].iloc[0]

    # AAPL paid $0.99/share over 2024, so the TTM rate must land there.
    assert ttm_dividends_per_share("AAPL", ASOF) == pytest.approx(0.99, abs=0.02)
    assert aapl["projected_income"] == pytest.approx(aapl["shares"] * aapl["ttm_dps"])
    assert aapl["yield_on_cost"] == pytest.approx(
        aapl["projected_income"] / aapl["cost_basis"] * 100
    )
    assert aapl["current_yield"] == pytest.approx(aapl["ttm_dps"] / aapl["price"] * 100)
    assert aapl["ttm_received"] == pytest.approx(9.90)
    assert aapl["total_received"] == pytest.approx(9.90)
    # CASH has no cost basis and never appears in the outlook.
    assert "CASH" not in set(summary["symbol"])


def test_audit_passes_when_ledger_matches_market(rows, events):
    cal = get_investment_holdings_calendar(rows)
    results = audit_dividends(cal, sorted(cal), events, ASOF)
    aapl = [r for r in results if r[0] == "AAPL"]
    assert len(aapl) == 1
    _, expected, received, ok = aapl[0]
    assert ok, f"expected ~${expected:.2f}, ledger ${received:.2f}"
    assert received == pytest.approx(9.90)


def test_audit_flags_missing_dividend_rows(rows, events):
    cal = get_investment_holdings_calendar(rows)
    results = audit_dividends(cal, sorted(cal), events.iloc[0:0], ASOF)
    aapl = [r for r in results if r[0] == "AAPL"]
    assert len(aapl) == 1 and not aapl[0][3]
