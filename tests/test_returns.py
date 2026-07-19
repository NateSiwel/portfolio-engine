"""Return math and the split audit, across a real 10:1 split.

Uses the project's stock_data/ cache (created on first run).
"""

import os
from datetime import date

import pytest

from import_transactions import import_csv
from investment_holdings_calc import (
    audit_splits,
    compare_to_market,
    dense_priced_holdings_in_window,
    get_investment_holdings_calendar,
)
from stock_data_cache import get_price


@pytest.fixture
def split_portfolio(fixtures_dir):
    cal = get_investment_holdings_calendar(
        import_csv(os.path.join(fixtures_dir, "fidelity", "split_test"))
    )
    return cal, sorted(cal)


def test_twr_is_split_neutral(split_portfolio):
    """No phantom -90% day: the TWR curve must track NVDA's total return
    straight through the 2024-06-10 split."""
    cal, dates = split_portfolio
    start, end = date(2024, 6, 3), date(2024, 6, 14)
    priced = dense_priced_holdings_in_window(start, end, cal, dates)
    _, port, _ = compare_to_market(priced, "SPY")

    for a, b in zip(port, port[1:]):
        assert 0.9 < b / a < 1.1, f"discontinuous day step {b / a:.4f}"

    nvda = get_price("NVDA", end, column="Adj Close") / get_price(
        "NVDA", start, column="Adj Close"
    )
    # ~96% NVDA / 4% cash portfolio: close to NVDA, slightly dampened.
    assert abs(port[-1] - nvda) / nvda < 0.03


def test_audit_verifies_imported_split(split_portfolio):
    cal, dates = split_portfolio
    events = audit_splits(cal, dates, date(2024, 6, 14))
    assert [(e[0], e[1], e[4]) for e in events] == [("NVDA", date(2024, 6, 10), True)]
    assert events[0][2] == 10.0  # market ratio


def test_audit_flags_missing_split(fixtures_dir):
    """Holding NVDA through the split with no split row in the ledger must
    produce a warning event."""
    cal = get_investment_holdings_calendar(
        import_csv(os.path.join(fixtures_dir, "fidelity", "split_missing"))
    )
    events = audit_splits(cal, sorted(cal), date(2024, 6, 14))
    bad = [e for e in events if not e[4]]
    assert [(e[0], e[1]) for e in bad] == [("NVDA", date(2024, 6, 10))]
    assert bad[0][3] == 1.0  # ledger ratio: share count never moved
