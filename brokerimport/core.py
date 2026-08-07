"""The bank-agnostic import engine: adapters, registry, and the pipeline.

A BankAdapter captures everything bank-specific about one bank's CSV export;
banks register themselves here (see the fidelity module) so import_csv can look
them up by name. The pipeline reads a folder of exports, dedupes by an
idempotent key, and normalizes each row into a NormalizedRow. Nothing in this
module knows about any particular bank.
"""

from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .model import ActionType, NormalizedRow


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


# Registry of known banks, populated by adapter modules calling register_bank
# at import time (see fidelity). Kept here so the engine stays bank-agnostic.
BANKS: dict[str, BankAdapter] = {}


def register_bank(name: str, adapter: BankAdapter) -> None:
    """Register `adapter` under `name` (case-insensitive) for import_csv."""
    BANKS[name.lower()] = adapter


def _adapter(bank) -> BankAdapter:
    adapter = BANKS.get(bank.lower())
    if not adapter:
        raise ValueError(
            f"No adapter defined for bank '{bank}' (known: {sorted(BANKS)})"
        )
    return adapter


def get_idempotent_key(row, bank, account_name):
    """Return a unique key for a transaction row based on the bank and account name.

    Numeric key columns are canonicalized (Decimal-normalized) so that the same
    transaction keys identically across export-format changes — e.g. Fidelity
    writes a cash dividend's Quantity as "0.000" in one export and "0" in
    another, and without this a re-exported row would slip past dedup and be
    double-counted. Non-numeric columns (dates, action text, symbols) never
    parse as Decimal and pass through unchanged.
    """
    adapter = _adapter(bank)
    key_values = []
    for prop in adapter.key_columns:
        val = row.get(prop, "").strip()
        try:
            val = format(Decimal(val).normalize(), "f")
        except InvalidOperation:
            pass
        key_values.append(val)
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
