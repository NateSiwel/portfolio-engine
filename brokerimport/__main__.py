"""CLI: python -m brokerimport <path-to-account-folder>."""

import sys

from .core import BANKS, import_csv


def main(argv: list[str]) -> int:
    if len(argv) == 2:
        import_csv(argv[1])
        return 0
    print("usage: python -m brokerimport <path-to-folder>\n")
    print(f"Known banks: {', '.join(sorted(BANKS))}")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
