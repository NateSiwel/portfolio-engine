"""
Interactive portfolio visualization.

Data format per snapshot:
    (date, {'CASH': Decimal, 'TICKER': (shares, price, total_value), ...})

Produces a single self-contained HTML file with four linked views
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


def display_data(RAW, output_path="portfolio_dashboard.html", market_comparisons=None):
    """Render the dashboard.

    market_comparisons: optional {ticker: (dates, portfolio_curve, benchmark_curve)}
    from compare_to_market; adds a "vs Market" view plotting the portfolio's
    time-weighted return against each benchmark.
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
    fig.add_hline(y=0, line_dash="dot", line_color="gray", opacity=0.5)

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
    }

    view_labels = [
        (0, "Total value"),
        (1, "Stacked allocation"),
        (2, "Price performance"),
        (3, "Current allocation"),
        (4, "vs Market"),
    ]
    if not price_tickers:  # cash-only portfolio
        view_labels = [v for v in view_labels if v[0] != 2]
    if not market_comparisons:
        view_labels = [v for v in view_labels if v[0] != 4]

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
