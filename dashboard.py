"""
Interactive portfolio visualization.

Data format per snapshot:
    (date, {'CASH': Decimal, 'TICKER': (shares, price, total_value), ...})

Produces a single self-contained HTML file with several linked views
(switchable via buttons), a range slider, unified hover, and a
per-holding visibility legend.

All tickers, colors, orderings, and date labels are derived from the
data at runtime — nothing portfolio-specific is hardcoded.
"""

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

# Plotly's default qualitative palette, cycled if there are more holdings.
PALETTE = [
    "#636EFA",
    "#EF553B",
    "#00CC96",
    "#AB63FA",
    "#FFA15A",
    "#19D3F3",
    "#FF6692",
    "#B6E880",
    "#FF97FF",
    "#FECB52",
]

CASH_TICKER = "CASH"


BENCH_COLORS = ["#7f7f7f", "#ff7f0e", "#9467bd", "#8c564b"]


def display_data(
    RAW,
    output_path="portfolio_dashboard.html",
    market_comparisons=None,
    dividend_events=None,
    dividend_summary=None,
):
    """Render the dashboard.

    market_comparisons: optional {ticker: (dates, portfolio_curve, benchmark_curve)}
    from compare_to_market; adds a "vs Market" view plotting the portfolio's
    time-weighted return against each benchmark.

    dividend_events / dividend_summary: optional DataFrames from
    dividend_tracker.dividend_events / dividend_summary; add a monthly
    dividend income view and a per-ticker outlook view (projected forward
    income bars, yield-on-cost and the rest of the metrics on hover).
    """
    # ---------------------------------------------------------------------------
    # Reshape into a tidy DataFrame: one row per (date, holding)
    # ---------------------------------------------------------------------------
    rows = []
    for date, holdings in RAW:
        if holdings is None:
            # print(f"Warning: No holdings data for {date}, skipping...")
            continue
        for ticker, payload in holdings.items():
            if ticker == CASH_TICKER:
                rows.append(
                    dict(
                        date=date,
                        ticker=CASH_TICKER,
                        shares=None,
                        price=None,
                        value=float(payload),
                    )
                )
            else:
                shares, price, value = payload
                rows.append(
                    dict(
                        date=date,
                        ticker=ticker,
                        shares=float(shares),
                        price=float(price),
                        value=float(value),
                    )
                )

    if not rows:
        raise ValueError("No usable snapshots in RAW — nothing to plot.")

    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"])

    # ---------------------------------------------------------------------------
    # Derive tickers, ordering, and colors from the data
    # ---------------------------------------------------------------------------
    # Order by value on the latest date, largest first; any ticker that has
    # since been sold out (absent on the latest date) is appended at the end.
    latest_date = df["date"].max()
    latest_snapshot = df[df["date"] == latest_date].sort_values(
        "value", ascending=False
    )
    tickers = latest_snapshot["ticker"].tolist()
    tickers += [t for t in df["ticker"].unique() if t not in tickers]

    colors = {t: PALETTE[i % len(PALETTE)] for i, t in enumerate(tickers)}

    pivot_value = (
        df.pivot(index="date", columns="ticker", values="value")
        .reindex(columns=tickers)
        .fillna(0.0)
    )  # absent = not held that day
    pivot_price = df.pivot(index="date", columns="ticker", values="price").reindex(
        columns=tickers
    )
    total = pivot_value.sum(axis=1)
    dates = pivot_value.index

    # Priced holdings (everything with at least one price, i.e. not cash).
    price_tickers = [t for t in tickers if pivot_price[t].notna().any()]

    # % change indexed to each ticker's first available price.
    first_price = pivot_price.apply(
        lambda s: s.dropna().iloc[0] if s.notna().any() else float("nan")
    )
    pct_change = (pivot_price / first_price - 1) * 100

    # ---------------------------------------------------------------------------
    # Build figure: 4 view groups toggled by updatemenu buttons
    #   0) Total portfolio value (line + fill, with daily change bars)
    #   1) Stacked area by holding
    #   2) Individual prices, indexed % change
    #   3) Latest-day allocation donut
    # ---------------------------------------------------------------------------
    fig = make_subplots(specs=[[{"secondary_y": True}]])
    groups = []  # parallel list marking which view each trace belongs to

    # --- View 0: total value ---------------------------------------------------
    fig.add_trace(
        go.Scatter(
            x=dates,
            y=total,
            name="Total value",
            mode="lines+markers",
            line=dict(color="#1f77b4", width=3),
            fill="tozeroy",
            fillcolor="rgba(31,119,180,0.12)",
            hovertemplate="%{x|%a %b %d}<br>Total: $%{y:,.2f}<extra></extra>",
        )
    )
    groups.append(0)

    daily_change = total.diff()
    fig.add_trace(
        go.Bar(
            x=dates,
            y=daily_change,
            name="Daily change",
            marker_color=[
                "#2ca02c" if v >= 0 else "#d62728" for v in daily_change.fillna(0)
            ],
            opacity=0.6,
            hovertemplate="%{x|%a %b %d}<br>Change: $%{y:,.2f}<extra></extra>",
        ),
        secondary_y=True,
    )
    groups.append(0)

    # --- View 1: stacked area --------------------------------------------------
    for t in tickers:
        fig.add_trace(
            go.Scatter(
                x=dates,
                y=pivot_value[t],
                name=t,
                mode="lines",
                stackgroup="alloc",
                line=dict(width=0.5, color=colors[t]),
                hovertemplate=f"{t}: $%{{y:,.2f}}<extra></extra>",
            )
        )
        groups.append(1)

    # --- View 2: price % change ------------------------------------------------
    for t in price_tickers:
        fig.add_trace(
            go.Scatter(
                x=dates,
                y=pct_change[t],
                name=f"{t} %",
                mode="lines+markers",
                line=dict(color=colors[t], width=2.5),
                customdata=pivot_price[t],
                hovertemplate=(
                    f"{t}: %{{y:+.2f}}%<br>Price: $%{{customdata:,.2f}}<extra></extra>"
                ),
            )
        )
        groups.append(2)
    # Zero line for the % views (2 and 4); the buttons toggle its visibility.
    fig.add_hline(y=0, line_dash="dot", line_color="gray", opacity=0.5, visible=False)

    # --- View 3: latest allocation donut --------------------------------------
    latest = pivot_value.iloc[-1]
    latest = latest[latest > 0]  # hide sold-out holdings
    fig.add_trace(
        go.Pie(
            labels=latest.index.tolist(),
            values=latest.values,
            hole=0.45,
            marker=dict(colors=[colors[t] for t in latest.index]),
            textinfo="label+percent",
            hovertemplate="%{label}: $%{value:,.2f} (%{percent})<extra></extra>",
            visible=False,
        )
    )
    groups.append(3)

    # --- View 4: TWR vs market benchmarks --------------------------------------
    market_comparisons = market_comparisons or {}
    if market_comparisons:
        # The portfolio curve is identical across comparisons; take the first.
        cmp_dates, port_curve, _ = next(iter(market_comparisons.values()))
        fig.add_trace(
            go.Scatter(
                x=list(cmp_dates),
                y=[(g - 1) * 100 for g in port_curve],
                name="Portfolio (TWR)",
                mode="lines",
                line=dict(color="#1f77b4", width=3),
                hovertemplate="Portfolio: %{y:+.2f}%<extra></extra>",
            )
        )
        groups.append(4)

        for i, (ticker, (b_dates, _, bench_curve)) in enumerate(
            market_comparisons.items()
        ):
            fig.add_trace(
                go.Scatter(
                    x=list(b_dates),
                    y=[(g - 1) * 100 for g in bench_curve],
                    name=ticker,
                    mode="lines",
                    line=dict(
                        color=BENCH_COLORS[i % len(BENCH_COLORS)],
                        width=2,
                        dash="dash",
                    ),
                    hovertemplate=f"{ticker}: %{{y:+.2f}}%<extra></extra>",
                )
            )
            groups.append(4)

    # --- View 5: dividend income by month + cumulative --------------------------
    has_div_events = dividend_events is not None and len(dividend_events) > 0
    if has_div_events:
        from dividend_tracker import income_by_period

        monthly = income_by_period(dividend_events, "M")
        month_x = monthly.index.to_timestamp()  # bars sit at each month's start
        payers = sorted(
            (c for c in monthly.columns if c != "Total"),
            key=lambda t: -monthly[t].sum(),
        )
        extra = 0  # payers never held as positions (no color assigned yet)
        for t in payers:
            color = colors.get(t)
            if color is None:
                color = PALETTE[(len(tickers) + extra) % len(PALETTE)]
                extra += 1
            fig.add_trace(
                go.Bar(
                    x=month_x,
                    y=monthly[t],
                    name=t,
                    marker_color=color,
                    hovertemplate=f"{t}: $%{{y:,.2f}}<extra></extra>",
                )
            )
            groups.append(5)
        fig.add_trace(
            go.Scatter(
                x=month_x,
                y=monthly["Total"].cumsum(),
                name="Cumulative",
                mode="lines+markers",
                line=dict(color="#1f77b4", width=2.5, dash="dot"),
                hovertemplate="Cumulative: $%{y:,.2f}<extra></extra>",
            ),
            secondary_y=True,
        )
        groups.append(5)

    # --- View 6: dividend outlook (projected income per ticker) -----------------
    # Horizontal bars on a pair of overlaid axes (xaxis2/yaxis3): the shared
    # xaxis is date-typed, and a go.Table can't be used because plotly.js
    # crashes redrawing tables alongside a rangeslider figure.
    outlook = None
    if dividend_summary is not None and len(dividend_summary) > 0:
        s = dividend_summary
        payers_or_paid = s[(s["projected_income"] > 0) | (s["total_received"] > 0)]
        if len(payers_or_paid) > 0:
            outlook = payers_or_paid.sort_values("projected_income")
    has_div_summary = outlook is not None
    if has_div_summary:
        proj_total = float(outlook["projected_income"].sum())
        cost_total = float(outlook["cost_basis"].sum())
        fig.add_trace(
            go.Bar(
                x=outlook["projected_income"],
                y=outlook["symbol"],
                orientation="h",
                name="Projected income",
                marker_color=[colors.get(t, PALETTE[0]) for t in outlook["symbol"]],
                text=[
                    f"YoC {yoc:.2f}% · yield {cy:.2f}%"
                    for yoc, cy in zip(
                        outlook["yield_on_cost"], outlook["current_yield"]
                    )
                ],
                textposition="auto",
                customdata=outlook[
                    [
                        "yield_on_cost",
                        "current_yield",
                        "avg_cost",
                        "cost_basis",
                        "ttm_received",
                        "total_received",
                        "ttm_dps",
                    ]
                ].values,
                hovertemplate=(
                    "%{y}: $%{x:,.2f}/yr<br>"
                    "Yield on cost: %{customdata[0]:.2f}%"
                    " · Current yield: %{customdata[1]:.2f}%<br>"
                    "Avg cost: $%{customdata[2]:,.2f}"
                    " · Cost basis: $%{customdata[3]:,.2f}<br>"
                    "Received TTM: $%{customdata[4]:,.2f}"
                    " · All-time: $%{customdata[5]:,.2f}<br>"
                    "TTM rate: $%{customdata[6]:,.4f}/share"
                    "<extra></extra>"
                ),
                xaxis="x2",
                yaxis="y3",
                visible=False,
            )
        )
        groups.append(6)

    def vis(view):
        return [g == view for g in groups]

    axis_layouts = {
        0: dict(
            xaxis=dict(visible=True),
            yaxis=dict(visible=True, title="Portfolio value ($)"),
            yaxis2=dict(visible=True, title="Daily change ($)"),
            title="Total Portfolio Value",
        ),
        1: dict(
            xaxis=dict(visible=True),
            yaxis=dict(visible=True, title="Value ($)"),
            yaxis2=dict(visible=False),
            title="Allocation Over Time (stacked)",
        ),
        2: dict(
            xaxis=dict(visible=True),
            yaxis=dict(visible=True, title=f"% change since {dates[0]:%b %d}"),
            yaxis2=dict(visible=False),
            title="Price Performance (indexed)",
        ),
        3: dict(
            xaxis=dict(visible=False),
            yaxis=dict(visible=False),
            yaxis2=dict(visible=False),
            title=f"Current Allocation — {dates[-1]:%b %d, %Y}",
        ),
        4: dict(
            xaxis=dict(visible=True),
            yaxis=dict(visible=True, title="Return (%)"),
            yaxis2=dict(visible=False),
            title="Portfolio vs Market — time-weighted, contributions excluded",
        ),
        5: dict(
            xaxis=dict(visible=True),
            yaxis=dict(visible=True, title="Dividend income ($/month)"),
            yaxis2=dict(visible=True, title="Cumulative ($)"),
            title="Dividend Income by Month",
        ),
        6: dict(
            xaxis=dict(visible=False),
            yaxis=dict(visible=False),
            yaxis2=dict(visible=False),
            xaxis2=True,
            yaxis3=True,
            hovermode="closest",
            title=(
                "Dividend Outlook — trailing-12-month rate × current shares"
                + (
                    f" (proj. ${proj_total:,.2f}/yr"
                    + (
                        f", {proj_total / cost_total * 100:.2f}% on cost)"
                        if cost_total
                        else ")"
                    )
                    if has_div_summary
                    else ""
                )
            ),
        ),
    }

    view_labels = [
        (0, "Total value"),
        (1, "Stacked allocation"),
        (2, "Price performance"),
        (3, "Current allocation"),
        (4, "vs Market"),
        (5, "Dividend income"),
        (6, "Dividend outlook"),
    ]
    if not price_tickers:  # cash-only portfolio
        view_labels = [v for v in view_labels if v[0] != 2]
    if not market_comparisons:
        view_labels = [v for v in view_labels if v[0] != 4]
    if not has_div_events:
        view_labels = [v for v in view_labels if v[0] != 5]
    if not has_div_summary:
        view_labels = [v for v in view_labels if v[0] != 6]

    buttons = []
    for view, label in view_labels:
        lay = axis_layouts[view]
        buttons.append(
            dict(
                label=label,
                method="update",
                args=[
                    {"visible": vis(view)},
                    {
                        "title.text": lay["title"],
                        "xaxis.visible": lay["xaxis"]["visible"],
                        "yaxis.visible": lay["yaxis"]["visible"],
                        "yaxis.title.text": lay["yaxis"].get("title", ""),
                        "yaxis2.visible": lay["yaxis2"]["visible"],
                        "yaxis2.title.text": lay["yaxis2"].get("title", ""),
                        "xaxis2.visible": lay.get("xaxis2", False),
                        "yaxis3.visible": lay.get("yaxis3", False),
                        "hovermode": lay.get("hovermode", "x unified"),
                        # Hiding the axis doesn't hide its rangeslider, and the
                        # % change zero-line shape shows everywhere otherwise.
                        "xaxis.rangeslider.visible": lay["xaxis"]["visible"],
                        "shapes[0].visible": view in (2, 4),
                        # % views (2, 4) shouldn't inherit view 0's $ prefix.
                        "yaxis.tickprefix": "" if view in (2, 4) else "$",
                    },
                ],
            )
        )

    # start on view 0
    for i, tr in enumerate(fig.data):
        tr.visible = groups[i] == 0

    fig.update_layout(
        template="plotly_white",
        title=dict(text=axis_layouts[0]["title"], x=0.5, font=dict(size=22)),
        hovermode="x unified",
        barmode="stack",  # stacks the per-ticker dividend bars; other views
        # have at most one bar trace, so they're unaffected
        height=640,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0),
        updatemenus=[
            dict(
                type="buttons",
                direction="right",
                active=0,
                buttons=buttons,
                x=0.5,
                xanchor="center",
                y=1.18,
                yanchor="top",
                bgcolor="#f0f0f0",
                bordercolor="#cccccc",
            )
        ],
        xaxis=dict(
            title="Date",
            rangeslider=dict(visible=True, thickness=0.08),
            rangeselector=dict(
                buttons=[
                    dict(count=3, label="3d", step="day", stepmode="backward"),
                    dict(count=7, label="1w", step="day", stepmode="backward"),
                    dict(step="all", label="All"),
                ]
            ),
        ),
        yaxis=dict(title="Portfolio value ($)", tickprefix="$", tickformat=",.0f"),
        yaxis2=dict(title="Daily change ($)", showgrid=False),
        # Overlaid axis pair for the dividend-outlook bars: the shared xaxis
        # is date-typed, so dollar-valued horizontal bars need their own axes.
        xaxis2=dict(
            overlaying="x",
            visible=False,
            title="Projected annual income ($)",
            tickprefix="$",
        ),
        # No categoryorder here: "total ascending" crashes plotly.js while the
        # axis's only trace is hidden, so the data is pre-sorted instead.
        yaxis3=dict(overlaying="y", visible=False, type="category"),
        margin=dict(t=140),
    )

    fig.write_html(output_path, include_plotlyjs="cdn", full_html=True)
    try:
        fig.show()
    except Exception:
        pass  # no interactive renderer available; the HTML file is still saved
    print(f"Saved {output_path}")
    print(
        f"Total value on {dates[-1]:%b %d}: ${total.iloc[-1]:,.2f} "
        f"({total.iloc[-1] - total.iloc[0]:+,.2f} over the period)"
    )
