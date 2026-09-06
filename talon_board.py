#!/usr/bin/env python3
"""TALON phone page — board/talon.html, published to the public bot-arena-board
Pages repo by the talon workflow after every ring (six a day, seven days a week).

Everything on the page is rebuilt from committed files plus one keyless fetch of
BTC daily bars for the regime line and the buy-and-hold benchmark:

  state/talon/gate.json         the backtest verdict the cycle checks, with every check
  state/talon/talon_state.json  floors, locked reserve, halt, high-water (absent until the first cycle)
  journal/talon/*.jsonl         one row per event; snapshot rows make the equity curve
  config/talon.yaml             the knobs quoted in the footer

While the gate is failing the page says so first, because that is the whole story.
`render()` is pure so the tests can exercise it offline; `main()` gathers inputs.
"""
from __future__ import annotations

import argparse
import html
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from core.common import ROOT, now_et  # noqa: E402
from dashboard import line_chart  # noqa: E402

OUT = ROOT / "board" / "talon.html"
RING_HOURS_ET = (0, 4, 8, 12, 16, 20)   # bell.yml case table; mirrors the UTC cron in talon.yml


# ---------------------------------------------------------------------------
# loaders (all optional — the page degrades to "gated, nothing recorded yet")
# ---------------------------------------------------------------------------

def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def load_journal(journal_dir: Path, max_rows: int = 4000) -> list[dict]:
    rows: list[dict] = []
    if not journal_dir.exists():
        return rows
    for f in sorted(journal_dir.glob("*.jsonl")):
        for line in f.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    rows.append(json.loads(line))
                except Exception:
                    continue
    return rows[-max_rows:]


def fetch_btc(cfg: dict) -> dict:
    """{'closes': {date: close}, 'sma': float, 'last': float, 'asof': date} or {} on any failure."""
    try:
        from talon.data import drop_incomplete_bar, fetch_bars
        from talon.strategies import sma
        d = cfg["data"]
        bench = cfg["universe"]["benchmark"]
        bars = fetch_bars([bench], d["timeframe"], d["lookback_bars"], cfg=cfg)
        bars = drop_incomplete_bar(bars, d["timeframe"])
        df = bars[bench]
        if df.empty:
            return {}
        s = sma(df["close"], cfg["regime"]["sma_days"])
        return {
            "closes": {f"{ts:%Y-%m-%d}": float(v) for ts, v in df["close"].items()},
            "sma": float(s.iloc[-1]) if s.iloc[-1] == s.iloc[-1] else None,
            "last": float(df["close"].iloc[-1]),
            "asof": f"{df.index[-1]:%Y-%m-%d}",
        }
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def esc(x) -> str:
    return html.escape(str(x))


def money(x, nd=0) -> str:
    try:
        return f"${float(x):,.{nd}f}"
    except Exception:
        return "—"


def pct(x, nd=1) -> str:
    try:
        return f"{float(x) * 100:+.{nd}f}%"
    except Exception:
        return "—"


def next_ring(now: datetime) -> datetime:
    for h in RING_HOURS_ET:
        cand = now.replace(hour=h, minute=0, second=0, microsecond=0)
        if cand > now:
            return cand
    return (now + timedelta(days=1)).replace(hour=RING_HOURS_ET[0], minute=0, second=0, microsecond=0)


def tile(label: str, val: str, ret: float | None, color: str) -> str:
    d = ""
    if ret is not None:
        up = ret >= 0
        d = f'<div class="delta {"up" if up else "down"}">{"▲" if up else "▼"} {ret:+.2f}%</div>'
    return (f'<div class="tile" style="border-top:4px solid {color}">'
            f'<div class="tlabel">{esc(label)}</div><div class="tvalue">{val}</div>{d}</div>')


# ---------------------------------------------------------------------------
# render (pure)
# ---------------------------------------------------------------------------

def render(cfg: dict, gate: dict, state: dict, journal: list[dict], btc: dict, now: datetime) -> str:
    risk, pos_cfg, acc_cfg = cfg["risk"], cfg["rising_floor"]["position"], cfg["rising_floor"]["account"]
    start = float(risk["starting_equity"])
    name = cfg["meta"]["name"]

    # ---- gate banner ------------------------------------------------------
    if not gate:
        gate_line = ('<div class="leader gate fail">Gate: <b>NOT RUN</b> · no state/talon/gate.json — '
                     'the cycle refuses to trade until backtest/backtest.py writes one that passes</div>')
        checks_html = '<div class="empty">Run the backtest to populate this.</div>'
    else:
        passed = bool(gate.get("passed"))
        fails = [c for c in gate.get("checks", []) if not c.get("ok")]
        gen = esc(str(gate.get("generated_utc", ""))[:16].replace("T", " "))
        gate_line = (f'<div class="leader gate {"pass" if passed else "fail"}">Gate: '
                     f'<b>{"PASSED 🟢 — trading enabled" if passed else "FAILING 🔴 — no orders are placed"}</b>'
                     f' · {len(gate.get("checks", [])) - len(fails)}/{len(gate.get("checks", []))} checks'
                     f' · backtest {gen} UTC</div>')
        rows = ""
        for c in gate.get("checks", []):
            win = f' <span class="dim">{esc(c["window"])}</span>' if c.get("window") else ""
            v, t = c.get("value"), c.get("threshold")
            fmt = lambda x: (f"{x:.3f}" if isinstance(x, float) else esc(x))  # noqa: E731
            rows += (f'<tr><td class="sym">{"✅" if c.get("ok") else "❌"} {esc(c["check"])}{win}</td>'
                     f'<td class="num">{fmt(v)}</td><td class="num">{fmt(t)}</td></tr>')
        checks_html = ("<table class='tbl'><thead><tr><th>check</th><th>value</th><th>threshold</th></tr></thead>"
                       f"<tbody>{rows}</tbody></table>")
        m = gate.get("metrics", {})
        if m:
            arms = [("TALON", m.get("strategy", {})), ("BTC hold", m.get("btc_hold", {})),
                    ("equal-weight", m.get("equal_weight", {}))]
            brow = "".join(
                f"<tr><td class='sym'>{esc(a)}</td><td class='num'>{pct(x.get('total_return_pct'))}</td>"
                f"<td class='num'>{x.get('sharpe', '—')}</td><td class='num'>{pct(x.get('max_drawdown_pct'), 0)}</td></tr>"
                for a, x in arms if x)
            w = gate.get("window", {})
            checks_html += (f"<p class='why'>Backtest window {esc(str(w.get('start', ''))[:10])} → "
                            f"{esc(str(w.get('end', ''))[:10])} · {esc(len(gate.get('universe', [])))} names · "
                            f"{m.get('strategy', {}).get('trades', '—')} trades</p>"
                            "<table class='tbl'><thead><tr><th>arm</th><th>return</th><th>Sharpe</th><th>max DD</th></tr></thead>"
                            f"<tbody>{brow}</tbody></table>")

    # ---- account + regime + halt standing ---------------------------------
    snaps = [r for r in journal if r.get("event") == "snapshot"]
    snap = state.get("last_snapshot") or (snaps[-1] if snaps else {})
    af = state.get("account_floor", {})
    eq = snap.get("equity")
    locked = float(af.get("locked", snap.get("locked", 0.0)) or 0.0)

    leaders = []
    if btc.get("last") and btc.get("sma"):
        on = btc["last"] > btc["sma"]
        leaders.append(f'<div class="leader">Regime: <b>{"ON 🟢" if on else "OFF 🟡 — no new entries"}</b> · '
                       f'BTC {money(btc["last"])} vs {cfg["regime"]["sma_days"]}-day SMA {money(btc["sma"])} '
                       f'({(btc["last"] / btc["sma"] - 1) * 100:+.1f}%) · completed bar {esc(btc.get("asof", ""))}</div>')
    elif snap.get("regime_on") is not None:
        leaders.append(f'<div class="leader">Regime at last cycle: <b>{"ON 🟢" if snap["regime_on"] else "OFF 🟡"}</b></div>')
    if state:
        hw = float(state.get("equity_high_water", 0) or 0)
        dd = (1 - float(eq) / hw) * 100 if (eq and hw) else None
        halted = state.get("halted")
        standing = (f"<b>HALTED</b> — {esc(state.get('halt_reason', ''))}" if halted else
                    (f"{dd:.1f}% below high-water {money(hw)} · kill switch at −{float(risk['kill_switch_dd_pct']) * 100:.0f}%, "
                     f"daily breaker at −{float(risk['daily_loss_breaker_pct']) * 100:.0f}%" if dd is not None else "no equity recorded"))
        leaders.append(f'<div class="leader">Risk: {standing}</div>')
        leaders.append(f'<div class="leader">Account floor: <b>{money(locked)}</b> locked · basis {money(af.get("basis", 0))} · '
                       f'sweeps {float(acc_cfg["sweep_fraction"]) * 100:.0f}% of gains past +{float(acc_cfg["activation_gain_pct"]) * 100:.0f}% · '
                       f'the bot cannot lower this</div>')
    else:
        leaders.append('<div class="leader">Account: <b>no cycle has run yet</b> — every ring so far was skipped at the gate</div>')

    # ---- tiles + chart ------------------------------------------------------
    curve: dict[str, float] = {}
    for r in snaps:
        curve[str(r.get("ts", ""))[:10]] = float(r["equity"])
    port = sorted(curve.items())
    bench: list[tuple[str, float]] = []
    closes = btc.get("closes", {})
    if port and closes:
        days = [d for d, _ in port if d in closes]
        if days:
            base = closes[days[0]]
            bench = [(d, start * closes[d] / base) for d in days]
    bv = bench[-1][1] if bench else None
    tiles = (tile(f"{name} equity", money(eq) if eq else "gated", (float(eq) / start - 1) * 100 if eq else None, "var(--series-1)")
             + tile("BTC buy & hold", money(bv) if bv else "—", (bv / start - 1) * 100 if bv else None, "var(--series-2)"))
    if port:
        chart = line_chart([sr for sr in [
            {"label": name, "short": name, "color": "var(--series-1)", "points": port},
            {"label": "BTC buy & hold", "short": "BTC", "color": "var(--series-2)", "points": bench},
        ] if sr["points"]])
    else:
        chart = '<div class="empty">Equity curve appears after the first cycle that gets past the gate.</div>'

    # ---- open positions ------------------------------------------------------
    positions = state.get("positions", {})
    if positions:
        rows = ""
        for sym, f in sorted(positions.items()):
            entry, floor, hw = float(f["entry"]), float(f["floor"]), float(f["high_water"])
            r_now = (hw - entry) / float(f["r_unit"]) if f.get("r_unit") else 0.0
            rows += (f"<tr><td class='sym'>{esc(sym)}</td><td class='num'>{money(entry, 2)}</td>"
                     f"<td class='num'>{money(floor, 2)}</td><td class='num'>{esc(f.get('stage', ''))}</td>"
                     f"<td class='num'>{r_now:+.1f}R</td><td class='num'>{(floor / entry - 1) * 100:+.1f}%</td></tr>")
        positions_html = ("<table class='tbl'><thead><tr><th>pair</th><th>entry</th><th>floor</th><th>stage</th>"
                          "<th>peak R</th><th>floor vs entry</th></tr></thead><tbody>" + rows + "</tbody></table>"
                          "<p class='why'>Floors only move up. Exits are judged on the close of the last completed daily bar.</p>")
    else:
        positions_html = '<div class="empty">No open positions.</div>'

    # ---- latest cycle ---------------------------------------------------------
    plans = [r for r in journal if r.get("event") == "plan"]
    if plans:
        p = plans[-1]
        run_id = esc(p.get("run_id", ""))
        acts = [r for r in journal if r.get("run_id") == p.get("run_id") and r.get("event") in
                ("entry", "exit", "exit_signal", "floor_up", "sweep", "halt", "order_failed", "adopt", "flatten")]
        cand_rows = ""
        for c in sorted(p.get("candidates", []), key=lambda c: -float(c.get("score", 0))):
            mom = c.get("momentum")
            cand_rows += (f"<tr><td class='sym'>{esc(c['symbol'])}</td><td class='num'>{float(c.get('score', 0)):.2f}</td>"
                          f"<td class='num'>{pct(mom) if mom is not None else '—'}</td>"
                          f"<td class='num'>{esc(', '.join(c.get('reasons', [])) or '—')}</td></tr>")
        targets = p.get("targets", {})
        tgt = ", ".join(f"{esc(s)} {money(n)}" for s, n in targets.items()) if targets else "none"
        notes = "".join(f"<p class='why'>• {esc(n)}</p>" for n in p.get("notes", []))
        act_rows = "".join(f"<p class='why'>{esc(str(a.get('ts', ''))[11:16])} {esc(a['event'])} "
                           f"{esc(a.get('symbol', ''))} {esc(a.get('reason', a.get('error', '')))}</p>" for a in acts[-12:])
        session = (f'<div class="why"><b>{run_id}</b> · tradeable {money(p.get("tradeable"))} · planned entries: {tgt}</div>{notes}'
                   + (f"<table class='tbl'><thead><tr><th>pair</th><th>score</th><th>momentum</th><th>voters</th></tr></thead>"
                      f"<tbody>{cand_rows}</tbody></table>" if cand_rows else "")
                   + (f"<p class='why'><b>Actions this run</b></p>{act_rows}" if act_rows else ""))
    else:
        session = ('<div class="empty">No cycle has got past the gate yet. Each ring checks state/talon/gate.json, '
                   'finds passed=false, posts a notice and skips. Nothing is bought or sold.</div>')

    # ---- schedule --------------------------------------------------------------
    last_run = state.get("last_run_utc")
    nxt = next_ring(now)
    sched = (f"<p class='why'>Rings at {', '.join(f'{h:02d}:00' for h in RING_HOURS_ET)} ET every day via the bell "
             f"(GitHub's own cron also fires, usually 2–3 hours late). Next ring <b>{nxt:%a %I:%M %p} ET</b>.</p>"
             f"<p class='why'>Last cycle that reached the broker: <b>{esc(str(last_run)[:16].replace('T', ' ') + ' UTC') if last_run else 'none yet'}</b>.</p>")

    strategies = [n for n, s in cfg["strategies"].items() if float(s.get("weight", 0)) > 0]
    updated = now.strftime("%b %d, %I:%M %p")
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="900">
<title>{esc(name)} — crypto trend bot</title>
<style>
.viz-root {{
  color-scheme: light;
  --surface-1:#fcfcfb; --page:#f9f9f7; --ink-1:#0b0b0b; --ink-2:#52514e;
  --muted:#898781; --grid:#e1e0d9; --axis:#c3c2b7;
  --series-1:#2a78d6; --series-2:#eb6834;
  --good-text:#006300; --crit:#d03b3b; --ring:rgba(11,11,11,0.10);
  --pass-bg:#e6f4e6; --fail-bg:#fbe7e7;
}}
@media (prefers-color-scheme: dark) {{
  :root:where(:not([data-theme="light"])) .viz-root {{
    color-scheme: dark;
    --surface-1:#1a1a19; --page:#0d0d0d; --ink-1:#ffffff; --ink-2:#c3c2b7;
    --muted:#898781; --grid:#2c2c2a; --axis:#383835;
    --series-1:#3987e5; --series-2:#d95926;
    --good-text:#0ca30c; --crit:#d03b3b; --ring:rgba(255,255,255,0.10);
    --pass-bg:#10290f; --fail-bg:#3a1414;
  }}
}}
body{{margin:0;background:var(--page);color:var(--ink-1);
  font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}}
.viz-root{{max-width:640px;margin:0 auto;padding:18px 14px 40px}}
h1{{font-size:19px;margin:0 0 2px}}
.nav{{font-size:13px;margin-bottom:8px}} .nav a{{color:var(--ink-2)}}
.sub{{color:var(--ink-2);font-size:13px;margin-bottom:12px}}
.leader{{font-size:14px;margin:6px 0 10px}}
.gate{{padding:10px 12px;border-radius:10px}} .gate.pass{{background:var(--pass-bg)}} .gate.fail{{background:var(--fail-bg)}}
.dim{{color:var(--muted);font-size:11px}}
.tiles{{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin:12px 0}}
.tile{{background:var(--surface-1);border:1px solid var(--ring);border-radius:12px;padding:12px 14px}}
.tlabel{{color:var(--ink-2);font-size:12px}} .tvalue{{font-size:26px;font-weight:650}}
.delta{{font-size:14px}} .delta.up{{color:var(--good-text)}} .delta.down{{color:var(--crit)}}
.panel{{background:var(--surface-1);border:1px solid var(--ring);border-radius:12px;padding:12px 14px;margin:10px 0;overflow-x:auto}}
h2{{font-size:15px;margin:18px 0 6px}}
.tbl{{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}}
.tbl th{{text-align:right;color:var(--muted);font-weight:500;font-size:12px;padding:4px 0 6px 10px;border-bottom:1px solid var(--axis)}}
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
.tooltip{{position:absolute;pointer-events:none;background:var(--surface-1);border:1px solid var(--ring);border-radius:8px;padding:6px 10px;font-size:12px;box-shadow:0 2px 8px rgba(0,0,0,.12);white-space:nowrap}}
footer{{color:var(--muted);font-size:12px;margin-top:22px}}
</style></head>
<body><div class="viz-root">
<div class="nav"><a href="index.html">← Arena scoreboard</a> · <a href="glider.html">GLIDER</a> · <a href="steward.html">STEWARD</a></div>
<h1>{esc(name)} — crypto trend bot with a rising floor</h1>
<div class="sub">Updated {updated} ET · Alpaca paper · {len(cfg["universe"]["symbols"])} pairs · daily bars, six checks a day</div>
{gate_line}
{"".join(leaders)}
<div class="tiles">{tiles}</div>
<div class="panel">{chart}</div>
<h2>Backtest gate</h2>
<div class="panel">{checks_html}</div>
<h2>Open positions</h2>
<div class="panel">{positions_html}</div>
<h2>Latest cycle</h2>
<div class="panel">{session}</div>
<h2>Schedule</h2>
<div class="panel">{sched}</div>
<footer>voters: {esc(", ".join(strategies))} + cross-sectional momentum · regime: BTC vs {cfg["regime"]["sma_days"]}-day SMA ·
{float(risk["risk_per_trade_pct"]) * 100:.0f}% risk/trade · max {cfg["ensemble"]["max_positions"]} positions ·
floor {pos_cfg["initial_atr_mult"]}×ATR then break-even at +1R, locks at +2/+3/+5R, {float(pos_cfg["trail_pct"]) * 100:.0f}% trail ·
{money(start)} paper account · <a href="https://github.com/ccsi9401/bot-arena/blob/main/TALON.md">manual</a></footer>
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


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--offline", action="store_true", help="skip the BTC fetch (regime line and benchmark omitted)")
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)
    cfg = yaml.safe_load((ROOT / "config" / "talon.yaml").read_text(encoding="utf-8"))
    gate = load_json(ROOT / cfg["paths"]["gate"], {})
    state = load_json(ROOT / cfg["paths"]["state"], {})
    journal = load_journal(ROOT / cfg["paths"]["journal_dir"])
    btc = {} if args.offline else fetch_btc(cfg)
    page = render(cfg, gate, state, journal, btc, now_et())
    out = Path(args.out) if args.out else OUT
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page, encoding="utf-8")
    print(f"wrote {out} ({len(page):,} bytes; gate={'passed' if gate.get('passed') else 'failing/absent'}; "
          f"positions={len(state.get('positions', {}))}; journal rows={len(journal)}; btc={'yes' if btc else 'no'})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
