"""Per-commit metric scatter charts as a self-contained HTML (inline SVG).

No plotting dependency (matplotlib/numpy) — Surveyor stays "pure Python, only
lizard required". One chart per per-commit metric: X = commit in history order,
Y = the metric value, dot colour = bug ground-truth (red = bug-fix commit, blue
= ordinary change). Heavy-tailed metrics (the impact family) auto-switch to a log
Y so the bulk isn't crushed under a few spikes.
"""
from __future__ import annotations

import math

W = 1100
CH = 240          # per-chart height
L, R, T, B = 72, 20, 34, 30   # margins
BLUE = "#3b82f6"
RED = "#ef4444"
MAXPTS = 20000    # cap circles per chart (keep all reds, sample blues) for big repos


def _human(v: float) -> str:
    a = abs(v)
    for div, suf in ((1e9, "G"), (1e6, "M"), (1e3, "k")):
        if a >= div:
            return f"{v / div:.1f}{suf}"
    return f"{v:.0f}"


def _esc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def _pick_scale(values: list[float], scale: str) -> str:
    if scale in ("linear", "log"):
        return scale
    nz = sorted(v for v in values if v > 0)
    if not nz:
        return "linear"
    med = nz[len(nz) // 2]
    return "log" if (max(nz) / max(med, 1)) > 50 else "linear"


def _svg_chart(key: str, label: str, points: list[tuple[int, float, bool]],
               n_total: int, scale: str) -> str:
    vals = [p[1] for p in points]
    ymax = max(vals) if vals else 0
    mode = _pick_scale(vals, scale)
    inner_w = W - L - R
    inner_h = CH - T - B
    xspan = max(n_total - 1, 1)

    def xpix(i: int) -> float:
        return L + inner_w * (i / xspan)

    if mode == "log":
        tmax = math.log10(ymax + 1) or 1.0

        def ypix(v: float) -> float:
            return T + inner_h * (1 - math.log10(v + 1) / tmax)
        ticks = [10 ** k for k in range(0, int(tmax) + 1) if 10 ** k <= max(ymax, 1)]
    else:
        ym = ymax or 1.0

        def ypix(v: float) -> float:
            return T + inner_h * (1 - v / ym)
        ticks = [ymax * f for f in (0, 0.25, 0.5, 0.75, 1.0)]

    parts = [f'<svg viewBox="0 0 {W} {CH}" class="chart" xmlns="http://www.w3.org/2000/svg">']
    parts.append(f'<text x="{L}" y="20" class="title">{_esc(label)} '
                 f'<tspan class="dim">({mode} Y)</tspan></text>')
    # y gridlines + labels
    for tv in ticks:
        y = ypix(tv)
        parts.append(f'<line x1="{L}" y1="{y:.1f}" x2="{W - R}" y2="{y:.1f}" class="grid"/>')
        parts.append(f'<text x="{L - 6}" y="{y + 3:.1f}" class="ytick">{_human(tv)}</text>')
    # axes
    parts.append(f'<line x1="{L}" y1="{T}" x2="{L}" y2="{CH - B}" class="axis"/>')
    parts.append(f'<line x1="{L}" y1="{CH - B}" x2="{W - R}" y2="{CH - B}" class="axis"/>')
    parts.append(f'<text x="{L}" y="{CH - 8}" class="xtick">oldest</text>')
    parts.append(f'<text x="{W - R}" y="{CH - 8}" class="xtick" text-anchor="end">newest →</text>')

    show_titles = len(points) <= 5000
    for colour, group, big in ((BLUE, [p for p in points if not p[2]], False),
                               (RED, [p for p in points if p[2]], True)):
        r = 2.2 if big else 1.7
        op = 0.85 if big else 0.5
        parts.append(f'<g fill="{colour}" fill-opacity="{op}">')
        for i, v, _bug in group:
            c = f'<circle cx="{xpix(i):.1f}" cy="{ypix(v):.1f}" r="{r}"/>'
            parts.append(c)
        parts.append("</g>")
    parts.append("</svg>")
    return "\n".join(parts)


def _downsample(points: list[tuple[int, float, bool]]) -> tuple[list, int]:
    """Keep every red (bug) point; evenly thin blues to fit MAXPTS. Returns
    (points, dropped_blue_count). X still uses the true commit index."""
    reds = [p for p in points if p[2]]
    blues = [p for p in points if not p[2]]
    budget = MAXPTS - len(reds)
    if budget <= 0 or len(blues) <= budget:
        return points, 0
    stride = math.ceil(len(blues) / budget)
    kept = blues[::stride]
    return reds + kept, len(blues) - len(kept)


def write_commit_charts(rows: list[dict], metrics: list[tuple[str, str]],
                        out_path: str, title: str) -> None:
    """rows: chronological, each {idx, sha, subject, bug, <metric keys...>}."""
    n = len(rows)
    n_bug = sum(1 for r in rows if r["bug"])
    charts = []
    for key, label in metrics:
        pts = [(r["idx"], float(r[key] or 0), r["bug"]) for r in rows]
        pts, dropped = _downsample(pts)
        svg = _svg_chart(key, label, pts, n, scale="auto")
        note = (f' <span class="dim">({dropped:,} low-value points thinned)</span>'
                if dropped else "")
        charts.append(f'<section><div class="hd">{_esc(label)}{note}</div>{svg}</section>')

    html = f"""<!doctype html>
<meta charset="utf-8">
<title>{_esc(title)} — per-commit metrics</title>
<style>
  body {{ font: 14px/1.4 system-ui, sans-serif; margin: 24px; color: #1f2937; background: #fff; }}
  h1 {{ font-size: 18px; margin: 0 0 2px; }}
  .sub {{ color: #6b7280; margin-bottom: 18px; }}
  .legend span {{ margin-right: 16px; }}
  .dot {{ display: inline-block; width: 10px; height: 10px; border-radius: 50%; vertical-align: middle; margin-right: 5px; }}
  section {{ margin: 10px 0 22px; }}
  .hd {{ font-weight: 600; margin: 0 0 2px; }}
  svg.chart {{ width: 100%; height: auto; border: 1px solid #eee; background: #fcfcfd; }}
  .title {{ font: 600 13px sans-serif; fill: #374151; }}
  .dim {{ fill: #9ca3af; color: #9ca3af; font-weight: 400; }}
  .axis {{ stroke: #9ca3af; stroke-width: 1; }}
  .grid {{ stroke: #eee; stroke-width: 1; }}
  .ytick {{ font: 10px sans-serif; fill: #6b7280; text-anchor: end; }}
  .xtick {{ font: 10px sans-serif; fill: #6b7280; }}
</style>
<h1>{_esc(title)} — per-commit metrics</h1>
<div class="sub">{n:,} commits &middot; X = commit in history order (oldest → newest) &middot; Y = metric value</div>
<div class="legend">
  <span><span class="dot" style="background:{RED}"></span>bug-fix commit ({n_bug:,})</span>
  <span><span class="dot" style="background:{BLUE}"></span>ordinary change ({n - n_bug:,})</span>
</div>
{''.join(charts)}
"""
    with open(out_path, "w") as fh:
        fh.write(html)
