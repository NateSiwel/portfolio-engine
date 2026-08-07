"""Next earnings date per ticker — the "why is this mover moving" signal.

Earnings dates don't move intraday and Yahoo's calendar endpoint is a separate,
slower request than the price tape, so this is deliberately decoupled from the
quote poller: fetched once per calendar day per ticker and cached in SQLite,
never polled.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import date, datetime


class EarningsStore:
    def __init__(self, path: str):
        self.path = path
        self._init()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    def _init(self) -> None:
        with self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS earnings_date ("
                "  ticker TEXT PRIMARY KEY,"
                "  next_date TEXT,"
                "  checked_day TEXT NOT NULL"
                ")"
            )

    def get_all(self) -> dict[str, date]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT ticker, next_date FROM earnings_date WHERE next_date IS NOT NULL"
            ).fetchall()
        return {t: date.fromisoformat(d) for t, d in rows}

    def checked_today(self, ticker: str, today: date) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT checked_day FROM earnings_date WHERE ticker = ?", (ticker,)
            ).fetchone()
        return bool(row and row[0] == today.isoformat())

    def record(self, ticker: str, next_date: date | None, today: date) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO earnings_date (ticker, next_date, checked_day) "
                "VALUES (?, ?, ?) ON CONFLICT(ticker) DO UPDATE SET "
                "next_date=excluded.next_date, checked_day=excluded.checked_day",
                (ticker, next_date.isoformat() if next_date else None, today.isoformat()),
            )


def _fetch_next_earnings_date(ticker: str) -> date | None:
    """Nearest upcoming earnings date for `ticker`, or None if unknown.

    `yf.Ticker.calendar` shape has drifted across yfinance versions — a dict
    with an "Earnings Date" list (current) or a DataFrame indexed the same way
    (older) — so both are handled. A ticker with no known earnings date (ETFs,
    funds, anything Yahoo has nothing scheduled for) just returns None.
    """
    import yfinance as yf

    try:
        cal = yf.Ticker(ticker).calendar
    except Exception:  # noqa: BLE001 - best-effort; caller keeps the old value
        return None
    if cal is None:
        return None

    if isinstance(cal, dict):
        raw = cal.get("Earnings Date")
    else:  # pandas DataFrame, older yfinance
        try:
            raw = cal.loc["Earnings Date"].dropna().tolist()
        except Exception:  # noqa: BLE001
            raw = None
    if not raw:
        return None
    if not isinstance(raw, (list, tuple)):
        raw = [raw]

    dates = []
    for d in raw:
        if isinstance(d, datetime):
            d = d.date()
        if isinstance(d, date):
            dates.append(d)
    return min(dates) if dates else None


def tag(d: date | None, today: date | None = None) -> str | None:
    """Short "why is this moving" label for an earnings date within the next week."""
    if d is None:
        return None
    today = today or date.today()
    delta = (d - today).days
    if delta < 0 or delta > 6:
        return None
    return "ER today" if delta == 0 else f"ER {d.strftime('%a')}"


def refresh_stale(tickers: list[str], store: EarningsStore, today: date | None = None) -> None:
    """Fetch and cache next-earnings-date for any ticker not yet checked today."""
    today = today or date.today()
    for t in tickers:
        if store.checked_today(t, today):
            continue
        d = _fetch_next_earnings_date(t)
        store.record(t, d, today)


def run_earnings_refresher(
    pf, store: EarningsStore, stop: threading.Event, check_every: int = 3600
) -> None:
    """Background loop: refresh once per calendar day, otherwise sleep.

    Assumes `pf.earnings` already holds today's data from a synchronous refresh
    at startup; this just catches the day rollover for a long-running process,
    waking hourly so it's never more than an hour late to notice.
    """
    last_day = date.today()
    while not stop.is_set():
        if stop.wait(check_every):
            return
        today = date.today()
        if today == last_day:
            continue
        try:
            refresh_stale(pf.tickers, store, today)
            pf.earnings = store.get_all()
            last_day = today
        except Exception as exc:  # noqa: BLE001 - keep previous data, try again next hour
            print(f"[livedash] earnings refresh failed: {exc}")
