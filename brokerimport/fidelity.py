"""Fidelity adapter: parse Fidelity's CSV export and classify its actions.

Registers itself under "fidelity" on import. To add another bank, mirror this
module — a parse function plus a BankAdapter — and register it the same way.
"""

import csv
import io

from .core import BankAdapter, register_bank
from .model import ActionType


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

register_bank("fidelity", FIDELITY)
