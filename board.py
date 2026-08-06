#!/usr/bin/env python3
"""Generates board/index.html — the PUBLIC mobile scoreboard.

Published to the public bot-arena-board repo (GitHub Pages) by the nightly
scoreboard workflow. Deliberately minimal: equity race, return tiles, summary
stats. NO scanner data, NO trade reasoning, NO open positions — nothing that
would leak either bot's signals (RULES.md §2.3). Safe for the open internet.
"""
from __future__ import annotations

import html
import json
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from core.common import ROOT, now_et  # noqa: E402
from dashboard import line_chart, equity_series  # noqa: E402  (reuse chart + curves)

OUT = ROOT / "board" / "index.html"


def build() -> str:
    comp = yaml.safe_load((ROOT / "config" / "competition.yaml").read_text())
    sb_file = ROOT / "reports" / "scoreboard.json"
    sb = json.loads(sb_file.read_text()) if sb_file.exists() else {}
    rows = sb.get("competitors", [])
    curves = {c["slug"]: equity_series(c["slug"]) for c in comp["competitors"]}
    colors = ["var(--series-1)", "var(--series-2)"]
    chart = line_chart([
        {"label": c["label"], "short": c["label"].split()[0],
         "color": colors[i % 2], "points": curves[c["slug"]]}
        for i, c in enumerate(comp["competitors"]) if curves[c["slug"]]])

    # tiles
    tiles = []
    by_slug = {r["slug"]: r for r in rows}
    for i, c in enumerate(comp["competitors"]):
        r = by_slug.get(c["slug"], {})
        eq = r.get("equity")
        ret = r.get("total_return_pct")
        delta = ""
        if ret is not None:
            up = ret >= 0
            delta = (f'<div class="delta {"up" if up else "down"}">'
                     f'{"▲" if up else "▼"} {ret:+.2f}%</div>')
        tiles.append(f'''<div class="tile" style="border-top:4px solid {colors[i%2]}">
  <div class="tlabel">{html.escape(c["label"])}</div>
  <div class="tvalue">{f"${eq:,.0f}" if eq is not None else "—"}</div>{delta}</div>''')

    # stats table (position-free)
    stat_rows = ""
    if rows:
        keys = [("total_return_pct", "Total return %"), ("day_pl_pct", "Last day %"),
                ("max_drawdown_pct", "Max drawdown %"), ("sharpe_daily_ann", "Sharpe (ann.)"),
                ("trading_days", "Days scored"), ("kill_switch", "Stopped out")]
        head = "<tr><th></th>" + "".join(
            f"<th>{html.escape(r['label'].split()[0])}</th>" for r in rows) + "</tr>"
        def fmt(v):
            if v is None:
                return "—"
            if v is True:
                return "YES"
            if v is False:
                return "no"
            return v
        body = "".join(
            f"<tr><td class='k'>{label}</td>" + "".join(
                f"<td class='num'>{fmt(r.get(k))}</td>" for r in rows) + "</tr>"
            for k, label in keys)
        stat_rows = f"<table class='tbl'><thead>{head}</thead><tbody>{body}</tbody></table>"
    else:
        stat_rows = ('<div class="empty">First scores post after the first '
                     'trading day\'s close (5:15pm ET).</div>')

    leader = sb.get("leader")
    leader_html = (f'<div class="leader">Leader: <b>{html.escape(leader)}</b></div>'
                   if leader else "")
    start = comp.get("start_date") or "TBD"

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="900">
<title>Bot Arena — scoreboard</title>
<style>
.viz-root {{
  color-scheme: light;
  --surface-1:#fcfcfb; --page:#f9f9f7; --ink-1:#0b0b0b; --ink-2:#52514e;
  --muted:#898781; --grid:#e1e0d9; --axis:#c3c2b7;
  --series-1:#2a78d6; --series-2:#eb6834;
  --good-text:#006300; --crit:#d03b3b; --ring:rgba(11,11,11,0.10);
}}
@media (prefers-color-scheme: dark) {{
  :root:where(:not([data-theme="light"])) .viz-root {{
    color-scheme: dark;
    --surface-1:#1a1a19; --page:#0d0d0d; --ink-1:#ffffff; --ink-2:#c3c2b7;
    --muted:#898781; --grid:#2c2c2a; --axis:#383835;
    --series-1:#3987e5; --series-2:#d95926;
    --good-text:#0ca30c; --crit:#d03b3b; --ring:rgba(255,255,255,0.10);
  }}
}}
body{{margin:0;background:var(--page);color:var(--ink-1);
  font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}}
.viz-root{{max-width:640px;margin:0 auto;padding:18px 14px 40px}}
h1{{font-size:19px;margin:0 0 2px}}
.sub{{color:var(--ink-2);font-size:13px;margin-bottom:12px}}
.nav{{font-size:13px;margin-bottom:8px}} .nav a{{color:var(--ink-2)}}
.leader{{font-size:14px;margin:6px 0 10px}}
.tiles{{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin:12px 0}}
.tile{{background:var(--surface-1);border:1px solid var(--ring);border-radius:12px;
  padding:12px 14px}}
.tlabel{{color:var(--ink-2);font-size:12px}}
.tvalue{{font-size:27px;font-weight:650}}
.delta{{font-size:14px}} .delta.up{{color:var(--good-text)}} .delta.down{{color:var(--crit)}}
.panel{{background:var(--surface-1);border:1px solid var(--ring);border-radius:12px;
  padding:12px 14px;margin:10px 0;overflow-x:auto}}
.tbl{{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}}
.tbl th{{text-align:right;color:var(--muted);font-weight:500;font-size:12px;
  padding:4px 0 6px 10px;border-bottom:1px solid var(--axis)}}
.tbl th:first-child{{text-align:left;padding-left:0}}
.tbl td{{padding:7px 0 7px 10px;border-bottom:1px solid var(--grid);text-align:right}}
.tbl td.k{{text-align:left;color:var(--ink-2);padding-left:0}}
.num{{font-variant-numeric:tabular-nums}}
.empty{{color:var(--muted);padding:14px 2px;font-size:13px}}
.grid{{stroke:var(--grid);stroke-width:1}} .axis{{stroke:var(--axis);stroke-width:1}}
.tick{{fill:var(--muted);font-size:11px}} .endlbl{{font-size:12px;font-weight:600}}
.linechart{{width:100%;height:auto;display:block}}
.chartwrap{{position:relative}}
.xhair{{stroke:var(--axis);stroke-width:1;stroke-dasharray:3 3}}
.tooltip{{position:absolute;pointer-events:none;background:var(--surface-1);
  border:1px solid var(--ring);border-radius:8px;padding:6px 10px;font-size:12px;
  box-shadow:0 2px 8px rgba(0,0,0,.12);white-space:nowrap}}
footer{{color:var(--muted);font-size:12px;margin-top:22px}}
</style></head>
<body><div class="viz-root">
<div class="nav"><a href="steward.html">STEWARD portfolio →</a></div>
<h1>{html.escape(comp["title"])}</h1>
<div class="sub">Started {html.escape(str(start))} · updated
  {now_et():%b %d, %I:%M %p} ET · refreshes nightly after the close</div>
{leader_html}
<div class="tiles">{"".join(tiles)}</div>
<div class="panel">{chart}</div>
<div class="panel">{stat_rows}</div>
<footer>Bot Arena · equal $50k paper accounts · scored on total return ·
positions and signals are not published during the round (rules §2.3)</footer>
</div>
<script>
document.querySelectorAll('.chartwrap').forEach(w => {{
  const data = JSON.parse(w.dataset.series || '[]');
  if (!data.length) return;
  const svg = w.querySelector('svg'), tip = w.querySelector('.tooltip'),
        xh = w.querySelector('.xhair'), vb = svg.viewBox.baseVal;
  const move = e => {{
    const r = svg.getBoundingClientRect();
    const cx = (e.touches ? e.touches[0].clientX : e.clientX);
    const mx = (cx - r.left) * vb.width / r.width;
    let best = null;
    data.forEach(s => s.points.forEach(p => {{
      const d = Math.abs(p[2] - mx);
      if (!best || d < best.d) best = {{d, date: p[0]}};
    }}));
    if (!best) return;
    const rows = data.map(s => {{
      const p = s.points.find(q => q[0] === best.date);
      return p ? `<div><span style="color:${{s.color}}">●</span> ${{s.label}}: ` +
             `$${{p[1].toLocaleString(undefined,{{maximumFractionDigits:0}})}}</div>` : '';
    }}).join('');
    const px = data.flatMap(s => s.points).find(q => q[0] === best.date);
    xh.setAttribute('x1', px[2]); xh.setAttribute('x2', px[2]);
    xh.style.display = '';
    tip.innerHTML = `<b>${{best.date}}</b>${{rows}}`;
    tip.style.display = 'block';
    const lx = px[2] * r.width / vb.width;
    tip.style.left = Math.min(lx + 12, r.width - tip.offsetWidth - 4) + 'px';
    tip.style.top = '16px';
  }};
  svg.addEventListener('mousemove', move);
  svg.addEventListener('touchstart', move, {{passive: true}});
  svg.addEventListener('touchmove', move, {{passive: true}});
  svg.addEventListener('mouseleave', () => {{
    tip.style.display = 'none'; xh.style.display = 'none';
  }});
}});
</script></body></html>"""


def main() -> int:
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(build())
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
