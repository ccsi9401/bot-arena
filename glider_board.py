#!/usr/bin/env python3
"""GLIDER phone page — board/glider.html, published to the public
bot-arena-board Pages repo by the glider workflow (daily 15:30 ET cycle).

Shows: equity vs SPY race, the Markov 2.0 regime gate, open positions, the
latest session's decisions, and the self-learning stack's standing (learner
cooldown + reflection governor). Like STEWARD, GLIDER competes against nobody
until Round 2 starts, so it can afford to show holdings and reasoning.

The SPY race needs no data key: every session's scan.json already carries the
benchmark close, so the curve is rebuilt from committed journals.
"""
from __future__ import annotations

import html
import json
import sys
from datetime import date, timedelta
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from core.common import ROOT, now_et  # noqa: E402
from dashboard import line_chart  # noqa: E402

OUT = ROOT / "board" / "glider.html"
FIRST_SESSION = "first session Mon Aug 31, 3:30pm ET"


def equity_curve() -> list[tuple[str, float]]:
    f = ROOT / "state" / "glider" / "equity_curve.json"
    if not f.exists():
        return []
    by_day: dict[str, float] = {}
    for pt in json.loads(f.read_text(encoding="utf-8")):
        by_day[pt["ts_et"][:10]] = pt["equity"]
    return sorted(by_day.items())


def bench_closes() -> dict[str, float]:
    """SPY close per session day, from committed scan journals (no API needed)."""
    out: dict[str, float] = {}
    for rdir in sorted(ROOT.glob("journal/glider_2*")):
        f = rdir / "scan.json"
        if not f.exists():
            continue
        try:
            scan = json.loads(f.read_text(encoding="utf-8"))
            px = scan["symbols"][scan["benchmark"]]["close"]
            out[scan["asof_et"][:10]] = float(px)
        except Exception:
            continue
    return out


def latest_session() -> dict:
    """Newest journal dir that got past the market-closed check."""
    for rdir in sorted(ROOT.glob("journal/glider_2*"), reverse=True):
        if not rdir.is_dir() or not (rdir / "analysis.json").exists():
            continue
        out = {"run_id": rdir.name}
        for stage in ("scan", "analysis", "validation", "execution"):
            f = rdir / f"{stage}.json"
            if f.exists():
                try:
                    out[stage] = json.loads(f.read_text(encoding="utf-8"))
                except Exception:
                    pass
        return out
    return {}


def state_json(name: str, default):
    f = ROOT / "state" / "glider" / f"{name}.json"
    if not f.exists():
        return default
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return default


def main() -> int:
    cfg = yaml.safe_load((ROOT / "config" / "glider.yaml").read_text(encoding="utf-8"))
    s, risk, learn = cfg["strategy"], cfg["risk"], cfg.get("learning", {})
    start = cfg["starting_equity"]

    run = latest_session()
    analysis = run.get("analysis", {})
    validation = run.get("validation", {})
    scan = run.get("scan", {})

    port = equity_curve()
    eq = port[-1][1] if port else None

    closes = bench_closes()
    bench: list[tuple[str, float]] = []
    if port and closes:
        days = [d for d, _ in port if d in closes]
        if days:
            base = closes[days[0]]
            bench = [(d, start * closes[d] / base) for d in days]
    bv = bench[-1][1] if bench else None

    chart = line_chart([
        sr for sr in [
            {"label": "GLIDER (Claude)", "short": "GLIDER",
             "color": "var(--series-1)", "points": port},
            {"label": "SPY buy & hold", "short": "SPY",
             "color": "var(--series-2)", "points": bench},
        ] if sr["points"]]) if port else f'<div class="empty">No sessions yet — {FIRST_SESSION}.</div>'

    def tile(label, val, ret, color):
        d = ""
        if ret is not None:
            up = ret >= 0
            d = (f'<div class="delta {"up" if up else "down"}">'
                 f'{"▲" if up else "▼"} {ret:+.2f}%</div>')
        return (f'<div class="tile" style="border-top:4px solid {color}">'
                f'<div class="tlabel">{label}</div>'
                f'<div class="tvalue">{val}</div>{d}</div>')

    tiles = (tile("GLIDER (Claude)", f"${eq:,.0f}" if eq else "—",
                  (eq / start - 1) * 100 if eq else None, "var(--series-1)")
             + tile("SPY buy & hold", f"${bv:,.0f}" if bv else "—",
                    (bv / start - 1) * 100 if bv else None, "var(--series-2)"))

    # ---- Markov 2.0 regime gate badge ----
    gate = ""
    b = (scan.get("symbols") or {}).get(scan.get("benchmark", ""), {})
    sig, st = b.get("markov2_signal"), b.get("markov2_state", "?")
    if sig is not None:
        ok = analysis.get("regime_ok")
        gate = (f'<div class="leader">Regime gate: <b>{"OPEN 🟢" if ok else "CLOSED 🟡"}</b>'
                f' · P(bull)−P(bear) = {sig:+.3f} ({st})</div>')
    elif analysis:
        ok = analysis.get("regime_ok")
        gate = (f'<div class="leader">Regime gate: <b>{"OPEN 🟢" if ok else "CLOSED 🟡"}</b>'
                f' · 200SMA fallback (Markov matrix warming up)</div>')

    # ---- open positions ----
    ledger = state_json("ledger", {})
    if ledger:
        rows = ""
        for sym, rec in sorted(ledger.items()):
            age = ""
            try:
                age = f'{(date.fromisoformat(f"{now_et():%Y-%m-%d}") - date.fromisoformat(rec["opened"])).days}d'
            except Exception:
                pass
            entry = rec.get("entry")
            stop = rec.get("stop")
            rows += (f"<tr><td class='sym'>{html.escape(sym)}</td>"
                     f"<td class='num'>{f'${entry:,.2f}' if entry else '—'}</td>"
                     f"<td class='num'>{f'${stop:,.2f}' if stop else '—'}</td>"
                     f"<td class='num'>{age}</td></tr>")
        positions = ("<table class='tbl'><thead><tr><th>Symbol</th><th>Entry</th>"
                     "<th>Stop</th><th>Held</th></tr></thead><tbody>" + rows + "</tbody></table>")
    else:
        positions = f'<div class="empty">No open positions{"" if port else " — " + FIRST_SESSION}.</div>'

    # ---- latest session ----
    session = ""
    if analysis:
        counts = (f'{len(analysis.get("intents", []))} intents · '
                  f'{len(validation.get("approved", []))} approved · '
                  f'{len(validation.get("rejected", []))} rejected'
                  if validation else f'{len(analysis.get("intents", []))} intents')
        notes = "".join(f"<p class='why'>• {html.escape(n)}</p>"
                        for n in analysis.get("notes", []))
        session = (f'<div class="why"><b>{run["run_id"]}</b> · {counts}</div>{notes}')
    else:
        session = f'<div class="empty">Appears after the first cycle — {FIRST_SESSION}.</div>'

    # ---- self-learning standing ----
    hist = state_json("learn_history", [])
    refl = state_json("reflection", {})
    exit_desc = (f'trail {s.get("trail_atr_mult")}×ATR' if s.get("exit_mode") == "trail"
                 else f'target {s.get("target_r_mult")}R')
    params = (f'RSI2 &lt; {s.get("pullback_rsi2_max")} · ≤{s.get("max_pct_below_52wk_high")}% off 52wk high · '
              f'stop {s.get("stop_atr_mult")}×ATR · exit {exit_desc} · '
              f'time stop {s.get("max_hold_days")}d · gate {s.get("regime_filter")}')
    learn_rows = [f"<p class='why'>Current knobs: {params}</p>"]
    last_change = next((h for h in reversed(hist) if h.get("changed")), None)
    last_run = hist[-1] if hist else None
    if last_run:
        learn_rows.append(f"<p class='why'>Last learner run {last_run.get('date')}: "
                          f"{html.escape(str(last_run.get('decision', '')))}</p>")
    if last_change:
        unlocked = date.fromisoformat(last_change["date"]) + timedelta(
            days=int(learn.get("min_days_between_changes", 28)))
        learn_rows.append(f"<p class='why'>Config locked by cooldown until <b>{unlocked:%b %d}</b> "
                          f"— the monthly learner evaluates but cannot change anything before then.</p>")
    if refl:
        n = refl.get("live_closed_trades", 0)
        need = int(learn.get("min_live_trades", 30))
        learn_rows.append(f"<p class='why'>Reflection (Sat): {n}/{need} closed live trades — "
                          f"{html.escape(str(refl.get('summary', '')))}</p>")
    learning = "".join(learn_rows)

    page = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="900">
<title>GLIDER — Claude's swing bot</title>
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
<div class="nav"><a href="index.html">← Arena scoreboard</a> · <a href="steward.html">STEWARD →</a></div>
<h1>GLIDER — Claude's swing bot</h1>
<div class="sub">Updated {now_et():%b %d, %I:%M %p} ET · trades daily at 3:30pm ET ·
self-learning (monthly, gated)</div>
{gate}
<div class="tiles">{tiles}</div>
<div class="panel">{chart}</div>
<h2>Open positions</h2>
<div class="panel">{positions}</div>
<h2>Latest session</h2>
<div class="panel">{session}</div>
<h2>Self-learning</h2>
<div class="panel">{learning}</div>
<footer>trend-pullback entries · Markov 2.0 regime gate (stride matrix) ·
{risk["risk_per_trade_pct"]}% risk/trade · max {risk["max_positions"]} positions ·
kill switch −{risk["kill_switch_drawdown_pct"]:.0f}% ·
${start:,.0f} paper account</footer>
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
