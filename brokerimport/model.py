"""The bank-agnostic ledger contract: what every adapter normalizes rows into.

Pure data types with no dependency on any particular bank or on the import
machinery — this is what downstream consumers (holdings, dividends) import.
"""

from enum import Enum


class ActionType(Enum):
    """Bank-agnostic meaning of a transaction row.

    The holdings math itself runs on quantity/amount/cash_balance; the type
    exists so consumers can reason about semantics (e.g. a SPLIT moves shares
    without cash) and so unmapped actions from a new bank surface loudly
    instead of flowing through silently.
    """

    BUY = "buy"
    SELL = "sell"
    DIVIDEND = "dividend"  # cash distribution, incl. capital gains
    REINVESTMENT = "reinvestment"
    SPLIT = "split"  # share distribution / reverse split: qty delta, no cash
    DEPOSIT = "deposit"  # external cash in
    WITHDRAWAL = "withdrawal"  # external cash out
    TRANSFER = "transfer"  # cash/securities moved between accounts
    INTEREST = "interest"
    FEE = "fee"
    UNKNOWN = "unknown"


class NormalizedRow:
    """One broker transaction with bank-agnostic field names.

    cash_balance is None when the row carries no balance (corporate-action
    rows like split distributions leave it blank); 0 means an actual zero
    balance. amount/quantity/price default to 0 when blank.
    """

    def __init__(
        self, date, action, action_type, symbol, amount, quantity, cash_balance, price
    ):
        self.date = date
        self.action = action
        self.action_type = action_type
        self.symbol = symbol
        self.amount = amount
        self.quantity = quantity
        self.cash_balance = cash_balance
        self.price = price
