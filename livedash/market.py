"""US-equity market-clock helpers.

Only the regular session (09:30–16:00 ET, Mon–Fri) is treated as "open"; we
deliberately ignore holidays (no market-calendar dependency) — the worst case
is one wasted quote fetch on a holiday, which the closed-market backoff already
keeps rare. Everything is computed in market time so it's correct regardless of
where the Pi's clock is set.
"""

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

MARKET_TZ = ZoneInfo("America/New_York")
_OPEN = time(9, 30)
_CLOSE = time(16, 0)
# The official closing auction print lands within a few minutes of the bell;
# treat the session's prices as provisional until comfortably past it.
_SETTLE = time(16, 30)
# Funds strike NAV at the close and post it over the following hours; nothing
# useful is on the quote endpoint before roughly this time.
_NAV_POST = time(17, 0)


def now_et() -> datetime:
    return datetime.now(MARKET_TZ)


def _clock(now: datetime) -> time:
    return now.timetz().replace(tzinfo=None)


def market_phase(now: datetime | None = None) -> str:
    """Which part of the data day it is: 'open', 'settling', or 'closed'.

    The distinction that matters to the poller is between an evening that is
    still delivering data (the closing print, then fund NAVs) and one that has
    nothing left to give. Both are "the market is closed", but only one of them
    is worth a network request.
    """
    now = now or now_et()
    if now.weekday() >= 5:  # Sat/Sun
        return "closed"
    t = _clock(now)
    if _OPEN <= t <= _CLOSE:
        return "open"
    if t > _CLOSE:
        return "settling"
    return "closed"  # pre-market: yesterday's numbers, and nothing new coming


def is_market_open(now: datetime | None = None) -> bool:
    return market_phase(now) == "open"


def close_has_printed(now: datetime | None = None) -> bool:
    """Past the point where the official closing auction print has landed."""
    return _clock(now or now_et()) >= _SETTLE


def secs_until_next_open(now: datetime | None = None, lead: float = 60.0) -> float:
    """Seconds until `lead` seconds before the next regular session opens.

    Waking a little ahead of the bell means the first paint of the session is
    already live, instead of the panel opening on pre-market numbers old enough
    to trip the stale banner.
    """
    now = now or now_et()
    target = now.replace(hour=_OPEN.hour, minute=_OPEN.minute, second=0, microsecond=0)
    if now >= target:  # today's open is behind us
        target += timedelta(days=1)
    while target.weekday() >= 5:
        target += timedelta(days=1)
    # Via UTC deliberately: subtracting two aware datetimes that share a tzinfo
    # object ignores the zone and measures wall-clock time, which is an hour off
    # across a DST switch — enough to wake up after the bell every March.
    delta = target.astimezone(timezone.utc) - now.astimezone(timezone.utc)
    return max(delta.total_seconds() - lead, 0.0)


def nav_may_have_posted(now: datetime | None = None) -> bool:
    """Could a mutual fund's NAV for the most recent session be published yet?

    During the session it provably can't be, so the per-fund quote-endpoint
    lookup is pure latency and wasted requests until the evening. On a weekend
    the last session's NAV posted Friday evening, so it's always available.
    """
    now = now or now_et()
    if now.weekday() >= 5:
        return True
    return now.timetz().replace(tzinfo=None) >= _NAV_POST


def session_label(now: datetime | None = None) -> str:
    """Short human label for the current session state."""
    now = now or now_et()
    if now.weekday() >= 5:
        return "Weekend"
    t = now.timetz().replace(tzinfo=None)
    if t < _OPEN:
        return "Pre-market"
    if t <= _CLOSE:
        return "Market open"
    return "After hours"
