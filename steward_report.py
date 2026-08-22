#!/usr/bin/env python3
"""STEWARD report — reports/steward.html (+ steward.md summary).

The portfolio's glass box: equity vs SPY, current vs target allocation, the
momentum leaderboard, and the manager's written reasoning for the latest cycle.
Regenerated on every cycle and nightly pulse.
"""
from __future__ import annotations

import html
import json
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from core.common import ROOT, now_et  # noqa: E402
from dashboard import line_chart, equity_series, chip  # noqa: E402

OUT = ROOT / "reports" / "steward.html"
OUT_MD = ROOT / "reports" / "steward.md"


def curve_from(name: str) -> list[tuple[str, float]]:
    f = ROOT / "state" / "steward" / f"{name}.json"
    if not f.exists():
        return []
    by_day: dict[str, float] = {}
    for pt in json.loads(f.read_text(encoding="utf-8")):
        by_day[pt["ts_et"][:10]] = pt["equity"]
    return sorted(by_day.items())


def latest_cycle() -> dict:
    runs = sorted(ROOT.glob("journal/steward_2*"), reverse=True)
    for rdir in runs:
        if not rdir.is_dir():
            continue
        out = {"run_id": rdir.name}
        for stage in ("scan", "analysis", "plan", "execution", "skipped"):
            f = rdir / f"{stage}.json"
            if f.exists():
                try:
                    out[stage] = json.loads(f.read_text(encoding="utf-8"))
                except Exception:
                    pass
        if len(out) > 1:
            return out
    return {}


def main() -> int:
    cfg = yaml.safe_load((ROOT / "config" / "steward.yaml").read_text(encoding="utf-8"))
    run = latest_cycle()
    analysis = run.get("analysis", {})
    p = run.get("plan", {})
    execution = run.get("execution", {})

    port = curve_from("equity_curve")
    bench = curve_from("benchmark_curve")
    eq = port[-1][1] if port else None
    bv = bench[-1][1] if bench else None
    start = cfg["starting_equity"]

    chart = line_chart([
        s for s in [
            {"label": "STEWARD (Claude)", "short": "STEWARD",
             "color": "var(--series-1)", "points": port},
            {"label": "SPY buy & hold", "short": "SPY",
             "color": "var(--series-2)", "points": bench},
        ] if s["points"]])

    def tile(label, val, ret, color):
        d = ""
        if ret is not None:
            up = ret >= 0
            d = (f'<div class="delta {"up" if up else "down"}">'
                 f'{"▲" if up else "▼"} {ret:+.2f}%</div>')
        return (f'<div class="tile" style="border-top:4px solid {color}">'
                f'<div class="tlabel">{label}</div>'
                f'<div class="tvalue">{val}</div>{d}</div>')

    tiles = (tile("STEWARD (Claude)", f"${eq:,.0f}" if eq else "—",
                  (eq / start - 1) * 100 if eq else None, "var(--series-1)")
             + tile("SPY buy & hold", f"${bv:,.0f}" if bv else "—",
                    (bv / start - 1) * 100 if bv else None, "var(--series-2)"))

    # allocation table
    alloc = ""
    if p.get("targets_final"):
        cur = p.get("current_weights", {})
        rows = ""
        for sym, w in p["targets_final"].items():
            rows += (f"<tr><td class='sym'>{sym}</td>"
                     f"<td class='num'>{cur.get(sym, 0)*100:.1f}%</td>"
                     f"<td class='num'>{w*100:.1f}%</td></tr>")
        rows += (f"<tr><td class='sym'>CASH</td><td class='num'>—</td>"
                 f"<td class='num'>{p.get('cash_target', 0)*100:.1f}%</td></tr>")
        alloc = ("<table class='tbl'><thead><tr><th>Holding</th><th>Now</th>"
                 "<th>Target</th></tr></thead><tbody>" + rows + "</tbody></table>")
    else:
        alloc = '<div class="empty">Allocation appears after the first weekly cycle.</div>'

    # momentum leaderboard
    lb = ""
    for r in analysis.get("momentum_leaderboard", [])[:10]:
        lb += (f"<tr><td class='sym'>{r['symbol']}</td>"
               f"<td class='num'>{r['mom_12_1']*100:+.1f}%</td>"
               f"<td>{chip(r['above_200sma'], 'uptrend' if r['above_200sma'] else 'downtrend')}"
               f"</td></tr>")
    lb_html = (("<table class='tbl'><thead><tr><th>Stock</th><th>12-1 momentum</th>"
                "<th>Trend</th></tr></thead><tbody>" + lb + "</tbody></table>")
               if lb else '<div class="empty">Appears after the first cycle.</div>')

    # manager's notes + orders
    notes = "".join(f"<p class='why'>• {html.escape(n)}</p>"
                    for n in analysis.get("notes", []) + p.get("notes", []))
    halts = "".join(f'<span class="chip crit">⚠ {html.escape(h)}</span>'
                    for h in p.get("halts", []))
    orders = ""
    for o in p.get("orders", []):
        ok = next((r for r in execution.get("results", [])
                   if r.get("symbol") == o["symbol"]), {})
        status = chip(bool(ok.get("ok")), "filled" if ok.get("ok") else "pending/failed") \
            if execution and not execution.get("dry_run") else \
            '<span class="chip mgmtc">dry run</span>'
        orders += (f"<tr><td class='sym'>{o['symbol']}</td><td>{o['side'].upper()}</td>"
                   f"<td class='num'>{o['qty']}</td>"
                   f"<td>{html.escape(o['reason'])}</td><td>{status}</td></tr>")
    orders_html = (("<table class='tbl'><thead><tr><th>Sym</th><th>Side</th><th>Qty</th>"
                    "<th>Why</th><th>Status</th></tr></thead><tbody>" + orders +
                    "</tbody></table>")
                   if orders else
                   '<div class="empty">No rebalance trades in the latest cycle — '
                   'portfolio within its drift bands.</div>')

    regime = analysis.get("regime_on")
    regime_html = ("" if regime is None else
                   f'<div class="leader">Regime: <b>{"RISK-ON" if regime else "RISK-OFF"}</b></div>')

    page = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>STEWARD — Claude's portfolio</title>
<style>
.viz-root {{
  color-scheme: light;
  --surface-1:#fcfcfb; --page:#f9f9f7; --ink-1:#0b0b0b; --ink-2:#52514e;
  --muted:#898781; --grid:#e1e0d9; --axis:#c3c2b7;
  --series-1:#2a78d6; --series-2:#eb6834;
  --good:#0ca30c; --good-text:#006300; --crit:#d03b3b; --ring:rgba(11,11,11,0.10);
}}
@media (prefers-color-scheme: dark) {{
  :root:where(:not([data-theme="light"])) .viz-root {{
    color-scheme: dark;
    --surface-1:#1a1a19; --page:#0d0d0d; --ink-1:#ffffff; --ink-2:#c3c2b7;
    --muted:#898781; --grid:#2c2c2a; --axis:#383835;
    --series-1:#3987e5; --series-2:#d95926;
    --good:#0ca30c; --good-text:#0ca30c; --crit:#d03b3b; --ring:rgba(255,255,255,0.10);
  }}
}}
body{{margin:0;background:var(--page);color:var(--ink-1);
  font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}}
.viz-root{{max-width:760px;margin:0 auto;padding:18px 14px 40px}}
h1{{font-size:19px;margin:0 0 2px}} h2{{font-size:15px;margin:22px 0 8px}}
.sub{{color:var(--ink-2);font-size:13px;margin-bottom:12px}}
.leader{{font-size:14px;margin:6px 0 10px}}
.tiles{{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin:12px 0}}
.tile{{background:var(--surface-1);border:1px solid var(--ring);border-radius:12px;
  padding:12px 14px}}
.tlabel{{color:var(--ink-2);font-size:12px}} .tvalue{{font-size:26px;font-weight:650}}
.delta{{font-size:14px}} .delta.up{{color:var(--good-text)}} .delta.down{{color:var(--crit)}}
.panel{{background:var(--surface-1);border:1px solid var(--ring);border-radius:12px;
  padding:12px 14px;margin:8px 0;overflow-x:auto}}
.tbl{{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}}
.tbl th{{text-align:left;color:var(--muted);font-weight:500;font-size:12px;
  padding:4px 10px 6px 0;border-bottom:1px solid var(--axis)}}
.tbl td{{padding:6px 10px 6px 0;border-bottom:1px solid var(--grid)}}
.sym{{font-weight:600}} .num{{font-variant-numeric:tabular-nums}}
.chip{{display:inline-block;font-size:12px;padding:1px 8px;border-radius:99px;
  border:1px solid var(--ring);margin:1px 2px}}
.chip.good{{color:var(--good-text)}} .chip.crit{{color:var(--crit)}}
.chip.mgmtc{{color:var(--ink-2)}}
.why{{color:var(--ink-2);font-size:13px;margin:5px 0}}
.empty{{color:var(--muted);padding:12px 2px;font-size:13px}}
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
<h1>STEWARD — Claude's portfolio</h1>
<div class="sub">Updated {now_et():%b %d, %I:%M %p} ET · latest cycle
  <b>{html.escape(run.get("run_id", "—"))}</b> {halts}</div>
{regime_html}
<div class="tiles">{tiles}</div>
<h2>Portfolio vs. the benchmark</h2>
<div class="panel">{chart}</div>
<h2>Allocation — current vs. target</h2>
<div class="panel">{alloc}</div>
<h2>Latest rebalance orders</h2>
<div class="panel">{orders_html}</div>
<h2>Momentum leaderboard (12-1)</h2>
<div class="panel">{lb_html}</div>
<h2>Manager's notes</h2>
<div class="panel">{notes or '<div class="empty">Appear after the first cycle.</div>'}</div>
<footer>scan → analyze → plan → execute · weekly decisions, nightly pulse ·
kill switch at −{yaml.safe_load((ROOT / "config" / "steward.yaml").read_text(encoding="utf-8"))["risk"]["kill_switch_drawdown_pct"]:.0f}%</footer>
</div>
<script>
document.querySelectorAll('.chartwrap').forEach(w => {{
  const data = JSON.parse(w.dataset.series || '[]');
  if (!data.length) return;
  const svg = w.querySelector('svg'), tip = w.querySelector('.tooltip'),
        xh = w.querySelector('.xhair'), vb = svg.viewBox.baseVal;
  svg.addEventListener('mousemove', e => {{
    const r = svg.getBoundingClientRect();
    const mx = (e.clientX - r.left) * vb.width / r.width;
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
    tip.style.top = '18px';
  }});
  svg.addEventListener('mouseleave', () => {{
    tip.style.display = 'none'; xh.style.display = 'none';
  }});
}});
</script></body></html>"""

    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(page, encoding="utf-8")

    md = [f"# STEWARD — {now_et():%Y-%m-%d %H:%M} ET",
          f"\nEquity: {f'${eq:,.0f}' if eq else '—'}",
          f"SPY shadow: {f'${bv:,.0f}' if bv else '—'}",
          f"Regime: {'RISK-ON' if regime else 'RISK-OFF' if regime is not None else '—'}\n"]
    for n in analysis.get("notes", []):
        md.append(f"- {n}")
    OUT_MD.write_text("\n".join(md) + "\n", encoding="utf-8")
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
