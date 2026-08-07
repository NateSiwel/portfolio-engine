"""The Dash application: layout, callbacks, and the live-render loop.

Design for a smooth always-on panel:

* A background thread keeps quotes fresh at the market-aware cadence.
* Every view is mounted at once and stacked; switching between them is a
  **clientside** class toggle (a CSS crossfade), so a tap never hits the server
  or rebuilds a figure — it's instant regardless of how large the charts are.
* The server only refreshes *content* in the background: cheap hero/positions
  text on a short tick, and the (heavier) figures on the slower quote cadence,
  updated in place via Plotly.react. The clock ticks entirely clientside.
* Both content callbacks are **gated on the data actually changing**, not on
  the wall clock. The ticks are fast so the age readout and the stale banner
  stay honest, but rebuilding a positions table (one SVG sparkline per row) or
  re-serializing a figure only happens when the inputs that figure depends on
  have moved. Everything else returns `no_update` and costs an empty response.
"""

from __future__ import annotations

import os
import threading
from datetime import date

import plotly.graph_objects as go
from dash import ALL, Dash, Input, Output, State, ctx, dcc, html, no_update

from . import earnings, figures
from .config import Config
from .earnings import EarningsStore
from .market import close_has_printed, is_market_open, now_et, session_label
from .portfolio import Portfolio, ViewModel
from .quotes import QuoteService, run_poller
from .sparkline import sparkline
from .store import SnapshotStore

VIEWS_BASE = ["Overview", "Treemap", "History", "Movers", "vs Market"]
VIEW_SLUGS = {
    "Overview": "overview",
    "Treemap": "treemap",
    "History": "history",
    "Movers": "movers",
    "vs Market": "vsmarket",
}
# Views spanning the full history get a time-window selector; the others
# (Overview/Treemap/Movers) are intraday-only, so a window has no meaning there.
WINDOWED_VIEWS = {"History", "vs Market"}
DEFAULT_WINDOW = "1M"


# --------------------------------------------------------------------------- #
# Formatting helpers
# --------------------------------------------------------------------------- #
def money(x: float, blur: bool, *, signed: bool = False, cents: bool = False) -> str:
    if blur:
        return "•••"
    fmt = ",.2f" if cents else ",.0f"
    if signed:
        return f"{'+' if x >= 0 else '−'}${abs(x):{fmt}}"
    return f"${x:{fmt}}"


def pct(x: float | None, *, signed: bool = True) -> str:
    if x is None:
        return "—"
    return f"{x:+.2f}%" if signed else f"{x:.2f}%"


def arrow(x: float | None) -> str:
    if x is None:
        return ""
    return "▲" if x >= 0 else "▼"


def age_label(secs: float | None) -> str:
    """Data age, in whatever unit reads at a glance.

    Now that the poller sleeps through the night and the weekend rather than
    re-fetching prices that can't have changed, this legitimately reaches days
    — and "226103s ago" is not something anyone should have to parse.
    """
    if secs is None:
        return "no data"
    if secs < 90:
        return f"{secs:.0f}s ago"
    if secs < 90 * 60:
        return f"{secs / 60:.0f}m ago"
    if secs < 48 * 3600:
        return f"{secs / 3600:.0f}h ago"
    return f"{secs / 86400:.0f}d ago"


# --------------------------------------------------------------------------- #
# View renderers
# --------------------------------------------------------------------------- #
def render_hero(vm: ViewModel, cfg: Config, blur: bool) -> html.Div:
    color = cfg.color_for(vm.day_change)
    bench = vm.benchmarks.get(cfg.benchmarks[0], {})
    bench_pct = bench.get("day_pct")
    # Beating/lagging the headline benchmark today is the "should I care" signal.
    rel = None if bench_pct is None else vm.day_pct - bench_pct

    return html.Div(
        className="hero",
        children=[
            html.Div(
                className="hero-left",
                children=[
                    html.Div("PORTFOLIO", className="hero-label"),
                    html.Div(money(vm.total_value, blur), className="hero-total"),
                ],
            ),
            html.Div(
                className="hero-right",
                style={"color": color},
                children=[
                    html.Div(
                        [
                            html.Span(arrow(vm.day_change), className="hero-arrow"),
                            html.Span(
                                money(vm.day_change, blur, signed=True),
                                className="hero-change",
                            ),
                        ]
                    ),
                    html.Div(
                        [
                            html.Span(pct(vm.day_pct), className="hero-pct"),
                            html.Span(" today", className="hero-suffix"),
                        ]
                    ),
                    html.Div(
                        className="hero-bench",
                        children=(
                            f"{cfg.benchmarks[0]} {pct(bench_pct)} · "
                            f"{'beating' if rel >= 0 else 'lagging'} by {abs(rel):.2f} pts"
                            if rel is not None
                            else f"{cfg.benchmarks[0]} —"
                        ),
                    ),
                ],
            ),
        ],
    )


def _position_row(p, cfg: Config, blur: bool, today: date) -> html.Div:
    color = cfg.color_for(p.day_pct if p.day_pct is not None else 0)
    tkr_children = [
        html.Div(p.ticker, className="tkr-sym"),
        html.Div(f"{p.shares:g} sh", className="tkr-sh"),
    ]
    # A same-day/this-week earnings date is the "why is this mover moving"
    # signal — surfaced once daily, never re-checked intraday.
    earn_label = earnings.tag(p.earnings_date, today)
    if earn_label:
        tkr_children.append(html.Div(earn_label, className="tkr-earn"))
    return html.Div(
        className="pos-row",
        children=[
            html.Div(tkr_children, className="c-tkr"),
            html.Div(
                sparkline(
                    p.intraday,
                    baseline=p.prev_close,
                    up=cfg.up,
                    down=cfg.down,
                    width=180,
                    height=48,
                    muted=p.shape_from_proxy,
                ),
                className="c-spark",
            ),
            # "≈" = price estimated from a proxy ETF (mutual funds have no tape
            # until NAV posts); the flag clears once the real NAV lands. The
            # curve stays proxy-shaped (drawn faint) even after that.
            html.Div(
                ("≈ " if p.estimated else "") + money(p.price, blur, cents=True),
                className="c-price",
            ),
            html.Div(
                [
                    html.Span(arrow(p.day_pct)),
                    " ",
                    ("≈ " if p.estimated else "") + pct(p.day_pct),
                ],
                className="c-daypct",
                style={"color": color},
            ),
            html.Div(
                money(p.day_change, blur, signed=True)
                if p.day_change is not None
                # No prior close to measure against — "+$0" would read as a flat
                # day rather than as the unknown it is.
                else "—",
                className="c-daychg",
                style={"color": color if p.day_change is not None else cfg.text_dim},
            ),
            html.Div(money(p.value, blur), className="c-value"),
            html.Div(
                html.Div(
                    html.Div(
                        className="wbar-fill",
                        style={"width": f"{min(p.weight, 100):.0f}%"},
                    ),
                    className="wbar",
                ),
                className="c-weight",
                title=f"{p.weight:.1f}%",
            ),
        ],
    )


def render_positions(vm: ViewModel, cfg: Config, blur: bool) -> list:
    header = html.Div(
        className="pos-row pos-head",
        children=[
            html.Div("TICKER", className="c-tkr"),
            html.Div("", className="c-spark"),
            html.Div("PRICE", className="c-price"),
            html.Div("DAY", className="c-daypct"),
            html.Div("CHANGE", className="c-daychg"),
            html.Div("VALUE", className="c-value"),
            html.Div("WEIGHT", className="c-weight"),
        ],
    )
    today = now_et().date()
    rows = [_position_row(p, cfg, blur, today) for p in vm.positions]
    if vm.cash > 0:
        rows.append(
            html.Div(
                className="pos-row pos-cash",
                children=[
                    html.Div("CASH", className="c-tkr tkr-sym"),
                    html.Div("", className="c-spark"),
                    html.Div("", className="c-price"),
                    html.Div("", className="c-daypct"),
                    html.Div("", className="c-daychg"),
                    html.Div(money(vm.cash, blur), className="c-value"),
                    html.Div("", className="c-weight"),
                ],
            )
        )
    # Header stays pinned; data rows spread to fill the panel so a few holdings
    # don't huddle at the top of a 1920x1200 screen.
    return [header, html.Div(rows, className="pos-rows")]


GRAPH_TITLES = {
    "Treemap": "Allocation · sized by weight, colored by today",
    "History": "Portfolio value",
    "Movers": "Today's movers",
    "vs Market": "Time-weighted return vs benchmarks",
}


def build_figure(
    view: str,
    vm: ViewModel,
    pf: Portfolio,
    cfg: Config,
    blur: bool = False,
    window: str = DEFAULT_WINDOW,
):
    if view == "Treemap":
        return figures.treemap(vm, cfg)
    if view == "History":
        return figures.history(pf, vm, cfg, blur, window)
    if view == "Movers":
        return figures.movers(vm, cfg)
    if view == "vs Market":
        return figures.vs_market(pf, cfg, window)
    return go.Figure()


def figure_key(view: str, snap_key: list, blur: bool, window: str) -> list:
    """The inputs `view`'s figure actually depends on.

    Rebuilding a figure means re-serializing every point of every trace, so
    each one is skipped while its own key holds still. The dependencies really
    are this uneven: vs Market is a pure function of the time window and never
    reads a quote, while Treemap and Movers ignore both the window and blur.
    """
    if view == "History":
        return [snap_key, blur, window]
    if view == "vs Market":
        return [window]
    return [snap_key]


# --------------------------------------------------------------------------- #
# App factory
# --------------------------------------------------------------------------- #
def create_app(cfg: Config | None = None) -> tuple[Dash, Portfolio]:
    cfg = cfg or Config()

    print("[livedash] loading portfolio (this can take a while on a cold cache)…")
    pf = Portfolio(csv_dir=cfg.csv_dir, benchmarks=cfg.benchmarks).load()
    print(
        f"[livedash] {len(pf.tickers)} holdings, "
        f"{len(pf.history_dates)} days of history."
    )

    views = list(VIEWS_BASE)
    if not pf.twr:
        views.remove("vs Market")

    service = QuoteService(pf.quote_tickers, proxies=cfg.fund_proxies)
    service.refresh_blocking()  # first paint has live data
    store = SnapshotStore(cfg.snapshot_db)

    # Next-earnings-date per holding — a once-daily lookup, not part of the
    # quote tape, so it's fetched synchronously here (first paint has it) and
    # then only re-checked on a day rollover by the background thread below.
    earnings_store = EarningsStore(cfg.snapshot_db)
    earnings.refresh_stale(pf.tickers, earnings_store)
    pf.earnings = earnings_store.get_all()

    stop = threading.Event()
    threading.Thread(target=run_poller, args=(service, cfg, stop), daemon=True).start()
    threading.Thread(
        target=earnings.run_earnings_refresher,
        args=(pf, earnings_store, stop),
        daemon=True,
    ).start()

    blur0 = cfg.start_blurred
    vm0 = pf.build_view_model(service.snapshot, cfg.stale_after_secs)
    graph_cfg = {"displayModeBar": False, "staticPlot": True, "responsive": True}

    def panel(i: int, view: str) -> html.Div:
        if view == "Overview":
            content = [
                html.Div(
                    render_positions(vm0, cfg, blur0),
                    id="overview-body",
                    className="positions",
                )
            ]
        else:
            slug = VIEW_SLUGS[view]
            head = [html.Div(GRAPH_TITLES.get(view, view), className="view-title")]
            if view in WINDOWED_VIEWS:
                # Each windowed panel carries its own button strip (all panels
                # stay mounted); one shared store keeps every strip in sync.
                head.append(
                    html.Div(
                        [
                            html.Button(
                                w,
                                id={"type": "winbtn", "view": slug, "win": w},
                                className="winbtn active"
                                if w == DEFAULT_WINDOW
                                else "winbtn",
                                n_clicks=0,
                            )
                            for w in figures.WINDOWS
                        ],
                        className="winbtns",
                    )
                )
            content = [
                html.Div(head, className="view-head"),
                dcc.Graph(
                    id=f"fig-{slug}",
                    className="view-graph",
                    config=graph_cfg,
                    figure=build_figure(view, vm0, pf, cfg, blur0),
                ),
            ]
        return html.Div(
            id={"type": "panel", "index": i},
            className="panel active" if i == 0 else "panel",
            children=content,
        )

    assets = os.path.join(os.path.dirname(__file__), "assets")
    app = Dash(
        __name__,
        assets_folder=assets,
        title="Portfolio",
        update_title=None,
        suppress_callback_exceptions=True,
    )
    rotate_ms = max(cfg.rotate_secs, 1) * 1000
    app.index_string = _INDEX.replace("__ROTATE_MS__", str(rotate_ms))
    app._livedash_stop = stop  # keep a handle so it isn't GC'd

    app.layout = html.Div(
        id="root",
        children=[
            dcc.Interval(id="clock-tick", interval=1000),
            dcc.Interval(id="tick", interval=3000),
            dcc.Interval(id="fig-tick", interval=max(cfg.refresh_open_secs, 5) * 1000),
            dcc.Interval(
                id="rotate",
                interval=rotate_ms,
                disabled=cfg.rotate_secs == 0,
            ),
            dcc.Store(id="view-index", data=0),
            dcc.Store(id="blur", data=blur0),
            dcc.Store(id="paused", data=False),
            dcc.Store(id="window", data=DEFAULT_WINDOW),
            dcc.Store(id="client-beat"),
            # What the browser is currently showing, so the server can skip
            # rebuilding it. Client-side state on purpose: a reload or a second
            # viewer starts from a blank key and gets a full render.
            dcc.Store(id="render-key"),
            dcc.Store(id="fig-keys"),
            html.Div(id="stale-banner", className="stale-banner"),
            html.Div(
                className="topbar",
                children=[
                    html.Div(
                        className="clock",
                        children=[
                            html.Div(id="clock-time", className="clock-time"),
                            html.Div(id="clock-sub", className="clock-sub"),
                        ],
                    ),
                    html.Div(
                        [
                            html.Button(
                                v,
                                id={"type": "viewbtn", "index": i},
                                className="viewbtn active" if i == 0 else "viewbtn",
                                n_clicks=0,
                            )
                            for i, v in enumerate(views)
                        ],
                        className="viewbtns",
                    ),
                    html.Div(
                        [
                            html.Button(
                                "Hold", id="pausebtn", className="iconbtn", n_clicks=0
                            ),
                            html.Button(
                                "Privacy", id="blurbtn", className="iconbtn", n_clicks=0
                            ),
                        ],
                        className="controls",
                    ),
                ],
            ),
            html.Div(
                id="screen",
                className="screen",
                children=[
                    html.Div(render_hero(vm0, cfg, blur0), id="hero"),
                    html.Div(
                        [panel(i, v) for i, v in enumerate(views)],
                        id="stage",
                        className="stage",
                    ),
                ],
            ),
        ],
    )

    _register_callbacks(app, pf, service, store, cfg, views)
    return app, pf


def _register_callbacks(app, pf, service, store, cfg, views):
    n_views = len(views)
    fig_views = [v for v in views if v != "Overview"]

    # --- view index: rotation tick or a tapped tab (tiny server round-trip) ---
    @app.callback(
        Output("view-index", "data"),
        Input("rotate", "n_intervals"),
        Input({"type": "viewbtn", "index": ALL}, "n_clicks"),
        State("view-index", "data"),
        prevent_initial_call=True,
    )
    def switch_view(_rot, _clicks, current):
        trig = ctx.triggered_id
        if isinstance(trig, dict) and trig.get("type") == "viewbtn":
            return trig["index"]
        return ((current or 0) + 1) % n_views

    # --- instant switch: toggle the active panel entirely in the browser ------
    app.clientside_callback(
        "function(idx, ids){ idx = idx || 0;"
        " return ids.map(function(_, i){ return i === idx ? 'panel active' : 'panel'; }); }",
        Output({"type": "panel", "index": ALL}, "className"),
        Input("view-index", "data"),
        State({"type": "panel", "index": ALL}, "id"),
    )

    # --- keep the tab strip in sync with the visible panel --------------------
    app.clientside_callback(
        "function(idx, ids){ idx = idx || 0;"
        " return ids.map(function(_, i){ return i === idx ? 'viewbtn active' : 'viewbtn'; }); }",
        Output({"type": "viewbtn", "index": ALL}, "className"),
        Input("view-index", "data"),
        State({"type": "viewbtn", "index": ALL}, "id"),
    )

    # --- time window: a tapped chip updates the shared store ------------------
    @app.callback(
        Output("window", "data"),
        Input({"type": "winbtn", "view": ALL, "win": ALL}, "n_clicks"),
        prevent_initial_call=True,
    )
    def set_window(_clicks):
        trig = ctx.triggered_id
        if isinstance(trig, dict) and trig.get("type") == "winbtn":
            return trig["win"]
        return DEFAULT_WINDOW

    # --- highlight the active window chip on every windowed panel -------------
    app.clientside_callback(
        "function(win, ids){ return ids.map(function(id){"
        " return id.win === win ? 'winbtn active' : 'winbtn'; }); }",
        Output({"type": "winbtn", "view": ALL, "win": ALL}, "className"),
        Input("window", "data"),
        State({"type": "winbtn", "view": ALL, "win": ALL}, "id"),
    )

    # --- heartbeat: every successful server round-trip stamps the browser -----
    # The index page's watchdog raises a banner when this stops advancing, so a
    # killed/crashed server can't leave confident frozen numbers on screen (the
    # server-side stale banner can't help when the server itself is gone).
    app.clientside_callback(
        "function(_){ window.__livedashBeat = Date.now(); return null; }",
        Output("client-beat", "data"),
        Input("clock-sub", "children"),
    )

    # --- clock: ticks every second in market time, no server involved ---------
    app.clientside_callback(
        "function(n){ try { return new Date().toLocaleTimeString('en-US',"
        "{hour:'numeric',minute:'2-digit',second:'2-digit',hour12:true,"
        "timeZone:'America/New_York'}); } catch(e){ return ''; } }",
        Output("clock-time", "children"),
        Input("clock-tick", "n_intervals"),
    )

    @app.callback(
        Output("blur", "data"),
        Input("blurbtn", "n_clicks"),
        State("blur", "data"),
        prevent_initial_call=True,
    )
    def toggle_blur(_n, blurred):
        return not blurred

    @app.callback(
        Output("paused", "data"),
        Output("rotate", "disabled"),
        Output("pausebtn", "children"),
        Input("pausebtn", "n_clicks"),
        State("paused", "data"),
        prevent_initial_call=True,
    )
    def toggle_pause(_n, paused):
        paused = not paused
        return paused, paused or cfg.rotate_secs == 0, ("Resume" if paused else "Hold")

    # --- content refresh: cheap text every tick, heavy rows only on new data --
    @app.callback(
        Output("hero", "children"),
        Output("overview-body", "children"),
        Output("clock-sub", "children"),
        Output("stale-banner", "children"),
        Output("stale-banner", "className"),
        Output("screen", "className"),
        Output("render-key", "data"),
        Input("tick", "n_intervals"),
        Input("blur", "data"),
        State("render-key", "data"),
    )
    def refresh_content(_tick, blur, prev_key):
        snap = service.snapshot
        blur = bool(blur)

        # The age readout and the stale state do change on every tick, and both
        # come straight off the snapshot — no view model needed to produce them.
        age = age_label(snap.age_secs())
        sub = f"{session_label(now_et())} · updated {age}"
        if snap.is_stale(cfg.stale_after_secs):
            banner = f"⚠ STALE DATA — last update {age}. Numbers may be out of date."
            banner_cls, screen_cls = "stale-banner show", "screen stale"
        else:
            banner, banner_cls, screen_cls = "", "stale-banner", "screen"

        # The expensive half — every position row, each carrying a freshly
        # rendered SVG sparkline — is worth redoing only when a new quote
        # snapshot (or a blur toggle) has something different to say.
        key = [snap.render_key(), blur]
        if key == prev_key:
            return no_update, no_update, sub, banner, banner_cls, screen_cls, no_update

        vm = pf.build_view_model(snap, cfg.stale_after_secs)
        _maybe_snapshot(vm, store)
        return (
            render_hero(vm, cfg, blur),
            render_positions(vm, cfg, blur),
            sub,
            banner,
            banner_cls,
            screen_cls,
            key,
        )

    # --- figures refresh: heavier, on the slower quote cadence; Plotly.react
    #     updates them in place, and it happens off the interaction path -------
    @app.callback(
        *[Output(f"fig-{VIEW_SLUGS[v]}", "figure") for v in fig_views],
        Output("fig-keys", "data"),
        Input("fig-tick", "n_intervals"),
        Input("blur", "data"),
        Input("window", "data"),
        State("fig-keys", "data"),
    )
    def refresh_figures(_tick, blur, window, prev_keys):
        snap = service.snapshot
        blur = bool(blur)
        window = window or DEFAULT_WINDOW
        snap_key = snap.render_key()

        keys = [figure_key(v, snap_key, blur, window) for v in fig_views]
        if not isinstance(prev_keys, list) or len(prev_keys) != len(keys):
            prev_keys = [None] * len(keys)  # first paint, or a reloaded client
        if keys == prev_keys:
            return [no_update] * (len(keys) + 1)

        vm = pf.build_view_model(snap, cfg.stale_after_secs)
        figs = [
            build_figure(v, vm, pf, cfg, blur, window) if k != prev else no_update
            for v, k, prev in zip(fig_views, keys, prev_keys)
        ]
        return [*figs, keys]

    # --- figure cadence tracks the market, so an overnight panel isn't ticking
    #     at trading speed against a snapshot that moves every five minutes ----
    @app.callback(
        Output("fig-tick", "interval"),
        Input("fig-tick", "n_intervals"),
        State("fig-tick", "interval"),
        prevent_initial_call=True,
    )
    def fig_cadence(_tick, current):
        # Capped below refresh_closed_secs so the first figures after the
        # opening bell land promptly instead of up to five minutes late.
        secs = (
            cfg.refresh_open_secs
            if is_market_open()
            else min(cfg.refresh_closed_secs, 60)
        )
        interval = max(secs, 5) * 1000
        return interval if interval != current else no_update


# One snapshot per calendar day, written once the session has closed so the
# recorded number is the settled end-of-day value.
_last_snapshot_day: date | None = None


def _maybe_snapshot(vm: ViewModel, store: SnapshotStore) -> None:
    global _last_snapshot_day
    today = now_et().date()
    if _last_snapshot_day == today:
        return
    if is_market_open():
        return  # wait for the close so the value is settled
    # Being past the bell isn't enough: the official closing auction print lands
    # a few minutes later, and what's on screen is only settled if the quotes
    # behind it were fetched after that. Anything earlier — a provisional 16:00
    # price, or pre-market showing last night's numbers — would be written into
    # the permanent record as today's close.
    as_of = vm.as_of
    if as_of is None or as_of.date() != today or not close_has_printed(as_of):
        return
    if vm.total_value <= 0:
        return
    store.record(today, vm.total_value)
    _last_snapshot_day = today


_INDEX = """<!DOCTYPE html>
<html>
<head>
  {%metas%}
  <meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
  <title>{%title%}</title>
  {%favicon%}
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500;600&display=swap" rel="stylesheet">
  {%css%}
</head>
<body>
  <div id="client-stale">⚠ Connection lost — display frozen at last update</div>
  {%app_entry%}
  <footer>{%config%}{%scripts%}{%renderer%}</footer>
  <script>
    /* Dead-server watchdog. The server-rendered stale banner can't fire if the
       server process is gone, so the browser itself tracks the last successful
       round-trip (stamped by the heartbeat callback) and raises this banner
       after ~10 missed 3s ticks. */
    (function () {
      window.__livedashBeat = Date.now();
      setInterval(function () {
        var el = document.getElementById("client-stale");
        if (!el) return;
        var age = (Date.now() - window.__livedashBeat) / 1000;
        el.style.display = age > 30 ? "block" : "none";
      }, 5000);
    })();

    /* Restart the rotation countdown whenever someone touches the screen.
       Auto-rotation exists for an unattended panel; if you've just tapped a tab
       you want the full dwell time on it, not whatever fraction of the cycle
       happened to be left. dcc.Interval only rebuilds its timer when the
       `interval` prop actually *changes*, so alternate between N and N+1 ms —
       invisible in practice, but enough for React to see a new value. */
    (function () {
      var BASE = __ROTATE_MS__;
      var flip = false;
      var last = 0;
      function restart() {
        var dc = window.dash_clientside;
        if (!dc || !dc.set_props) return;
        /* Throttle: a scroll fires touchmove continuously and every prop write
           costs a render pass over the whole tree, which a Pi feels. Skipping
           is safe — a reset within the last second already left the countdown
           full — and it keeps a burst of events from batching into a no-op. */
        var now = Date.now();
        if (now - last < 1000) return;
        last = now;
        flip = !flip;
        dc.set_props("rotate", {interval: flip ? BASE + 1 : BASE});
      }
      ["pointerdown", "keydown", "wheel", "touchmove"].forEach(function (ev) {
        document.addEventListener(ev, restart, {capture: true, passive: true});
      });
    })();
  </script>
</body>
</html>"""
