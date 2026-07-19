# --------------------------------------------------------------------------- #
# CSV reading
# --------------------------------------------------------------------------- #
import csv
import io
import os

from datetime import datetime
from decimal import Decimal


class NormalizedRow:
    def __init__(self, date, action, symbol, amount, quantity, cash_balance, price):
        self.date = date
        self.action = action
        self.symbol = symbol
        self.amount = amount
        self.quantity = quantity
        self.cash_balance = cash_balance
        self.price = price


# idempotent key properties that map the name of a bank to a list of column headers that are used to identify unique transactions
# Key automatically includes name of the bank + account_name
idempotent_key_properties = {
    "fidelity": [
        "Run Date",
        "Action",
        "Symbol",
        "Amount ($)",
        "Quantity",
        "Cash Balance ($)",
    ]
}

# normalized -> real mapping of column names for each bank
column_mapping = {
    "fidelity": {
        "date": "Run Date",
        "action": "Action",
        "symbol": "Symbol",
        "amount": "Amount ($)",
        "quantity": "Quantity",
        "cash_balance": "Cash Balance ($)",
        "price": "Price ($)",
    }
}


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


def get_idempotent_key(row, bank, account_name):
    """Return a unique key for a transaction row based on the bank and account name."""
    key_properties = idempotent_key_properties.get(bank.lower())
    if not key_properties:
        raise ValueError(f"No idempotent key properties defined for bank '{bank}'")

    key_values = [row.get(prop, "").strip() for prop in key_properties]
    return f"{bank}:{account_name}:" + ":".join(key_values)


DATE_MAPPING = {"fidelity": "%m/%d/%Y"}


def normalize_row(bank, row):
    mapping = column_mapping.get(bank.lower())
    if not mapping:
        raise ValueError(f"No column mapping defined for bank '{bank}'")

    # parse date into date object
    parsed_date = datetime.strptime(row.get(mapping["date"]), DATE_MAPPING[bank.lower()]).date()

    normalized_row = NormalizedRow(
        date=parsed_date,
        action=row.get(mapping["action"]),
        symbol=row.get(mapping["symbol"]),
        amount=Decimal(row.get(mapping["amount"])),
        quantity=Decimal(row.get(mapping["quantity"])),
        cash_balance=Decimal(row.get(mapping["cash_balance"])),
        price=Decimal(row.get(mapping["price"]) or 0),
    )

    return normalized_row


def import_csv(folder_path) -> list[NormalizedRow]:
    from pathlib import Path

    folder_path = Path(str(folder_path).replace("\\\\", "/"))
    bank = folder_path.parts[-2]
    account_name = folder_path.parts[-1]

    seen = set()
    normalized_rows = []
    all_rows = []
    for csv_path in folder_path.glob("*.csv"):
        rows = parse_fidelity_csv(csv_path)
        # Fidelity exports newest-first; reverse so same-date rows stay
        # chronological through the stable date sort below (row order is the
        # only intra-day sequencing signal — there is no timestamp column).
        all_rows.extend(reversed(rows))

    mapping = column_mapping.get(bank.lower())
    if not mapping:
        raise ValueError(f"No column mapping defined for bank '{bank}'")
    all_rows.sort(
        key=lambda x: datetime.strptime(
            x.get(mapping["date"]), DATE_MAPPING[bank.lower()]
        ).date()
    )
    for row in all_rows:
        idempotent_key = get_idempotent_key(row, bank, account_name)

        # print(idempotent_key, row)
        if idempotent_key in seen:
            # print(f"Duplicate transaction found: {idempotent_key}")
            continue
            # raise ValueError(f"Duplicate transaction found: {idempotent_key}")
        seen.add(idempotent_key)

        normalized_row = normalize_row(bank, row)
        normalized_rows.append(normalized_row)
        # table.add_record(idempotent_key, normalized_row)

    return normalized_rows


if __name__ == "__main__":
    import sys

    if len(sys.argv) == 2:
        import_csv(sys.argv[1])
    else:
        print("usage: python import_transactions.py <path-to-folder>\n")
        print("Known accounts:")
