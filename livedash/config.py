"""Runtime configuration for the live dashboard.

Everything here is overridable from the environment so the same code runs on a
laptop (development) and on the Pi (kiosk) without edits. Colors live here too
so the whole look can be retuned in one place.
"""

import os
from dataclasses import dataclass, field


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Config:
    # Where the broker CSVs live. Point this at the account you want on screen.
    csv_dir: str = os.environ.get("LIVEDASH_CSV_DIR", "csvs/fidelity/roth")

    # Benchmarks plotted on the "vs Market" view; the first is the headline one
    # used for the day/return comparison badge.
    benchmarks: tuple[str, ...] = ("SPY", "QQQ")

    # Mutual funds price once a day at NAV (posted hours after the close), so
    # they have no intraday tape at all. Each entry maps a fund to an ETF on the
    # same index; during the session the fund's move is estimated from the proxy
    # and marked "≈" in the UI, and the real NAV takes over once it posts.
    fund_proxies: dict[str, str] = field(
        default_factory=lambda: {
            "FXAIX": "SPY",  # Fidelity 500 Index
            "FNILX": "SPY",  # Fidelity ZERO Large Cap
            "FSKAX": "VTI",  # Fidelity Total Market
            "FZROX": "VTI",  # Fidelity ZERO Total Market
            "FTIHX": "VXUS",  # Fidelity Total International
            "FZILX": "VXUS",  # Fidelity ZERO International
            "FXNAX": "AGG",  # Fidelity US Bond Index
        }
    )

    # Refresh cadence (seconds). Quotes refresh fast while the tape runs, and
    # back off to the slow cadence through the evening that the closing print
    # and fund NAVs land in. Once those have arrived the poller stops entirely
    # until just before the next open, so neither of these applies overnight or
    # at the weekend — see `run_poller`.
    refresh_open_secs: int = _env_int("LIVEDASH_REFRESH_OPEN", 20)
    refresh_closed_secs: int = _env_int("LIVEDASH_REFRESH_CLOSED", 300)

    # Auto-rotation between views; 0 disables rotation (stay on one view).
    rotate_secs: int = _env_int("LIVEDASH_ROTATE", 30)

    # Data older than this (seconds) during market hours grays the screen and
    # raises the stale banner. A frozen dashboard showing confident numbers is
    # worse than an obviously-stale one.
    stale_after_secs: int = _env_int("LIVEDASH_STALE_AFTER", 75)

    # SQLite file for the daily end-of-day value snapshots.
    snapshot_db: str = os.environ.get("LIVEDASH_DB", "livedash_snapshots.db")

    host: str = os.environ.get("LIVEDASH_HOST", "127.0.0.1")
    port: int = _env_int("LIVEDASH_PORT", 8050)
    debug: bool = _env_bool("LIVEDASH_DEBUG", False)

    # Start with dollar amounts blurred (percentages only) — useful for a
    # screen that lives in a shared room. Toggleable at runtime.
    start_blurred: bool = _env_bool("LIVEDASH_BLUR", False)

    # --- palette -----------------------------------------------------------
    # Dark, low-glare background for an always-on panel. Gains/losses are keyed
    # by color AND arrow, so the display stays readable with red/green color
    # blindness (see the design brief). Green is pulled toward teal and red kept
    # warm so the pair stays separable under deuteranopia (validated ΔE ≥ 8 on
    # this surface).
    bg: str = "#0a0c10"
    panel: str = "#10141b"
    panel_alt: str = "#161c25"
    border: str = "#1f2630"
    text: str = "#e4e9ef"
    text_dim: str = "#77808d"
    up: str = "#1fa588"
    down: str = "#e05252"
    accent: str = "#b8892f"
    warn: str = "#c48a2d"

    # Line/series palette: portfolio amber first, then muted steel blue and
    # rose for benchmarks. Validated for CVD separation on the dark surface.
    palette: tuple[str, ...] = field(
        default_factory=lambda: (
            "#b8892f",
            "#5e8fd0",
            "#c0699b",
            "#1fa588",
            "#8a93a3",
            "#a98457",
            "#4f9e9b",
            "#7d76c4",
            "#b37070",
            "#5d8a5e",
        )
    )

    def color_for(self, delta: float) -> str:
        return self.up if delta >= 0 else self.down
