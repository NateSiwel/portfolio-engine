"""Plotly figures for the non-table views, all on a shared dark kiosk theme.

Each builder takes an already-built ViewModel (and, where needed, the Portfolio
for historical series) and returns a go.Figure styled for an unattended panel:
dark background, no chrome to speak of, large fonts, and clamped interactivity.
"""

from __future__ import annotations

from datetime import date, timedelta

import plotly.graph_objects as go

from . import earnings
from .config import Config
from .portfolio import Portfolio, ViewModel

SANS = '"IBM Plex Sans", Inter, system-ui, sans-serif'
MONO = '"IBM Plex Mono", Consolas, monospace'

# Time windows selectable on the historical views. "ALL" = full history.
WINDOWS = ("1M", "3M", "6M", "YTD", "1Y", "ALL")
_WINDOW_DAYS = {"1M": 30, "3M": 91, "6M": 182, "1Y": 365}


def window_start(window: str, today: date | None = None) -> date | None:
    """First date included by `window`, or None for the full history."""
    today = today or date.today()
    if window == "YTD":
        return date(today.year, 1, 1)
    days = _WINDOW_DAYS.get(window)
    return today - timedelta(days=days) if days else None


def _base_layout(cfg: Config, height: int | None = None) -> dict:
    lay = {
        "paper_bgcolor": cfg.bg,
        "plot_bgcolor": cfg.bg,
        "font": {"color": cfg.text, "family": SANS, "size": 19},
        "margin": {"l": 64, "r": 30, "t": 18, "b": 40},
        "showlegend": False,
        "hovermode": False,  # a wall display isn't hovered; keep it static & cheap
    }
    if height:
        lay["height"] = height
    return lay


def _axis(cfg: Config) -> dict:
    return {
        "gridcolor": cfg.border,
        "zerolinecolor": cfg.border,
        "linecolor": cfg.border,
        "color": cfg.text_dim,
        "tickfont": {"family": MONO, "size": 17},
    }


def _hex_rgb(hx: str) -> tuple[int, int, int]:
    hx = hx.lstrip("#")
    return int(hx[0:2], 16), int(hx[2:4], 16), int(hx[4:6], 16)


def _blend(a: str, b: str, t: float) -> str:
    """Linear blend from hex color `a` to `b`, t in [0, 1]."""
    ra, ga, ba = _hex_rgb(a)
    rb, gb, bb = _hex_rgb(b)
    return (
        f"rgb({round(ra + (rb - ra) * t)},"
        f"{round(ga + (gb - ga) * t)},{round(ba + (bb - ba) * t)})"
    )


_NEUTRAL = "#3a4149"  # flat/unknown tiles: a quiet gray, no polarity signal


def _diverging(pct: float | None, cfg: Config) -> str:
    """Neutral→up/down color for a day-change percentage, saturating past ±3%."""
    if pct is None:
        return _NEUTRAL
    p = max(-3.0, min(3.0, pct)) / 3.0
    if p >= 0:
        return _blend(_NEUTRAL, cfg.up, p)
    return _blend(_NEUTRAL, cfg.down, -p)


def treemap(vm: ViewModel, cfg: Config) -> go.Figure:
    labels, values, colors, text = [], [], [], []
    for p in vm.positions:
        labels.append(p.ticker)
        values.append(p.value)
        colors.append(_diverging(p.day_pct, cfg))
        pct = f"{p.day_pct:+.1f}%" if p.day_pct is not None else "—"
        text.append(f"{p.ticker}<br>{pct}")
    if vm.cash > 0:
        labels.append("CASH")
        values.append(vm.cash)
        colors.append(cfg.panel_alt)
        text.append("CASH")

    fig = go.Figure(
        go.Treemap(
            labels=labels,
            values=values,
            parents=[""] * len(labels),
            text=text,
            textinfo="text",
            marker={"colors": colors, "line": {"color": cfg.bg, "width": 2}},
            textfont={"size": 28, "color": cfg.text, "family": SANS},
            tiling={"pad": 2},
            sort=True,
            hoverinfo="skip",
        )
    )
    lay = _base_layout(cfg)
    lay["margin"] = {"l": 4, "r": 4, "t": 4, "b": 4}
    fig.update_layout(**lay)
    return fig


def history(
    pf: Portfolio, vm: ViewModel, cfg: Config, blur: bool = False, window: str = "1M"
) -> go.Figure:
    dates = list(pf.history_dates)
    values = list(pf.history_values)
    # Append the live total so the curve reaches "now".
    if dates:
        if dates[-1] != date.today():
            dates.append(date.today())
            values.append(vm.total_value)
        else:
            values[-1] = vm.total_value

    start = window_start(window)
    if start is not None:
        pts = [(d, v) for d, v in zip(dates, values) if d >= start]
        if len(pts) >= 2:  # keep the full curve rather than a degenerate one
            dates, values = ([d for d, _ in pts], [v for _, v in pts])

    up = values[-1] >= values[0] if len(values) >= 2 else True
    color = cfg.up if up else cfg.down
    r, g, b = _hex_rgb(color)
    fig = go.Figure(
        go.Scatter(
            x=dates,
            y=values,
            mode="lines",
            line={"color": color, "width": 2},
            fill="tozeroy",
            fillcolor=f"rgba({r},{g},{b},0.06)",
            hoverinfo="skip",
        )
    )
    lay = _base_layout(cfg)
    fig.update_layout(**lay)
    fig.update_xaxes(**_axis(cfg))
    # When blurred, keep the shape but hide the dollar scale (it reveals size).
    fig.update_yaxes(
        **_axis(cfg), tickprefix="$", tickformat=",.0f", showticklabels=not blur
    )
    # Full history is anchored at $0 for honest scale. Short windows get an
    # explicit range hugging the data — autorange won't do, because the
    # tozeroy fill drags it back down to $0 and flattens the window.
    if start is None:
        fig.update_yaxes(rangemode="tozero")
    elif values:
        lo, hi = min(values), max(values)
        pad = (hi - lo) * 0.08 or hi * 0.01 or 1.0
        fig.update_yaxes(range=[lo - pad, hi + pad])
    return fig


def movers(vm: ViewModel, cfg: Config, top: int = 12) -> go.Figure:
    ranked = [p for p in vm.positions if p.day_pct is not None]
    ranked.sort(key=lambda p: p.day_pct)
    ranked = (
        ranked[:top]
        if len(ranked) <= top
        else (ranked[: top // 2] + ranked[-top // 2 :])
    )
    y = [p.ticker for p in ranked]
    x = [p.day_pct for p in ranked]
    colors = [cfg.up if v >= 0 else cfg.down for v in x]
    labels = [f"{v:+.1f}%" for v in x]
    fig = go.Figure(
        go.Bar(
            x=x,
            y=y,
            orientation="h",
            marker_color=colors,
            text=labels,
            textposition="outside",
            textfont={"size": 20, "color": cfg.text, "family": MONO},
            cliponaxis=False,
            hoverinfo="skip",
        )
    )
    fig.update_layout(**_base_layout(cfg), bargap=0.55)
    fig.update_xaxes(**_axis(cfg), ticksuffix="%", zeroline=True, zerolinewidth=2)
    fig.update_yaxes(**_axis(cfg))
    return fig


def vs_market(pf: Portfolio, cfg: Config, window: str = "1M") -> go.Figure:
    start = window_start(window)

    def rebased(dates_, curve):
        """Growth-factor curve → % return, re-based at the window start."""
        pairs = list(zip(dates_, curve))
        if start is not None:
            trimmed = [(d, g) for d, g in pairs if d >= start]
            if len(trimmed) >= 2:
                pairs = trimmed
        base = pairs[0][1] or 1.0
        return [d for d, _ in pairs], [(g / base - 1) * 100 for _, g in pairs]

    fig = go.Figure()
    if pf.twr:
        first = next(iter(pf.twr.values()))
        px, py = rebased(first[0], first[1])
        fig.add_trace(
            go.Scatter(
                x=px,
                y=py,
                mode="lines",
                name="Portfolio",
                line={"color": cfg.accent, "width": 2.5},
                hoverinfo="skip",
            )
        )
        dashes = iter(["dash", "dot", "dashdot"])
        for i, (bench, (bdates, _, bcurve)) in enumerate(pf.twr.items()):
            bx, by = rebased(bdates, bcurve)
            fig.add_trace(
                go.Scatter(
                    x=bx,
                    y=by,
                    mode="lines",
                    name=bench,
                    line={
                        "color": cfg.palette[(i + 1) % len(cfg.palette)],
                        "width": 1.5,
                        "dash": next(dashes, "dot"),
                    },
                    hoverinfo="skip",
                )
            )
    lay = _base_layout(cfg)
    lay["showlegend"] = True
    lay["legend"] = {"orientation": "h", "y": 1.08, "x": 0, "font": {"size": 18}}
    fig.update_layout(**lay)
    fig.update_xaxes(**_axis(cfg))
    fig.update_yaxes(**_axis(cfg), ticksuffix="%", zeroline=True, zerolinewidth=2)
    return fig
