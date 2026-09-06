#!/usr/bin/env python3
"""STEWARD phone page — board/steward.html, published to the public
bot-arena-board Pages repo by the steward workflow (nightly pulse + Friday cycle).

Shows: portfolio vs SPY race, regime badge, current allocation, and the
manager's latest notes. STEWARD competes against nobody, so unlike the arena
board it can afford to show its holdings and reasoning.
"""
from __future__ import annotations

import html
import json
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from core.common import ROOT, now_et  # noqa: E402
from dashboard import line_chart  # noqa: E402

OUT = ROOT / "board" / "steward.html"


def curve_from(name: str) -> list[tuple[str, float]]:
    f = ROOT / "state" / "steward" / f"{name}.json"
    if not f.exists():
        return []
    by_day: dict[str, float] = {}
    for pt in json.loads(f.read_text(encoding="utf-8")):
        by_day[pt["ts_et"][:10]] = pt["equity"]
    return sorted(by_day.items())


def latest_cycle() -> dict:
    for rdir in sorted(ROOT.glob("journal/steward_2*"), reverse=True):
        if not rdir.is_dir():
            continue
        out = {"run_id": rdir.name}
        for stage in ("analysis", "plan", "execution"):
            f = rdir / f"{stage}.json"
            if f.exists():
                try:
                    out[stage] = json.loads(f.read_text(encoding="utf-8"))
                except Exception:
                    pass
        if len(out) > 1:
            return out
    return {}


def state_json(name: str, default):
    f = ROOT / "state" / "steward" / f"{name}.json"
    if not f.exists():
        return default
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return default


def main() -> int:
    cfg = yaml.safe_load((ROOT / "config" / "steward.yaml").read_text(encoding="utf-8"))
    run = latest_cycle()
    analysis = run.get("analysis", {})
    p = run.get("plan", {})

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

    regime = analysis.get("regime_on")
    regime_html = ("" if regime is None else
                   f'<div class="leader">Regime: <b>{"RISK-ON 🟢" if regime else "RISK-OFF 🟡"}</b>'
                   f' · SPY vs its {cfg["strategy"].get("trend_sma_days", 200)}-day SMA</div>')

    # ---- rebalance standing (date-based, see run_steward.cycle_status) ----
    cs = state_json("cycle_status", {})
    if cs:
        if cs.get("overdue"):
            standing = (f'<b>OVERDUE</b> — none since {cs.get("last_cycle_date_et") or "inception"}, '
                        f'{cs.get("days_since_anchor", 0)}d past the Friday slot')
        elif cs.get("due"):
            standing = "due at this Friday's 3:45pm slot"
        else:
            standing = f'done {cs.get("last_cycle_date_et")} · next Friday'
        regime_html += f'<div class="leader">Weekly rebalance: {standing}</div>'

    # ---- kill switch standing: inception floor OR high-water mark, whichever first ----
    risk = cfg["risk"]
    if eq:
        peak = max([start] + [v for _, v in port])
        peak_dd = (peak - eq) / peak * 100
        incp_dd = (start - eq) / start * 100
        regime_html += (f'<div class="leader">Kill switch: −{risk["kill_switch_drawdown_pct"]:.0f}% from start '
                        f'or −{risk.get("kill_switch_peak_drawdown_pct", 0):.0f}% off peak · '
                        f'book is {peak_dd:.1f}% below peak ${peak:,.0f}'
                        f'{" and " + f"{incp_dd:.1f}% below start" if incp_dd > 0 else ""}</div>')

    # ---- mandate + 10-year replay vs SPY (from the committed gate report) ----
    replay = ""
    gate_f = ROOT / "reports" / "backtest" / "steward.json"
    if gate_f.exists():
        try:
            g = json.loads(gate_f.read_text(encoding="utf-8"))
            s_, b_ = g["summary"], g["benchmark"]
            replay = (
                "<p class='why'>STEWARD is the <b>balanced book</b>: index ETFs in healthy regimes, a "
                "permanent bond/gold/T-bill ballast, defense when SPY loses its trend. Its job is to "
                "beat SPY on risk-adjusted return and drawdown, not on raw return — a book that is at "
                "most 70% equities cannot out-run the index in a bull decade, by design.</p>"
                "<table class='tbl'><thead><tr><th>10-year replay "
                f"{s_.get('window_start', '')} → {s_.get('window_end', '')}</th>"
                "<th>STEWARD</th><th>SPY</th></tr></thead><tbody>"
                f"<tr><td class='sym'>Total return</td><td class='num'>{s_['total_return_pct']:+.0f}%</td>"
                f"<td class='num'>{b_['total_return_pct']:+.0f}%</td></tr>"
                f"<tr><td class='sym'>CAGR</td><td class='num'>{s_['cagr_pct']:.1f}%</td>"
                f"<td class='num'>{b_['cagr_pct']:.1f}%</td></tr>"
                f"<tr><td class='sym'>Max drawdown</td><td class='num'>{s_['max_drawdown_pct']:.1f}%</td>"
                f"<td class='num'>{b_['max_drawdown_pct']:.1f}%</td></tr>"
                f"<tr><td class='sym'>Sharpe (daily, ann.)</td><td class='num'>{s_['sharpe_daily_ann']:.2f}</td>"
                f"<td class='num'>{b_['sharpe_daily_ann']:.2f}</td></tr>"
                "</tbody></table>"
                f"<p class='why'>Gate {'PASSED' if g.get('passed') else 'FAILED'}: "
                f"{', '.join(k for k, v in g.get('gate', {}).items() if v)}. "
                "Weekly rebalance, fills at close + 5 bps.</p>")
        except Exception:
            replay = ""

    alloc = ""
    if p.get("targets_final"):
        cur = p.get("current_weights", {})
        rows = ""
        for sym, w in sorted(p["targets_final"].items(), key=lambda kv: -kv[1]):
            rows += (f"<tr><td class='sym'>{sym}</td>"
                     f"<td class='num'>{cur.get(sym, 0)*100:.1f}%</td>"
                     f"<td class='num'>{w*100:.1f}%</td></tr>")
        rows += (f"<tr><td class='sym'>CASH</td><td class='num'>—</td>"
                 f"<td class='num'>{p.get('cash_target', 0)*100:.1f}%</td></tr>")
        alloc = ("<table class='tbl'><thead><tr><th>Holding</th><th>Now</th>"
                 "<th>Target</th></tr></thead><tbody>" + rows + "</tbody></table>")
    else:
        alloc = ('<div class="empty">The first portfolio gets built at the first '
                 'Friday 3:45pm ET cycle.</div>')

    notes = "".join(f"<p class='why'>• {html.escape(n)}</p>"
                    for n in analysis.get("notes", []))

    page = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="900">
<title>STEWARD — Claude's portfolio</title>
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
.nav{{font-size:13px;margin-bottom:8px}} .nav a{{color:var(--ink-2)}}
.sub{{color:var(--ink-2);font-size:13px;margin-bottom:12px}}
.leader{{font-size:14px;margin:6px 0 10px}}
.tiles{{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin:12px 0}}
.tile{{background:var(--surface-1);border:1px solid var(--ring);border-radius:12px;
  padding:12px 14px}}
.tlabel{{color:var(--ink-2);font-size:12px}} .tvalue{{font-size:26px;font-weight:650}}
.delta{{font-size:14px}} .delta.up{{color:var(--good-text)}} .delta.down{{color:var(--crit)}}
.panel{{background:var(--surface-1);border:1px solid var(--ring);border-radius:12px;
  padding:12px 14px;margin:10px 0;overflow-x:auto}}
h2{{font-size:15px;margin:18px 0 6px}}
.tbl{{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}}
.tbl th{{text-align:right;color:var(--muted);font-weight:500;font-size:12px;
  padding:4px 0 6px 10px;border-bottom:1px solid var(--axis)}}
.tbl th:first-child{{text-align:left;padding-left:0}}
.tbl td{{padding:7px 0 7px 10px;border-bottom:1px solid var(--grid);text-align:right}}
.tbl td.sym{{text-align:left;font-weight:600;padding-left:0}}
.num{{font-variant-numeric:tabular-nums}}
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
<div class="nav"><a href="index.html">← Arena scoreboard</a> · <a href="glider.html">GLIDER →</a></div>
<h1>STEWARD — Claude's portfolio</h1>
<div class="sub">Updated {now_et():%b %d, %I:%M %p} ET · weekly decisions (Fri 3:45pm),
nightly pulse · the balanced book</div>
{regime_html}
<div class="tiles">{tiles}</div>
<div class="panel">{chart}</div>
<h2>Allocation</h2>
<div class="panel">{alloc}</div>
<h2>Manager's notes</h2>
<div class="panel">{notes or '<div class="empty">Appear after the first cycle.</div>'}</div>
<h2>What this book is for</h2>
<div class="panel">{replay or '<div class="empty">The 10-year replay appears once the gate has run.</div>'}</div>
<footer>index sleeve (SPY/QQQ, momentum split) · {cfg["strategy"].get("trend_sma_days", 200)}-day regime gate ·
permanent defensive ballast (IEF/GLD/SHY) ·
kill switch −{cfg["risk"]["kill_switch_drawdown_pct"]:.0f}% from start / −{cfg["risk"].get("kill_switch_peak_drawdown_pct", 0):.0f}% off peak ·
${cfg["starting_equity"]:,.0f} paper account</footer>
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

    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(page, encoding="utf-8")
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
