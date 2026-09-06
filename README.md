# portfolio-engine

A personal investment analytics stack: it reads raw broker CSV exports, rebuilds
the account's full transaction-by-transaction history, prices it against real
market data, and reports what the portfolio actually returned as a static HTML
report or as an always-on live dashboard.

The numbers are *right* across
stock splits, dividends, reinvestments, mid-period contributions, and a broker
export format that occasionally lies by omission.

```
broker CSVs                                                       output
    │
    ▼
brokerimport/          bank-agnostic adapters → NormalizedRow ledger
    │
    ▼
investment_holdings_   holdings calendar, dense daily valuation,
calc.py                time-weighted return, split audit
    │                                              │
    ├── dividend_tracker.py   income, yield-on-cost, forward projection
    ├── factor_analysis.py    Fama-French + momentum regression
    │        └── french_data.py   cached Ken French factor library
    │
    ▼
stock_data_cache.py    per-ticker unadjusted daily price cache (yfinance)
    │
    ├──▶ dashboard.py   → single self-contained portfolio_dashboard.html
    └──▶ livedash/      → live Dash/Plotly kiosk (see livedash/README.md)
```

## Quick start

```bash
conda activate financetracking
pip install pandas yfinance plotly statsmodels
```

Drop a Fidelity CSV export into `csvs/{bank}/{account_name}/`, then:

```bash
python main.py
```

That writes `portfolio_dashboard.html` — a single self-contained file with
linked views, a range slider, unified hover, and a per-holding legend. Every
ticker, color, and ordering is derived from the data at runtime; nothing about
any particular portfolio is hardcoded.

For the live version:

```bash
pip install -r livedash/requirements.txt
python -m livedash
```

Then open `http://127.0.0.1:8050`. Full write-up, including the Raspberry Pi
kiosk deployment, in [livedash/README.md](livedash/README.md).

## What it computes

| | |
|---|---|
| **Holdings calendar** | Share count and cash balance after every transaction, replayed from the ledger. Cash is tracked from the broker's running balance where present and from signed amounts where not. |
| **Dense daily valuation** | Every calendar day in the window valued at real closing prices, backfilled from day one rather than from whenever logging started. |
| **Time-weighted return** | The portfolio's return with contributions and trades removed, against a buy-and-hold SPY/QQQ benchmark. |
| **Dividend income** | Receipts by month/quarter/year and by ticker, trailing-12-month yield on cost, and a forward income projection from per-share rates on ex-dates. |
| **Factor exposure** | Monthly excess returns regressed on Fama-French market/size/value plus momentum: each beta with its t-stat, annualized alpha, R², and a return attribution. |

## Design notes

These are the problems that took the actual work.

### Corporate actions are the whole game

A stock split is a 10× jump in share count against a 10× drop in price. Handled
naively it registers as a −90% day and poisons every return figure downstream.
Three separate mechanisms keep that from happening:

- **Prices are cached unadjusted.** Yahoo serves split-adjusted bars and
  restates them after every corporate action, so a cached adjusted price silently
  becomes wrong later. The cache reconstructs what actually traded that day from
  the adjusted bars plus the full split history, and keeps `Adj Close` alongside
  for return math. Cached rows stay valid indefinitely.
- **Position weights come from raw prices; returns come from `Adj Close`.** A
  split nets to zero, a dividend counts as gain, and neither can leak into the
  weighting. `test_twr_is_split_neutral` pins this.
- **The ledger is audited against the market.** For every split a held ticker
  underwent, the broker's share count must jump by roughly the same ratio. If it
  doesn't, the export omitted the split or the adapter didn't recognize its
  format — and every valuation after that date is off by the ratio. Same idea for
  dividends: the market's per-share amounts on ex-dates are cross-checked against
  the cash the ledger says was received.

The audits are deliberately fuzzy. A broker's split row can post several days
late, and ordinary trades in the window distort the ratio, so they report a
pointer, not a verdict.

### The price cache

Each ticker gets one CSV plus a meta file recording the contiguous calendar range
it covers. A query outside that range downloads only the missing head or tail and
merges it. Because Yahoo restates history, head/tail downloads deliberately
overlap the cached span by a week; if the overlap disagrees with what's on disk,
the whole file is refetched rather than trusted.

The valuation loop warms each ticker's full ownership span up front, so the
per-day pricing pass never triggers a download — including for positions already
open before the window started, a bug class that only shows up as mysterious
slowness. Writes are atomic. Ticker names are percent-encoded into filenames so a
path separator or `..` can never escape the cache directory.

### A market clock you can test

Everything about market hours takes an injectable `now`, so the entire clock test
suite is pure and offline — no network, no monkeypatched wall clock.

The bug that motivated the tests: several checks compared a time-of-day against
the settle threshold. At 00:20 on Tuesday, `00:20 < 16:30` is true, so the code
concluded Monday's close hadn't printed yet — hours after it had. Sessions have
to be reasoned about as *sessions*, walking back over midnights and weekends, not
as clock positions. Nineteen tests cover the rollover, the weekend walk-back, NAV
posting times, and phase transitions.

### Doing nothing, efficiently

The live dashboard's quote poller follows the market phase rather than a flat
open/closed switch: fast while the tape runs, slower through the evening that the
closing print and fund NAVs land in, then *nothing at all* until a minute before
the next open, once every displayed ticker has its final price for the session.
That's roughly 3,000 requests a week that would have returned identical bytes.
Waking ahead of the bell means the panel opens the session live instead of
spending the first few minutes under a stale banner.

The render path is gated the same way. UI ticks stay fast so the data-age readout
stays honest, but each callback is gated on the inputs it actually depends on
having moved — the "vs Market" figure only redraws when you change the time
window. Steady state is a ~180-byte response per tick. Per-row sparklines are
hand-generated inline SVG (a few hundred bytes) rather than Plotly figures,
because a table that redraws every 20 seconds on a Raspberry Pi can't afford
thirty chart objects.

### Failing loudly, in the right places

A frozen dashboard showing confident numbers is worse than one that admits it's
stale. During market hours, quotes older than a threshold gray the screen out and
raise a banner. Because a server-rendered banner can't fire if the server process
itself dies, a client-side watchdog tracks the last successful round-trip and
overlays a "connection lost" notice after ~30 seconds of silence.

The inverse applies to non-essential analysis: a cold or blocked Ken French
download must never sink the dashboard, so factor analysis is explicitly
best-effort and degrades to a printed note. The French loader caches extracted
CSVs, refreshes at most daily, and falls back to a stale cache when the network
is gone.

Unmapped broker actions surface as `ActionType.UNKNOWN` rather than flowing
through silently, so a new bank's unrecognized row shows up as a warning instead
of a quietly wrong balance.

### Adding a bank is one file

`brokerimport` knows nothing about any particular bank. A `BankAdapter` captures
everything that is bank-specific — how to find the data rows inside the export's
preamble and disclaimer, the column mapping, the date format, the columns that
form an idempotent dedupe key, the ordered action-classification rules, and any
symbol renames (a reverse split that reissues shares under a temporary CUSIP has
to be reconciled to one name before the ledger and the price source can agree).
Adapters register themselves at import. Fidelity is 92 lines; a second bank is
the same shape.

## Testing and CI

45 tests. They favor invariants over snapshots — that a split-adjusted return
curve is indistinguishable from an unsplit one, that a covered cache query never
touches the network, that a regression recovers known betas from synthetic data,
that the dividend audit flags a deliberately removed ledger row.

```bash
python -m pytest tests -q
```

GitHub Actions runs `ruff format --check`, `ruff check`, and the suite on Python
3.12. Ruff's default rule set applies with one documented project-wide exception:
the `flake8-datetimez` family is disabled, because this app models a single
account in a single timezone where every date is a naive local calendar date *by
design* — broker CSVs carry no timezone and "today" means the user's calendar
day. (The market clock, which genuinely needs timezone awareness, is fully
tz-aware in `America/New_York`.)

Personal `csvs/` and `stock_data/` are gitignored, so tests that need real data
self-skip in CI and the cache tests download a few small windows instead.

## Repo map

| Path | |
|---|---|
| `main.py` | Static report pipeline: import → value → audit → dividends → factors → HTML |
| `brokerimport/` | Bank-agnostic CSV import: adapters, registry, normalized ledger |
| `investment_holdings_calc.py` | Holdings calendar, dense valuation, TWR, split audit |
| `stock_data_cache.py` | Per-ticker unadjusted daily price cache over yfinance |
| `dividend_tracker.py` | Income, yield on cost, forward projection, dividend audit |
| `factor_analysis.py` | Fama-French + momentum regression and report |
| `french_data.py` | Cached loader for the Ken French Data Library |
| `dashboard.py` | Self-contained interactive HTML report |
| `livedash/` | Live kiosk dashboard — see its own README |
| `tests/` | Suite, with CSV fixtures for splits and dividends |

## Security and privacy

Broker exports and the price cache are gitignored; nothing about a real account
is in this repository. The live dashboard binds to loopback and has **no
authentication of any kind** — it is designed to be served to a browser running
on the same machine. Binding it to `0.0.0.0` hands your balances and holdings to
anything else on the network, so put it behind something that authenticates
(reverse proxy, WireGuard, Tailscale) if you need it off-box.

## Known limits

- **No market-holiday calendar.** The clock treats every weekday as a trading
  day; the worst case is one wasted quote fetch on a holiday, which the
  closed-market backoff already makes rare. A calendar dependency wasn't worth it
  for that.
- **One broker implemented.** The adapter seam exists and is exercised; only
  Fidelity is written.
- **Mutual funds have no intraday tape.** Funds like FXAIX price once daily at
  NAV, so an index ETF stands in mid-session, marked `≈` until the real NAV
  posts.
