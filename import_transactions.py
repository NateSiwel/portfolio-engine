# --------------------------------------------------------------------------- #
# CSV reading
# --------------------------------------------------------------------------- #
import csv
import io

from datetime import datetime
from decimal import Decimal
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


class BankAdapter:
    """Everything bank-specific about reading one bank's CSV export.

    parse_csv: path -> list of {header: value} dicts for the data rows.
    columns: normalized field name -> that bank's column header.
    date_format: strptime format of the date column.
    key_columns: headers whose values identify a unique transaction
        (the idempotent key automatically includes bank + account name).
    action_rules: ordered (substring, ActionType) pairs; the first substring
        found in the uppercased action text wins. Order rules most-specific
        first — company names can contain rule words.
    symbol_aliases: old -> new symbol renames (e.g. a reverse split that
        reissues shares under a temporary CUSIP), applied at normalize time
        so the ledger and the price source agree on one name.
    """

    def __init__(
        self,
        parse_csv,
        columns,
        date_format,
        key_columns,
        action_rules,
        symbol_aliases=None,
    ):
        self.parse_csv = parse_csv
        self.columns = columns
        self.date_format = date_format
        self.key_columns = key_columns
        self.action_rules = action_rules
        self.symbol_aliases = symbol_aliases or {}

    def classify(self, action_text) -> ActionType:
        text = (action_text or "").upper()
        for pattern, action_type in self.action_rules:
            if pattern in text:
                return action_type
        return ActionType.UNKNOWN


def parse_fidelity_csv(path) -> list:
    """Return the transaction rows as a list of {header: value} dicts.

    Fidelity wraps the data in a preamble and a junk disclaimer at the bottom.
    We start at the 'Run Date' header line and stop at the first blank line
    after it (the blank line precedes the trailing disclaimer).
    """
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        lines = f.readlines()

    header_idx = next(
        (i for i, line in enumerate(lines) if line.strip().startswith("Run Date")),
        None,
    )
    if header_idx is None:
        raise ValueError("Could not find a 'Run Date' header row in the CSV")

    data_lines = []
    for line in lines[header_idx + 1 :]:
        if line.strip() == "":  # first empty row ends the transaction block
            break
        data_lines.append(line)

    reader = csv.reader(io.StringIO("".join([lines[header_idx]] + data_lines)))
    all_rows = list(reader)
    if not all_rows:
        return []

    headers = [h.strip() for h in all_rows[0]]
    rows = []
    for raw in all_rows[1:]:
        rows.append(
            {headers[i]: (raw[i] if i < len(raw) else "") for i in range(len(headers))}
        )
    return rows


FIDELITY = BankAdapter(
    parse_csv=parse_fidelity_csv,
    columns={
        "date": "Run Date",
        "action": "Action",
        "symbol": "Symbol",
        "amount": "Amount ($)",
        "quantity": "Quantity",
        "cash_balance": "Cash Balance ($)",
        "price": "Price ($)",
    },
    date_format="%m/%d/%Y",
    key_columns=[
        "Run Date",
        "Action",
        "Symbol",
        "Amount ($)",
        "Quantity",
        "Cash Balance ($)",
    ],
    action_rules=[
        ("YOU BOUGHT", ActionType.BUY),
        ("YOU SOLD", ActionType.SELL),
        ("REINVESTMENT", ActionType.REINVESTMENT),
        ("DIVIDEND RECEIVED", ActionType.DIVIDEND),
        ("CAP GAIN", ActionType.DIVIDEND),  # LONG-TERM/SHORT-TERM CAP GAIN
        ("CASH IN LIEU", ActionType.SELL),  # fractional-share proceeds
        ("REVERSE SPLIT", ActionType.SPLIT),
        ("R/S", ActionType.SPLIT),
        ("DISTRIBUTION", ActionType.SPLIT),  # forward split's share delivery
        ("DIRECT DEPOSIT", ActionType.DEPOSIT),
        ("CASH CONTRIBUTION", ActionType.DEPOSIT),
        ("DIRECT DEBIT", ActionType.WITHDRAWAL),
        ("ELECTRONIC FUNDS TRANSFER", ActionType.TRANSFER),
        ("TRANSFERRED", ActionType.TRANSFER),
        ("INTEREST", ActionType.INTEREST),
        ("FOREIGN TAX", ActionType.FEE),
        ("FEE", ActionType.FEE),
    ],
)

BANKS = {"fidelity": FIDELITY}


def _adapter(bank) -> BankAdapter:
    adapter = BANKS.get(bank.lower())
    if not adapter:
        raise ValueError(
            f"No adapter defined for bank '{bank}' (known: {sorted(BANKS)})"
        )
    return adapter


def get_idempotent_key(row, bank, account_name):
    """Return a unique key for a transaction row based on the bank and account name."""
    adapter = _adapter(bank)
    key_values = [row.get(prop, "").strip() for prop in adapter.key_columns]
    return f"{bank}:{account_name}:" + ":".join(key_values)


def _decimal(value, default=None):
    """Decimal from a CSV cell, or `default` when the cell is blank.

    Corporate-action rows (split distributions, reverse splits) leave most
    money columns empty, so blank must not be an error — and for balances it
    must stay distinguishable from an actual 0.
    """
    value = (value or "").strip()
    return Decimal(value) if value else default


def normalize_row(bank, row):
    adapter = _adapter(bank)
    mapping = adapter.columns

    # parse date into date object
    parsed_date = datetime.strptime(
        row.get(mapping["date"]), adapter.date_format
    ).date()

    action = row.get(mapping["action"])
    symbol = (row.get(mapping["symbol"]) or "").strip()
    normalized_row = NormalizedRow(
        date=parsed_date,
        action=action,
        action_type=adapter.classify(action),
        symbol=adapter.symbol_aliases.get(symbol, symbol),
        amount=_decimal(row.get(mapping["amount"]), Decimal(0)),
        quantity=_decimal(row.get(mapping["quantity"]), Decimal(0)),
        cash_balance=_decimal(row.get(mapping["cash_balance"])),
        price=_decimal(row.get(mapping["price"]), Decimal(0)),
    )

    return normalized_row


def import_csv(folder_path) -> list[NormalizedRow]:
    from pathlib import Path

    folder_path = Path(str(folder_path).replace("\\\\", "/"))
    bank = folder_path.parts[-2]
    account_name = folder_path.parts[-1]
    adapter = _adapter(bank)

    seen = set()
    normalized_rows = []
    all_rows = []
    for csv_path in folder_path.glob("*.csv"):
        rows = adapter.parse_csv(csv_path)
        # Fidelity exports newest-first; reverse so same-date rows stay
        # chronological through the stable date sort below (row order is the
        # only intra-day sequencing signal — there is no timestamp column).
        all_rows.extend(reversed(rows))

    all_rows.sort(
        key=lambda x: datetime.strptime(
            x.get(adapter.columns["date"]), adapter.date_format
        ).date()
    )
    for row in all_rows:
        idempotent_key = get_idempotent_key(row, bank, account_name)

        if idempotent_key in seen:
            continue
        seen.add(idempotent_key)

        normalized_row = normalize_row(bank, row)
        normalized_rows.append(normalized_row)

    unknown = sorted(
        {
            (r.action or "").strip()
            for r in normalized_rows
            if r.action_type is ActionType.UNKNOWN
        }
    )
    if unknown:
        print(f"WARNING: {len(unknown)} unrecognized action(s) in {folder_path}:")
        for action in unknown:
            print(f"  {action}")
        print("  (rows still imported; classify them in the bank's action_rules)")

    return normalized_rows


if __name__ == "__main__":
    import sys

    if len(sys.argv) == 2:
        import_csv(sys.argv[1])
    else:
        print("usage: python import_transactions.py <path-to-folder>\n")
        print("Known accounts:")
