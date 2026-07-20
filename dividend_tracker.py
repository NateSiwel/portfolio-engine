"""Dividend income tracking and projection.

Ledger side: ActionType.DIVIDEND rows are the cash receipts (Fidelity folds
capital-gain distributions in too); REINVESTMENT rows are the purchases those
receipts fund, so they add to cost basis rather than income. Market side: the
price cache's Dividends column carries per-share amounts on ex-dates,
split-adjusted to the present, which drives trailing-12-month rates, forward
income projections, and an audit of the ledger's receipts.
"""

from datetime import date, timedelta
from decimal import Decimal
from typing import cast

import pandas as pd

from import_transactions import ActionType, NormalizedRow
from investment_holdings_calc import CASH_SYMBOLS, holdings_on_date
from stock_data_cache import get_history, get_price

EVENT_COLUMNS = ["date", "symbol", "amount", "reinvested"]


def dividend_events(normalized_rows: list[NormalizedRow]) -> pd.DataFrame:
    """One row per cash distribution received: date, symbol, amount, reinvested.

    Money-market payouts (SPAXX etc.) are grouped under CASH, matching the
    holdings ledger. `reinvested` marks receipts the broker plowed straight
    back into shares (a REINVESTMENT row for the same symbol on the same day).
    """
    reinvest_keys = {
        (r.date, r.symbol)
        for r in normalized_rows
        if r.action_type is ActionType.REINVESTMENT
    }
    rows = []
    for r in normalized_rows:
        if r.action_type is not ActionType.DIVIDEND or not r.amount:
            continue
        symbol = r.symbol
        if symbol in CASH_SYMBOLS or symbol.strip() == "":
            symbol = "CASH"
        rows.append(
            dict(
                date=r.date,
                symbol=symbol,
                amount=float(r.amount),
                reinvested=(r.date, r.symbol) in reinvest_keys,
            )
        )
    df = pd.DataFrame(rows, columns=EVENT_COLUMNS)
    df["date"] = pd.to_datetime(df["date"])
    return df.sort_values("date", kind="stable").reset_index(drop=True)


def income_by_period(events: pd.DataFrame, freq: str = "M") -> pd.DataFrame:
    """Dividend income pivot: one row per period, one column per symbol,
    plus a Total column. freq is a pandas period alias: 'M', 'Q', or 'Y'.
    """
    if events.empty:
        return pd.DataFrame()
    pivot = (
        events.assign(period=events["date"].dt.to_period(freq))
        .pivot_table(index="period", columns="symbol", values="amount", aggfunc="sum")
        .fillna(0.0)
    )
    pivot["Total"] = pivot.sum(axis=1)
    return pivot


def income_by_ticker(events: pd.DataFrame) -> pd.Series:
    """All-time dividend income per symbol, largest first."""
    if events.empty:
        return pd.Series(dtype=float)
    return events.groupby("symbol")["amount"].sum().sort_values(ascending=False)


def cost_basis_by_ticker(
    normalized_rows: list[NormalizedRow],
) -> dict[str, tuple[Decimal, Decimal]]:
    """Average-cost basis of the shares currently held: {symbol: (shares, cost)}.

    Cash-out rows that deliver shares (buys, reinvestments) add cost; share
    sales (shares out, cash in) remove cost in proportion to the shares sold.
    Pure share moves — splits, reverse splits, transfers — change the count
    but never the cost, so a 10:1 split leaves basis intact. Securities
    transferred in arrive with zero basis, which overstates yield-on-cost
    for those positions. Reinvested dividends count as new cost, so
    yield-on-cost here is against total money put in, not original outlay.
    """
    shares: dict[str, Decimal] = {}
    cost: dict[str, Decimal] = {}
    for r in normalized_rows:
        symbol = r.symbol
        if symbol.strip() == "" or symbol in CASH_SYMBOLS or not r.quantity:
            continue
        held = shares.get(symbol, Decimal(0))
        if r.quantity > 0:
            if r.amount < 0:
                cost[symbol] = cost.get(symbol, Decimal(0)) - r.amount
        elif r.amount > 0 and held > 0:
            sold_fraction = min(-r.quantity / held, Decimal(1))
            cost[symbol] = cost.get(symbol, Decimal(0)) * (1 - sold_fraction)
        shares[symbol] = held + r.quantity
        if shares[symbol] == 0:
            del shares[symbol]
            cost.pop(symbol, None)
    return {s: (q, cost.get(s, Decimal(0))) for s, q in shares.items()}


def ttm_dividends_per_share(ticker: str, asof: date) -> float:
    """Per-share dividends over the 365 days ending at `asof`, from market data.

    Yahoo reports these split-adjusted to the present, so the figure matches
    today's share count — multiply by current shares for a trailing-rate
    income projection.
    """
    hist = get_history(ticker, asof - timedelta(days=365), min(asof, date.today()))
    if "Dividends" not in hist.columns:
        return 0.0
    return float(hist["Dividends"].sum())


SUMMARY_COLUMNS = [
    "symbol",
    "shares",
    "cost_basis",
    "avg_cost",
    "price",
    "value",
    "ttm_dps",
    "projected_income",
    "yield_on_cost",
    "current_yield",
    "ttm_received",
    "total_received",
]


def dividend_summary(
    normalized_rows: list[NormalizedRow], events: pd.DataFrame, asof: date
) -> pd.DataFrame:
    """Per-ticker dividend metrics for currently held positions, as of `asof`.

    projected_income is the trailing-12-month per-share rate times today's
    shares — a backward-looking forward estimate that ignores announced cuts
    or raises. yield_on_cost and current_yield are that projection against
    average cost basis and market value, in percent. Sorted by projection,
    so non-payers sink to the bottom rather than disappearing.
    """
    rows = []
    cutoff = pd.Timestamp(asof) - pd.Timedelta(days=365)
    for symbol, (shares, cost) in sorted(cost_basis_by_ticker(normalized_rows).items()):
        try:
            price = get_price(symbol, asof)
            dps = ttm_dividends_per_share(symbol, asof)
        except ValueError:
            continue  # no market data; pricing already surfaces this
        received = events.loc[events["symbol"] == symbol, "amount"]
        ttm_mask = (events["symbol"] == symbol) & (events["date"] >= cutoff)
        shares_f, cost_f = float(shares), float(cost)
        projected = shares_f * dps
        rows.append(
            dict(
                symbol=symbol,
                shares=shares_f,
                cost_basis=cost_f,
                avg_cost=cost_f / shares_f if shares_f else float("nan"),
                price=price,
                value=shares_f * price,
                ttm_dps=dps,
                projected_income=projected,
                yield_on_cost=projected / cost_f * 100 if cost_f else float("nan"),
                current_yield=projected / (shares_f * price) * 100 if price else 0.0,
                ttm_received=float(events.loc[ttm_mask, "amount"].sum()),
                total_received=float(received.sum()),
            )
        )
    df = pd.DataFrame(rows, columns=SUMMARY_COLUMNS)
    return df.sort_values("projected_income", ascending=False).reset_index(drop=True)


def audit_dividends(
    holdings_calendar: dict[date, dict[str, Decimal]],
    sorted_dates: list[date],
    events: pd.DataFrame,
    end: date,
    threshold: float = 0.25,
):
    """Cross-check ledger dividend receipts against market dividend events.

    For each ticker ever held, the expected income is the sum over market
    ex-dates of shares held going into that day times the per-share amount.
    Yahoo's per-share figures are split-adjusted to the present, so each is
    scaled back up by the splits that came after its ex-date before meeting
    the ledger's (unadjusted) share counts.

    Payments lag ex-dates by days to weeks, funds fold capital-gain
    distributions into DIVIDEND rows Yahoo doesn't list, and withholding can
    shave receipts — so totals are compared per ticker with a loose
    threshold, and a warning is a pointer to missing rows, not a verdict.
    Returns (symbol, expected, received, ok) per dividend-paying held ticker.
    """
    results = []
    symbols = sorted({s for h in holdings_calendar.values() for s in h if s != "CASH"})
    for symbol in symbols:
        held_dates = [d for d in sorted_dates if holdings_calendar[d].get(symbol)]
        if not held_dates:
            continue
        if holdings_calendar[sorted_dates[-1]].get(symbol):
            last_held = end
        else:
            last_held = held_dates[-1]
        try:
            hist = get_history(symbol, held_dates[0], min(last_held, end, date.today()))
        except ValueError:
            continue  # no market data; pricing already surfaces this
        if "Dividends" not in hist.columns:
            continue
        divs = hist["Dividends"]
        divs = divs[divs != 0]
        if divs.empty:
            continue
        splits = hist.get("Stock Splits")
        expected = 0.0
        for ts, dps in divs.items():
            ex_date = cast(pd.Timestamp, ts).date()
            # Entitlement comes from owning before the ex-date; a buy on the
            # ex-date itself gets no dividend.
            held = (
                holdings_on_date(
                    ex_date - timedelta(days=1), holdings_calendar, sorted_dates
                )
                or {}
            )
            qty = held.get(symbol) or Decimal(0)
            if not qty:
                continue
            factor = 1.0
            if splits is not None:
                for ratio in splits[(splits != 0) & (splits.index > ts)]:
                    factor *= float(ratio)
            expected += float(qty) * float(dps) * factor
        if not expected:
            continue
        received = (
            float(events.loc[events["symbol"] == symbol, "amount"].sum())
            if not events.empty
            else 0.0
        )
        ok = abs(received - expected) <= expected * threshold
        results.append((symbol, expected, received, ok))
        if not ok:
            print(
                f"WARNING: market data implies ~${expected:,.2f} of {symbol}"
                f" dividends over the holding period but the ledger shows"
                f" ${received:,.2f}; dividend rows may be missing or"
                f" misclassified."
            )
    verified = sum(1 for r in results if r[3])
    if results:
        print(
            f"Dividend audit: {verified} ticker(s) verified,"
            f" {len(results) - verified} problem(s)."
        )
    else:
        print("Dividend audit: no market dividends during holding periods.")
    return results


def print_dividend_report(events: pd.DataFrame, summary: pd.DataFrame):
    """Console summary in the style of the split audit and market comparison."""
    if events.empty:
        print("\nDividends: none in the ledger.")
        return
    print("\nDividend income by year:")
    for period, row in income_by_period(events, "Y").iterrows():
        print(f"  {period}: ${row['Total']:,.2f}")
    total = events["amount"].sum()
    reinvested = events.loc[events["reinvested"], "amount"].sum()
    print(f"  All time: ${total:,.2f} (${reinvested:,.2f} reinvested)")

    if summary.empty:
        return
    print("\nForward dividend outlook (trailing-12-month rate x current shares):")
    print(
        f"  {'Ticker':<8}{'Proj. income':>13}{'Yield on cost':>15}{'Curr. yield':>13}"
    )
    for _, r in summary.iterrows():
        print(
            f"  {r['symbol']:<8}{'$' + format(r['projected_income'], ',.2f'):>13}"
            f"{r['yield_on_cost']:>14.2f}%{r['current_yield']:>12.2f}%"
        )
    cost_total = float(summary["cost_basis"].sum())
    value_total = float(summary["value"].sum())
    proj_total = float(summary["projected_income"].sum())
    yoc = f"{proj_total / cost_total * 100:>14.2f}%" if cost_total else f"{'—':>15}"
    cy = f"{proj_total / value_total * 100:>12.2f}%" if value_total else f"{'—':>13}"
    print(f"  {'Total':<8}{'$' + format(proj_total, ',.2f'):>13}{yoc}{cy}")
