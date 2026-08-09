"""Tiny inline-SVG sparklines.

Rendering one Plotly figure per position is far too heavy for a table that
redraws every 20 seconds on a Pi. An SVG polyline is a few hundred bytes, draws
instantly, and carries all the intraday shape a glanceable display needs.

The SVG is handed to the browser as a base64 data-URI on an `html.Img`, which
keeps this dependency-free (no dash-svg / dangerously-set-inner-html) while
still being pure server-side generated markup.

The line is colored by where it ends relative to a baseline (the previous
close), and the baseline is drawn as a faint dotted rule so "above/below where
it opened" reads at a distance.
"""

import base64

from dash import html


def _svg_markup(
    values: list[float],
    baseline: float | None,
    up: str,
    down: str,
    width: int,
    height: int,
    stroke: float,
    muted: bool,
) -> str:
    base = baseline if baseline is not None else values[0]
    lo = min(min(values), base)
    hi = max(max(values), base)
    span = hi - lo or 1.0
    pad = 3.0
    usable_h = height - 2 * pad
    # The endpoint dot sits at the last x — without horizontal room to match,
    # its right half falls outside the viewBox and gets clipped.
    r = stroke + 0.5
    pad_x = r + 0.5
    usable_w = width - 2 * pad_x

    def y(v: float) -> float:
        # SVG y grows downward; invert so higher price sits higher.
        return pad + (hi - v) / span * usable_h

    n = len(values)
    step = usable_w / (n - 1)

    def x(i: int) -> float:
        return pad_x + i * step

    coords = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(values))
    color = up if values[-1] >= base else down
    baseline_y = y(base)
    # A borrowed shape is drawn faint: the endpoint is a real price, but the
    # path between is the proxy's, and it shouldn't read as hard as a real tape.
    line_op = 0.5 if muted else 1.0
    return (
        f'<svg viewBox="0 0 {width} {height}" width="{width}" height="{height}" '
        f'preserveAspectRatio="none" xmlns="http://www.w3.org/2000/svg">'
        f'<line x1="0" y1="{baseline_y:.1f}" x2="{width}" y2="{baseline_y:.1f}" '
        f'stroke="#3a4149" stroke-width="1" stroke-dasharray="2 3"/>'
        f'<polyline points="{coords}" fill="none" stroke="{color}" '
        f'stroke-opacity="{line_op}" '
        f'stroke-width="{stroke}" stroke-linejoin="round" stroke-linecap="round"/>'
        f'<circle cx="{x(n - 1):.1f}" cy="{y(values[-1]):.1f}" '
        f'r="{r:.1f}" fill="{color}"/>'
        f"</svg>"
    )


def sparkline(
    values: list[float],
    *,
    baseline: float | None = None,
    up: str = "#1fa588",
    down: str = "#e05252",
    width: int = 120,
    height: int = 34,
    stroke: float = 2.0,
    muted: bool = False,
) -> html.Div:
    """A dash component rendering an SVG sparkline of `values`.

    `baseline` (previous close) sets both the line color and a dotted rule; if
    omitted, the first point is used. `muted` draws the line faint, for a curve
    whose shape is indicative rather than measured. Returns an empty box for <2
    points so the caller doesn't have to special-case fresh/illiquid tickers.
    """
    pts = [v for v in values if v is not None]
    box = {"width": f"{width}px", "height": f"{height}px"}
    if len(pts) < 2:
        return html.Div(style=box, className="spark-empty")

    svg = _svg_markup(pts, baseline, up, down, width, height, stroke, muted)
    b64 = base64.b64encode(svg.encode("utf-8")).decode("ascii")
    return html.Img(
        src=f"data:image/svg+xml;base64,{b64}",
        style={**box, "display": "block"},
        className="spark",
    )
