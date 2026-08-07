"""Bank-agnostic broker statement importer.

Normalizes each bank's CSV export into a common ledger of NormalizedRow
records. The generic engine lives in `core`; each supported bank is a small
adapter module (see `fidelity`) that registers itself on import. Add a bank by
writing one adapter module and importing it here.
"""

from . import fidelity  # noqa: F401 — imported for its register_bank side effect
from .core import (
    BANKS,
    BankAdapter,
    get_idempotent_key,
    import_csv,
    normalize_row,
    register_bank,
)
from .model import ActionType, NormalizedRow

__all__ = [
    "BANKS",
    "ActionType",
    "BankAdapter",
    "NormalizedRow",
    "get_idempotent_key",
    "import_csv",
    "normalize_row",
    "register_bank",
]
