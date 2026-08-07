# livedash — always-on portfolio display

A Dash/Plotly kiosk frontend for the `finance_sim` backend, built for a small
always-on touchscreen (e.g. a 10" Raspberry Pi panel). It reuses all the
existing portfolio math — holdings, cost basis, dividends, benchmark comparison,
the price cache — and adds a live-quote layer and a glanceable presentation on
top.

## What's on screen

- **Hero band** — total value and, in the biggest type on the panel, *today's
  dollar change* with an arrow, the day %, and how you're doing versus the
  headline benchmark (SPY) today. That "beating/lagging by N pts" line is the
  single most informative thing on the display.
- **Overview** — positions table with an intraday **sparkline** per holding
  (baseline = previous close), live price, day %, day $, value, and a weight
  bar.
- **Treemap** — boxes sized by position weight, colored by today's move. Reads
  concentration and the day at a glance.
- **History** — portfolio value over time, **backfilled from day one** out of
  the price cache (not just from when you started logging).
- **Movers** — today's biggest up/down movers.
- **vs Market** — time-weighted return against SPY / QQQ (contributions
  excluded), the same math as the static dashboard.

History and vs Market carry a **time-window selector** (1M / 3M / 6M / YTD /
1Y / ALL); vs Market re-bases both curves at the window start so the comparison
is over that window, not diluted by three years of compounding.

**Mutual funds** (FXAIX & friends) price once a day at NAV and have no intraday
tape, so an index-tracking ETF stands in for them (FXAIX → SPY; see
`fund_proxies` in `livedash/config.py`). Price and shape are handled separately:
mid-session the *price* is estimated from the proxy and marked "≈", and that
clears the moment Yahoo posts the real NAV. The *sparkline* is always the
proxy's shape rescaled onto the fund and pinned to whatever price is current, so
it's drawn faint whether or not the NAV has landed.

Views **auto-rotate** (default every 30s) so an unattended screen cycles through
all of them; tap any tab to jump, and use the controls to **pause** rotation
(⏸) or **blur** dollar amounts (👁, percentages stay visible) for a screen that
lives in a shared room.

**Stale-data detection:** during market hours, if the last successful quote
fetch is older than the threshold, the screen dims to grayscale and a red banner
appears — a frozen dashboard showing confident numbers is worse than one that
admits it's stale. Quotes poll fast while the market is open and back off hard
when it's closed.

**Dead-server watchdog:** the server-rendered stale banner can't fire if the
server process itself dies — the page just freezes at its last paint, clock
still ticking. A client-side watchdog in the page tracks the last successful
server round-trip and overlays a "connection lost" banner after ~30 s of
silence, clearing itself when the server comes back.

## Running it

From the repo root, in the project's conda env:

```bash
python -m livedash
```

Then open `http://127.0.0.1:8050`. First launch on a cold cache downloads price
history and can take a minute; afterwards it's fast.

### Configuration (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `LIVEDASH_CSV_DIR` | `csvs/fidelity/roth` | Which account's CSVs to display |
| `LIVEDASH_PORT` / `LIVEDASH_HOST` | `8050` / `127.0.0.1` | Bind address |
| `LIVEDASH_ROTATE` | `30` | View auto-rotate seconds (`0` = no rotation) |
| `LIVEDASH_REFRESH_OPEN` | `20` | Quote poll seconds while market open |
| `LIVEDASH_REFRESH_CLOSED` | `300` | Quote poll seconds through the post-close evening |
| `LIVEDASH_STALE_AFTER` | `75` | Data-age (s) that trips the stale banner |
| `LIVEDASH_BLUR` | `false` | Start with dollar amounts blurred |
| `LIVEDASH_DB` | `livedash_snapshots.db` | SQLite file for end-of-day snapshots |

Colors and fonts live in `livedash/config.py` (Python side) and
`livedash/assets/style.css` (browser side) — retune the whole look in one place.

## Deploying on the Raspberry Pi

The app is the easy part; keeping an unattended panel healthy for months is
where these builds usually fail. The pieces that matter:

**1. Run the server under systemd with restart-always** —
`~/.config/systemd/user/livedash.service`:

```ini
[Unit]
Description=Live portfolio dashboard
After=network-online.target

[Service]
WorkingDirectory=/home/pi/finance_sim
ExecStart=/home/pi/miniconda3/envs/financetracking/bin/python -m livedash
Environment=LIVEDASH_HOST=0.0.0.0
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
```

`systemctl --user enable --now livedash` (and `loginctl enable-linger pi` so it
starts without a login).

**2. Chromium in kiosk mode**, pointed at the local server, launched from your
desktop autostart:

```bash
chromium-browser --kiosk --incognito \
  --noerrdialogs --disable-infobars \
  --check-for-update-interval=31536000 \
  http://localhost:8050
```

**3. Disable screen blanking.** On X11: `xset s off -dpms`. On newer Raspberry
Pi OS (Wayland/labwc) the incantation changed — use
`wlr-randr` / the labwc autostart, or `swayidle` with an empty timeout.

**4. Nightly reload around 4am** so a long-running JS page doesn't leak memory.
Simplest is a cron that restarts the browser, e.g.
`0 4 * * * pkill -HUP chromium` (or restart the kiosk unit).

**5. Dim the backlight after hours** — a 10" panel at full brightness in a dark
room is the main reason these get unplugged. Write to
`/sys/class/backlight/*/brightness` from a cron (low in the evening, full in the
morning).

**6. Poll only when there's something to fetch** — already handled. The poller
follows the market phase rather than a flat open/closed switch: fast while the
tape runs, `LIVEDASH_REFRESH_CLOSED` through the evening that the closing print
and fund NAVs land in, then **nothing at all** until a minute before the next
open, once every displayed ticker has its final price for the session. That
skips roughly 3,000 requests a week that would return identical bytes, and the
wake-ahead means the panel opens the session live instead of spending the first
few minutes under a stale banner. The UI ticks stay fast (so the readout and the
stale banner remain honest) but re-render nothing unless the data behind them
moved: the positions table waits on a new quote snapshot, and each figure waits
on the inputs it actually uses — vs Market, for instance, only redraws when you
change the time window. Steady-state that's a ~180-byte response per tick.
```
