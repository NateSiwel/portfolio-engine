"""Always-on live portfolio dashboard (Dash/Plotly) built on the finance_sim backend.

A kiosk-oriented frontend for a small touchscreen (e.g. a 10" Raspberry Pi
panel): large, glanceable numbers, auto-rotating views, live intraday quotes,
and stale-data detection. All portfolio math is reused from the existing
backend (brokerimport / investment_holdings_calc / dividend_tracker); this
package only adds the live-quote layer and the presentation on top.

Entry point: `python -m livedash` (or `run_dashboard.py` at the repo root).
"""

from .config import Config

__all__ = ["Config"]
