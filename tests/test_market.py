"""Market-clock edge cases, focused on the midnight rollover.

Every helper takes an injectable `now`, so these are pure and offline: no
network, no monkeypatching of the wall clock. All datetimes are constructed in
market time (ET) to match how the module reasons about the trading day.
"""

from datetime import datetime

from zoneinfo import ZoneInfo

from livedash.market import (
    close_has_printed,
    is_market_open,
    last_session_date,
    market_phase,
)

ET = ZoneInfo("America/New_York")


def et(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=ET)


# 2026-08-10 is a Monday. The prior week ends Fri 7th; this week runs
# Mon 10th … Fri 14th, Sat 15th / Sun 16th.
PREV_FRI = (2026, 8, 7)
MON = (2026, 8, 10)
TUE = (2026, 8, 11)
FRI = (2026, 8, 14)
SAT = (2026, 8, 15)
SUN = (2026, 8, 16)


class TestLastSessionDate:
    def test_during_session_is_today(self):
        assert last_session_date(et(*MON, 10, 0)) == et(*MON, 10, 0).date()

    def test_pre_open_is_prior_trading_day(self):
        # 08:00 Tuesday, before the bell: the live session is still Monday's.
        assert last_session_date(et(*TUE, 8, 0)) == et(*MON, 10, 0).date()

    def test_after_midnight_rolls_back_to_prior_session(self):
        # 00:15 Tuesday: the calendar flipped but Monday's close still rules.
        assert last_session_date(et(*TUE, 0, 15)) == et(*MON, 10, 0).date()

    def test_pre_open_monday_walks_back_over_the_weekend(self):
        assert last_session_date(et(*MON, 8, 0)) == et(*PREV_FRI, 10, 0).date()

    def test_saturday_is_friday(self):
        assert last_session_date(et(*SAT, 12, 0)) == et(*FRI, 10, 0).date()

    def test_sunday_is_friday(self):
        assert last_session_date(et(*SUN, 12, 0)) == et(*FRI, 10, 0).date()


class TestCloseHasPrinted:
    def test_before_settle_is_false(self):
        assert close_has_printed(et(*MON, 16, 20)) is False

    def test_after_settle_is_true(self):
        assert close_has_printed(et(*MON, 17, 0)) is True

    def test_after_midnight_is_true(self):
        # The bug this branch fixes: 00:20 is < 16:30 as a time-of-day, but the
        # referenced session (Monday) closed hours ago.
        assert close_has_printed(et(*TUE, 0, 20)) is True

    def test_pre_open_next_morning_is_true(self):
        assert close_has_printed(et(*TUE, 8, 0)) is True

    def test_saturday_after_friday_close_is_true(self):
        assert close_has_printed(et(*SAT, 0, 20)) is True


class TestPhaseSanity:
    def test_open_during_session(self):
        assert is_market_open(et(*MON, 10, 0)) is True
        assert market_phase(et(*MON, 10, 0)) == "open"

    def test_settling_after_close(self):
        assert market_phase(et(*MON, 16, 30)) == "settling"

    def test_closed_pre_market(self):
        assert market_phase(et(*MON, 8, 0)) == "closed"

    def test_weekend_closed(self):
        assert market_phase(et(*SAT, 12, 0)) == "closed"
