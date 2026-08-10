"""Live intraday quotes for the held tickers.

One batched yfinance request per refresh pulls today's 5-minute bars for every
ticker at once (sparkline shape + last traded price) plus a short daily history
for the previous close. Failures are swallowed and the previous good snapshot is
kept, with a timestamp, so the UI layer can gray out and warn when data goes
stale instead of showing confident frozen numbers.
"""

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime

import pandas as pd

from .market import (
    MARKET_TZ,
    close_has_printed,
    is_market_open,
    last_session_date,
    market_phase,
    nav_may_have_posted,
    secs_until_next_open,
)

# Mutual funds (e.g. FXAIX) have no intraday bars, so the 5m request always
# "fails" for them — expected, and handled by the daily-close fallback in
# fetch_quotes. Quiet yfinance's own logging so an always-on panel doesn't
# accrue that noise for months; a genuinely down feed still surfaces as an
# empty result.
logging.getLogger("yfinance").setLevel(logging.CRITICAL)


@dataclass
class Quote:
    ticker: str
    last: float | None = None  # most recent traded price
    prev_close: float | None = None  # prior session's close (day-change base)
    intraday: list[float] = field(default_factory=list)  # today's 5m closes
    # These two are independent: a fund can have an exact NAV (estimated=False)
    # while its intraday curve is still the proxy's shape (shape_from_proxy=True).
    estimated: bool = False  # `last` inferred from a proxy ETF
    shape_from_proxy: bool = False  # intraday curve borrowed from a proxy ETF
    # `last` is this session's final number — the official close, or a posted
    # NAV — so no later fetch today can change it.
    settled: bool = False

    @property
    def day_change(self) -> float | None:
        if self.last is None or self.prev_close is None:
            return None
        return self.last - self.prev_close

    @property
    def day_pct(self) -> float | None:
        dc = self.day_change
        if dc is None or not self.prev_close:
            return None
        return dc / self.prev_close * 100.0


@dataclass
class QuoteSnapshot:
    quotes: dict[str, Quote]
    fetched_at: datetime | None  # None until the first successful fetch
    ok: bool  # did the most recent fetch succeed
    # Every ticker we display has its final price for the session, so there is
    # nothing left to fetch until the next open.
    settled: bool = False

    def age_secs(self, now: datetime | None = None) -> float | None:
        if self.fetched_at is None:
            return None
        now = now or datetime.now(self.fetched_at.tzinfo)
        return (now - self.fetched_at).total_seconds()

    def is_stale(self, stale_after: float, now: datetime | None = None) -> bool:
        """Old enough to distrust. Only meaningful while the tape is running —
        outside the session the data is *supposed* to sit still."""
        if not is_market_open():
            return False
        age = self.age_secs(now)
        return age is None or age > stale_after

    def render_key(self) -> list:
        """Identity of this snapshot, for skipping redundant re-renders.

        `ok` is part of it because a failed fetch carries the previous
        `fetched_at` forward, and the UI still has to react to that.
        """
        stamp = self.fetched_at.isoformat() if self.fetched_at else None
        return [stamp, self.ok]


def _extract(df: pd.DataFrame, ticker: str, field_name: str) -> pd.Series | None:
    """Pull one column for one ticker from a (possibly single-ticker) frame.

    yfinance returns a flat frame for a single ticker and a ticker-grouped
    MultiIndex for several, so normalize both to a plain Series here.
    """
    if df is None or df.empty:
        return None
    cols = df.columns
    if isinstance(cols, pd.MultiIndex):
        if (ticker, field_name) in cols:
            return df[(ticker, field_name)].dropna()
        return None
    if field_name in cols:
        return df[field_name].dropna()
    return None


def _quote_endpoint_price(ticker: str) -> tuple[float | None, float | None]:
    """(last, prev_close) from Yahoo's quote endpoint, or (None, None).

    Only used for mutual funds: Yahoo's quote endpoint carries the evening NAV
    an hour or more before the daily-history endpoint grows a row for it, so
    without this the fund would sit on a proxy estimate all evening.
    """
    import yfinance as yf

    try:
        fi = yf.Ticker(ticker).fast_info
        last = fi.get("lastPrice")
        prev = fi.get("regularMarketPreviousClose")
    except Exception:  # noqa: BLE001 - best-effort extra; caller has a fallback
        return None, None
    return (
        float(last) if last is not None else None,
        float(prev) if prev is not None else None,
    )


def _proxy_shape(
    path: list[float], proxy_base: float, fund_base: float, fund_last: float
) -> list[float]:
    """Rescale a proxy's intraday path onto a fund's price level.

    The fund contributes the two prices it actually knows (yesterday's NAV and
    today's) and the proxy contributes only the shape between them. Scaling
    alone would land the curve on the *proxy's* close, so the residual gap is
    spread linearly across the session — the wiggles survive and the right edge
    still agrees with the price shown next to it.
    """
    scaled = [fund_base * v / proxy_base for v in path]
    gap = fund_last - scaled[-1]
    n = len(scaled)
    return [v + gap * (i + 1) / n for i, v in enumerate(scaled)]


def fetch_quotes(
    tickers: list[str], proxies: dict[str, str] | None = None
) -> dict[str, Quote]:
    """Fetch a Quote for every ticker in one batched download.

    `proxies` maps tickers with no intraday tape (mutual funds) to an ETF that
    tracks the same index; until the fund's NAV posts for the day, its intraday
    move is estimated from the proxy and flagged `estimated`.

    Raises on a hard failure (network/parse) so the caller keeps the previous
    snapshot; an individual ticker with no data just gets an empty Quote.
    """
    import yfinance as yf

    tickers = list(dict.fromkeys(tickers))  # de-dupe, preserve order
    if not tickers:
        return {}
    proxies = {t: p for t, p in (proxies or {}).items() if t in tickers}
    fetch_list = list(dict.fromkeys([*tickers, *proxies.values()]))

    intraday = yf.download(
        fetch_list,
        period="1d",
        interval="5m",
        group_by="ticker",
        auto_adjust=False,
        progress=False,
        threads=True,
    )
    daily = yf.download(
        fetch_list,
        period="5d",
        interval="1d",
        group_by="ticker",
        auto_adjust=False,
        progress=False,
        threads=True,
    )

    # A dead feed empties the frames rather than raising, and every ticker would
    # then get a priceless Quote — which the view model papers over with cached
    # closes while `ok=True` holds the stale banner down, i.e. exactly the
    # confident frozen numbers this module exists to avoid. Only a total blank
    # counts: a fund with no 5m tape, or a pre-market fetch, legitimately empties
    # one frame, never both.
    if (intraday is None or intraday.empty) and (daily is None or daily.empty):
        raise RuntimeError("Yahoo returned no data for any ticker")

    quotes: dict[str, Quote] = {}
    today = last_session_date()  # last/current session, not the wall-clock date
    for t in fetch_list:
        intra = _extract(intraday, t, "Close")
        day = _extract(daily, t, "Close")
        spark = [float(v) for v in intra.tolist()] if intra is not None else []

        # Previous close = last daily close strictly before today's bar. If the
        # daily frame already carries today (post-open), that's row [-2];
        # otherwise the last row is the prior session.
        prev_close = None
        last = None
        closes: list[tuple] = []
        has_today = False
        if day is not None and len(day) >= 1:
            closes = [(idx.date(), float(val)) for idx, val in day.items()]
            prior = [v for d, v in closes if d < today]
            prev_close = (
                prior[-1] if prior else closes[-2][1] if len(closes) >= 2 else None
            )
            has_today = closes[-1][0] >= today
            # Today's daily bar tracks the consolidated last price live and
            # settles to the official closing auction print. The 5m tape does
            # neither — its final bar stops at 15:55 and misses the close — so
            # it's only a fallback for when the daily row hasn't appeared yet.
            if has_today:
                last = closes[-1][1]
        if last is None:
            last = spark[-1] if spark else (closes[-1][1] if closes else None)
        # Today's row carries the live consolidated price during the session
        # and the official print afterwards, so its mere existence isn't
        # finality: the number keeps moving until the closing auction prints.
        # Claiming otherwise would let the poller go to sleep for the night on
        # a provisional price and never fetch the real close.
        quotes[t] = Quote(
            ticker=t,
            last=last,
            prev_close=prev_close,
            intraday=spark,
            settled=has_today and close_has_printed(),
        )

    # Funds have no tape of their own, so the proxy ETF supplies the intraday
    # curve all day. Whether it also has to supply the *price* depends on how
    # far along the NAV is: mid-session it does, and it hands off as soon as the
    # real NAV lands — first on the quote endpoint, later in daily history.
    for t, proxy in proxies.items():
        q, p = quotes.get(t), quotes.get(proxy)
        if q is None or not q.prev_close or len(q.intraday) >= 2:
            continue  # unknown, unpriceable, or it has a real tape

        if not q.settled:
            # The quote endpoint posts the NAV well before the history endpoint
            # does; take it whenever it agrees with us on the prior close and
            # has actually moved off it. It's one extra request per fund, and
            # mid-session it can only ever echo yesterday's NAV back at us, so
            # don't spend it until the evening.
            nav = nav_prev = None
            if nav_may_have_posted():
                nav, nav_prev = _quote_endpoint_price(t)
            if (
                nav is not None
                and nav_prev is not None
                and abs(nav_prev - q.prev_close) < 0.005
                and abs(nav - nav_prev) > 1e-9
            ):
                # A posted NAV is the fund's final price for the day, even
                # though daily history won't grow the row for hours yet.
                q.last = nav
                q.settled = True
            elif p and p.prev_close and p.last is not None:
                q.last = q.prev_close * (p.last / p.prev_close)
                q.estimated = True

        if p and p.prev_close and p.intraday and q.last is not None:
            q.intraday = _proxy_shape(p.intraday, p.prev_close, q.prev_close, q.last)
            q.shape_from_proxy = True
    return quotes


class QuoteService:
    """Holds the latest snapshot and refreshes it on demand (thread-safe).

    Refreshes run on a background thread so a slow Yahoo response never blocks
    the Dash callback; the callback always reads whatever snapshot is current.
    """

    def __init__(self, tickers: list[str], proxies: dict[str, str] | None = None):
        self._tickers = tickers
        self._proxies = dict(proxies or {})
        self._lock = threading.Lock()
        self._snapshot = QuoteSnapshot(quotes={}, fetched_at=None, ok=False)

    @property
    def snapshot(self) -> QuoteSnapshot:
        with self._lock:
            return self._snapshot

    def set_tickers(self, tickers: list[str]) -> None:
        with self._lock:
            self._tickers = tickers

    def refresh_blocking(self) -> QuoteSnapshot:
        """Fetch synchronously and store the result. Returns the new snapshot."""
        with self._lock:
            tickers = list(self._tickers)
        try:
            quotes = fetch_quotes(tickers, self._proxies)
            snap = QuoteSnapshot(
                quotes=quotes,
                fetched_at=datetime.now(MARKET_TZ),
                ok=True,
                settled=_all_settled(quotes, tickers),
            )
        except Exception as exc:  # noqa: BLE001 - keep last good data, mark not-ok
            print(f"[livedash] quote refresh failed: {exc}")
            with self._lock:
                prev = self._snapshot
            snap = QuoteSnapshot(
                quotes=prev.quotes,
                fetched_at=prev.fetched_at,
                ok=False,
                settled=prev.settled,
            )
        with self._lock:
            self._snapshot = snap
        return snap


# Longest single wait. A sleep that spans hours is re-derived at this interval
# so a host that suspends and resumes reacts to the wall clock rather than to a
# timer that stopped advancing with it.
_SLEEP_CHUNK_SECS = 900.0


def _all_settled(quotes: dict[str, Quote], tickers: list[str]) -> bool:
    """Does every displayed ticker have its final price for the session?

    Only the tickers we actually show count — `quotes` also carries proxy ETFs
    fetched purely to shape a fund's curve, and those needn't have settled for
    the portfolio's own numbers to be final.
    """
    if not tickers:
        return False
    return all(
        (q := quotes.get(t)) is not None and q.settled and not q.estimated
        for t in tickers
    )


def _next_wait(service: QuoteService, cfg) -> float:
    """Seconds to wait before the next fetch, from the current market phase."""
    phase = market_phase()
    if phase == "open":
        return cfg.refresh_open_secs
    # The evening after a session still delivers real news — the official
    # closing print, then fund NAVs over the following hours — so keep polling
    # until the snapshot says all of it has landed.
    if phase == "settling" and not (close_has_printed() and service.snapshot.settled):
        return cfg.refresh_closed_secs
    # Overnight, weekends, and a settled evening: Yahoo has nothing left to say
    # until the bell, and asking anyway just returns the same bytes. Sleep to
    # just before the open instead. Floored at the open cadence so the final
    # minute before the bell polls at trading speed rather than spinning.
    return max(secs_until_next_open(), cfg.refresh_open_secs)


def run_poller(service: QuoteService, cfg, stop: threading.Event) -> None:
    """Background loop that keeps `service` fresh at the market's own pace.

    Fast while the tape runs, slow through the evening the closing print and
    fund NAVs land in, and asleep until the next open once there's nothing left
    to fetch — an unattended panel shouldn't spend the night asking Yahoo the
    same question. Startup normally primes the service synchronously so the
    first paint has live data; when it has, sleep first rather than immediately
    refetching the same prices.
    """
    if service.snapshot.fetched_at is None:
        service.refresh_blocking()
    while not stop.is_set():
        started = time.monotonic()
        while True:
            # Re-derived every chunk, so crossing into a new phase mid-sleep
            # (most importantly, the opening bell) shortens the wait already
            # in progress instead of being noticed hours late.
            wait = _next_wait(service, cfg)
            elapsed = time.monotonic() - started
            if elapsed >= wait:
                break
            if stop.wait(min(wait - elapsed, _SLEEP_CHUNK_SECS)):
                return
        service.refresh_blocking()
