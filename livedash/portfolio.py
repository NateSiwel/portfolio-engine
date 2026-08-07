"""Portfolio state: the bridge between the finance_sim backend and the UI.

`Portfolio.load()` does the heavy, once-per-startup work — parse the ledger,
resolve current holdings and cost basis, and backfill the historical daily-value
and benchmark curves from the price cache. `build_view_model()` is the cheap
per-refresh step that layers live quotes on top to produce everything the screen
needs for one paint.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from brokerimport import import_csv
from dividend_tracker import cost_basis_by_ticker
from investment_holdings_calc import (
    compare_to_market,
    dense_priced_holdings_in_window,
    get_investment_holdings_calendar,
)
from stock_data_cache import get_price

from .quotes import QuoteSnapshot

CASH = "CASH"


# --------------------------------------------------------------------------- #
# Per-refresh view model
# --------------------------------------------------------------------------- #
@dataclass
class Position:
    ticker: str
    shares: float
    price: float  # live (or last cached) price
    prev_close: float | None
    value: float
    day_change: float | None
    day_pct: float | None
    weight: float  # share of total portfolio value
    cost_basis: float | None
    total_gain: float | None  # value - cost_basis
    total_gain_pct: float | None
    intraday: list[float] = field(default_factory=list)
    estimated: bool = False  # price inferred from a proxy ETF
    shape_from_proxy: bool = False  # intraday curve borrowed from a proxy ETF
    earnings_date: date | None = None  # next known earnings date, if any


@dataclass
class ViewModel:
    positions: list[Position]
    cash: float
    total_value: float
    prev_total_value: float
    day_change: float
    day_pct: float
    benchmarks: dict[str, dict]  # ticker -> {day_pct, ...}
    as_of: object  # datetime | None
    age_secs: float | None
    stale: bool
    ok: bool


# --------------------------------------------------------------------------- #
# Startup state
# --------------------------------------------------------------------------- #
@dataclass
class Portfolio:
    csv_dir: str
    benchmarks: tuple[str, ...]

    # resolved on load()
    current_shares: dict[str, float] = field(default_factory=dict)  # ex-cash
    cash: float = 0.0
    cost_basis: dict[str, float] = field(default_factory=dict)
    fallback_price: dict[str, float] = field(default_factory=dict)  # cache close
    history_dates: list[date] = field(default_factory=list)
    history_values: list[float] = field(default_factory=list)
    twr: dict[str, tuple] = field(default_factory=dict)  # bench -> (dates,port,bench)
    # ticker -> next known earnings date; refreshed once daily, never polled.
    earnings: dict[str, date] = field(default_factory=dict)

    @property
    def tickers(self) -> list[str]:
        return list(self.current_shares.keys())

    @property
    def quote_tickers(self) -> list[str]:
        # Held names plus benchmarks (so the vs-market badge has live data too).
        return list(dict.fromkeys([*self.current_shares.keys(), *self.benchmarks]))

    def load(self, *, with_history: bool = True) -> Portfolio:
        rows = import_csv(self.csv_dir)
        calendar = get_investment_holdings_calendar(rows)
        dates = sorted(calendar.keys())
        if not dates:
            raise ValueError(f"No transactions found under {self.csv_dir!r}")

        latest = calendar[dates[-1]]
        self.cash = float(latest.get(CASH, 0) or 0)
        self.current_shares = {
            s: float(q) for s, q in latest.items() if s != CASH and q
        }

        self.cost_basis = {
            s: float(cost) for s, (shares, cost) in cost_basis_by_ticker(rows).items()
        }

        # Seed a fallback price per ticker from the cache (last settled close) so
        # the screen has real values before the first live fetch lands, and a
        # safety net for any ticker Yahoo returns nothing for intraday.
        today = date.today()
        for t in self.current_shares:
            try:
                self.fallback_price[t] = float(get_price(t, today))
            except Exception as exc:  # noqa: BLE001 - a missing price shouldn't sink load
                print(f"[livedash] no cached price for {t}: {exc}")

        if with_history:
            self._load_history(calendar, dates, today)
        return self

    def _load_history(self, calendar, dates, today: date) -> None:
        start = dates[0]
        priced = dense_priced_holdings_in_window(start, today, calendar, dates)
        hist_dates, hist_values = [], []
        for d, holdings in priced:
            if not holdings:
                continue
            total = 0.0
            for symbol, payload in holdings.items():
                total += float(payload) if symbol == CASH else float(payload[2])
            hist_dates.append(d)
            hist_values.append(total)
        self.history_dates, self.history_values = hist_dates, hist_values

        for bench in self.benchmarks:
            try:
                self.twr[bench] = compare_to_market(priced, bench)
            except Exception as exc:  # noqa: BLE001 - a bad benchmark shouldn't sink load
                print(f"[livedash] benchmark {bench} unavailable: {exc}")

    # ----------------------------------------------------------------------- #
    def build_view_model(self, snap: QuoteSnapshot, stale_after: float) -> ViewModel:
        quotes = snap.quotes
        positions: list[Position] = []
        total_value = self.cash
        prev_total_value = self.cash

        for ticker, shares in self.current_shares.items():
            q = quotes.get(ticker)
            price = q.last if q and q.last is not None else None
            if price is None:
                price = self.fallback_price.get(ticker)
            if price is None:
                continue  # no price anywhere; can't value it this refresh
            prev_close = q.prev_close if q else None
            value = shares * price
            prev_value = shares * (prev_close if prev_close is not None else price)
            day_change = (value - prev_value) if prev_close is not None else None
            day_pct = (price / prev_close - 1) * 100 if prev_close else None
            cost = self.cost_basis.get(ticker)
            gain = value - cost if cost is not None else None
            gain_pct = (gain / cost * 100) if cost else None
            total_value += value
            prev_total_value += prev_value
            positions.append(
                Position(
                    ticker=ticker,
                    shares=shares,
                    price=price,
                    prev_close=prev_close,
                    value=value,
                    day_change=day_change,
                    day_pct=day_pct,
                    weight=0.0,
                    cost_basis=cost,
                    total_gain=gain,
                    total_gain_pct=gain_pct,
                    intraday=list(q.intraday) if q else [],
                    estimated=bool(q.estimated) if q else False,
                    shape_from_proxy=bool(q.shape_from_proxy) if q else False,
                    earnings_date=self.earnings.get(ticker),
                )
            )

        for p in positions:
            p.weight = (p.value / total_value * 100) if total_value else 0.0
        positions.sort(key=lambda p: p.value, reverse=True)

        day_change = total_value - prev_total_value
        day_pct = (day_change / prev_total_value * 100) if prev_total_value else 0.0

        bench_info: dict[str, dict] = {}
        for b in self.benchmarks:
            q = quotes.get(b)
            bench_info[b] = {
                "day_pct": q.day_pct if q else None,
                "last": q.last if q else None,
            }

        age = snap.age_secs()
        stale = snap.is_stale(stale_after)

        return ViewModel(
            positions=positions,
            cash=self.cash,
            total_value=total_value,
            prev_total_value=prev_total_value,
            day_change=day_change,
            day_pct=day_pct,
            benchmarks=bench_info,
            as_of=snap.fetched_at,
            age_secs=age,
            stale=stale,
            ok=snap.ok,
        )

    # ----------------------------------------------------------------------- #
    def ytd_returns(self) -> dict[str, float]:
        """Year-to-date % return for the portfolio and each benchmark.

        Derived from the time-weighted curves by re-basing at the first trading
        day of the current year, so contributions don't distort it.
        """
        out: dict[str, float] = {}
        if not self.twr:
            return out
        year_start = date(date.today().year, 1, 1)

        def rebased(dates, curve) -> float | None:
            # Growth factor on the first trading day on/after Jan 1 this year.
            base = next((g for d, g in zip(dates, curve) if d >= year_start), None)
            if not base:
                return None
            return (curve[-1] / base - 1) * 100

        first = next(iter(self.twr.values()))
        p = rebased(first[0], first[1])
        if p is not None:
            out["portfolio"] = p
        for b, (bdates, _, bcurve) in self.twr.items():
            r = rebased(bdates, bcurve)
            if r is not None:
                out[b] = r
        return out
