"""Importer: action classification, split rows, and blank-cell tolerance."""

import os
from datetime import date
from decimal import Decimal

import pytest

from import_transactions import ActionType, import_csv
from investment_holdings_calc import (
    get_investment_holdings_calendar,
    holdings_on_date,
)


def test_split_fixture_classification(fixtures_dir):
    rows = import_csv(os.path.join(fixtures_dir, "fidelity", "split_test"))
    assert [r.action_type for r in rows] == [
        ActionType.BUY,
        ActionType.SPLIT,
        ActionType.SELL,
    ]


def test_split_row_quantities_and_cash(fixtures_dir):
    """A DISTRIBUTION row (blank price/amount/balance) must import, multiply
    the share count, and leave CASH untouched."""
    rows = import_csv(os.path.join(fixtures_dir, "fidelity", "split_test"))
    cal = get_investment_holdings_calendar(rows)

    buy = cal[date(2024, 1, 5)]
    assert buy["NVDA"] == Decimal("2")
    assert buy["CASH"] == Decimal("100.00")

    split_day = cal[date(2024, 6, 10)]
    assert split_day["NVDA"] == Decimal("20.00")  # 2 + 18 distributed
    assert split_day["CASH"] == Decimal("100.00")  # blank balance: no change

    after = cal[date(2024, 6, 11)]
    assert after["NVDA"] == Decimal("15.00")
    assert after["CASH"] == Decimal("705.00")


def test_split_row_fields_normalized(fixtures_dir):
    rows = import_csv(os.path.join(fixtures_dir, "fidelity", "split_test"))
    split = rows[1]
    assert split.action_type is ActionType.SPLIT
    assert split.quantity == Decimal("18.00")
    assert split.amount == 0
    assert split.price == 0
    assert split.cash_balance is None  # blank, not zero


def test_real_accounts_fully_classified():
    """Every action string in the real exports must map to a known type."""
    for folder in ("roth", "cash_management"):
        path = os.path.join("csvs", "fidelity", folder)
        if not os.path.isdir(path):
            pytest.skip("personal csvs not present")
        rows = import_csv(path)
        unknown = sorted(
            {r.action for r in rows if r.action_type is ActionType.UNKNOWN}
        )
        assert not unknown, f"{folder}: unclassified actions {unknown}"


def test_real_roth_holdings_snapshot():
    """Holdings on a fixed historical date never change as new exports land."""
    path = os.path.join("csvs", "fidelity", "roth")
    if not os.path.isdir(path):
        pytest.skip("personal csvs not present")
    cal = get_investment_holdings_calendar(import_csv(path))
    h = holdings_on_date(date(2024, 3, 20), cal, sorted(cal))
    assert h["QQQ"] == Decimal("19.427")
    assert h["META"] == Decimal("3.132")
    assert h["PYPL"] == Decimal("8.498")
    assert h["GOOG"] == Decimal("7.863")
    assert h["CASH"] == Decimal("0.32")
    assert h.get("NVDA", 0) == 0  # sold out on 2024-03-18
