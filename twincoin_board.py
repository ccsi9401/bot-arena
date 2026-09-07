#!/usr/bin/env python3
"""TWINCOIN phone page — board/twincoin.html, published to the public bot-arena-board
Pages repo by the twincoin workflow after every ring (six a day, seven days a week).

Rebuilt from committed files plus one keyless fetch of Alpaca bars (for the live six-vote
and the BTC buy-and-hold benchmark):

  state/twincoin/armed.json           the trade permit and the $500 ledger's zero point
  state/twincoin/twincoin_state.json  ledger, positions, floors, halt, high-water, last vote
  journal/twincoin/*.jsonl            one row per event; snapshot rows make the equity curve
  journal/twincoin/trades.csv         closed trades with R multiples
  config/twincoin.yaml                the knobs quoted in the footer

`render()` is pure so tests can run it offline; `main()` gathers inputs.
"""
from __future__ import annotations

import argparse
import csv
import html
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from core.common import ROOT, now_et  # noqa: E402
from dashboard import line_chart  # noqa: E402

OUT = ROOT / "board" / "twincoin.html"
RING_UTC_HOURS = (0, 4, 8, 12, 16, 20)
RING_MINUTE = 15
VOTE_ORDER = [("trend", "Trend D+4H"), ("rsi", "RSI 14"), ("macd", "MACD"), ("ema50", "50 EMA"), ("ema200", "200 EMA"), ("volume", "Volume")]


# ----------------------------------------------------------------- loaders (all optional)
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


def load_trades(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def fetch_live(cfg: dict) -> dict:
    """{'votes': {sym: {...}}, 'btc_closes': {date: close}, 'last': {sym: close}} or {} on failure."""
    try:
        from twincoin.data import load_market
        from twincoin.strategy import indicators_4h, indicators_daily, vote
        daily, h4, _ = load_market(cfg)
        out = {"votes": {}, "btc_closes": {}, "last": {}}
        for sym in cfg["universe"]["symbols"]:
            d = indicators_daily(daily[sym], int(cfg["signal"]["ema50_slope_bars"]), int(cfg["signal"]["obv_lookback"]))
            h = indicators_4h(h4[sym])
            r, hr = d.iloc[-1], h.iloc[-1]
            b, s, det = vote(r, hr, tuple(cfg["signal"]["rsi_bull"]), float(cfg["signal"]["rsi_bear"]))
            out["votes"][sym] = {"day": f"{d.index[-1]:%Y-%m-%d}", "bull": b, "bear": s, "detail": det, "close": float(r.close),
                                 "atr": float(r.atr), "ema20": float(r.ema20), "ema50": float(r.ema50), "ema200": float(r.ema200), "rsi": float(r.rsi)}
            out["last"][sym] = float(h["close"].iloc[-1])
            if sym.startswith("BTC"):
                out["btc_closes"] = {f"{ts:%Y-%m-%d}": float(v) for ts, v in daily[sym]["close"].items()}
        return out
    except Exception:
        return {}


# ----------------------------------------------------------------- helpers
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


def next_ring_et(now_utc: datetime) -> datetime:
    """Next :15 past a UTC hour divisible by 4, returned in ET."""
    base = now_utc.replace(minute=0, second=0, microsecond=0)
    for k in range(0, 30):
        cand = base + timedelta(hours=k)
        if cand.hour in RING_UTC_HOURS:
            cand = cand.replace(minute=RING_MINUTE)
            if cand > now_utc:
                return cand.astimezone(now_et().tzinfo)
    return now_utc


def tile(label: str, val: str, ret: float | None, color: str) -> str:
    d = ""
    if ret is not None:
        up = ret >= 0
        d = f'<div class="delta {"up" if up else "down"}">{"▲" if up else "▼"} {ret:+.2f}%</div>'
    return (f'<div class="tile" style="border-top:4px solid {color}">'
            f'<div class="tlabel">{esc(label)}</div><div class="tvalue">{val}</div>{d}</div>')


def chip(v: int) -> str:
    return {1: '<span class="chip b">bull</span>', -1: '<span class="chip s">bear</span>'}.get(v, '<span class="chip n">neutral</span>')


# ----------------------------------------------------------------- render (pure)
def render(cfg: dict, armed: dict, state: dict, journal: list[dict], trades: list[dict], live: dict, now_utc: datetime) -> str:
    R, L, S = cfg["risk"], cfg["ledger"], cfg["signal"]
    basis = float(L["basis_equity"])
    name = cfg["meta"]["name"].upper()
    syms = list(cfg["universe"]["symbols"])
    snaps = [r for r in journal if r.get("event") == "snapshot"]
    snap = snaps[-1] if snaps else {}
    eq = snap.get("ledger_equity", state.get("basis_equity") if state else None)
    hwm = float(state.get("hwm", basis)) if state else basis

    # ---- standing banner --------------------------------------------------
    if not armed.get("armed"):
        standing = '<div class="leader gate fail">Standing: <b>NOT ARMED</b> · rings skip until <code>--mode arm</code> has run</div>'
    elif state.get("halt_until"):
        standing = (f'<div class="leader gate fail">Standing: <b>HALTED by kill switch</b> until '
                    f'{esc(str(state["halt_until"])[:16].replace("T", " "))} UTC · then {float(R["kill_risk"]) * 100:.1f}% risk for {R["kill_trades"]} trades</div>')
    elif state.get("reconcile_halt"):
        standing = '<div class="leader gate fail">Standing: <b>ENTRIES HALTED</b> · a position vanished from the account; exits still run · clear with <code>--mode clear-halt</code></div>'
    else:
        dd = (1 - float(eq) / hwm) * 100 if (eq and hwm) else 0.0
        blocks = []
        if state.get("no_entry_until") and str(state["no_entry_until"]) > now_utc.isoformat():
            blocks.append("daily loss limit")
        if state.get("week_block_until") and str(state["week_block_until"]) > now_utc.isoformat():
            blocks.append("weekly loss limit")
        if state.get("streak_until") and str(state["streak_until"]) > now_utc.isoformat():
            blocks.append("losing-streak pause")
        extra = f' · no new entries: {", ".join(blocks)}' if blocks else ""
        standing = (f'<div class="leader gate pass">Standing: <b>ARMED 🟢 — trading</b> · {dd:.1f}% below high-water {money(hwm, 2)} · '
                    f'kill switch at −{float(R["kill_dd"]) * 100:.0f}%{extra}</div>')

    # ---- tiles + chart ------------------------------------------------------
    curve: dict[str, float] = {}
    for r in snaps:
        curve[str(r.get("ts", ""))[:10]] = float(r["ledger_equity"])
    port = sorted(curve.items())
    bench: list[tuple[str, float]] = []
    closes = live.get("btc_closes", {})
    if port and closes:
        days = [d for d, _ in port if d in closes]
        if days:
            base = closes[days[0]]
            bench = [(d, basis * closes[d] / base) for d in days]
    bv = bench[-1][1] if bench else None
    tiles = (tile("Ledger equity", money(eq, 2) if eq is not None else "—", (float(eq) / basis - 1) * 100 if eq is not None else None, "var(--series-1)")
             + tile("BTC buy & hold, same start", money(bv, 2) if bv else "—", (bv / basis - 1) * 100 if bv else None, "var(--series-2)"))
    if len(port) >= 2:
        chart = line_chart([sr for sr in [
            {"label": name, "short": name, "color": "var(--series-1)", "points": port},
            {"label": "BTC buy & hold", "short": "BTC", "color": "var(--series-2)", "points": bench},
        ] if sr["points"]])
    else:
        chart = '<div class="empty">Equity curve appears after the second day of snapshots.</div>'

    # ---- signal board --------------------------------------------------------
    votes = live.get("votes") or state.get("last_vote", {}) or {}
    src = "live, keyless fetch" if live.get("votes") else "last cycle"
    vote_html = ""
    for sym in syms:
        v = votes.get(sym)
        if not v:
            vote_html += f'<div class="asset"><div class="ahead"><b>{esc(sym)}</b> <span class="dim">no vote yet</span></div></div>'
            continue
        det = v.get("detail", {})
        b, s = int(v.get("bull", 0)), int(v.get("bear", 0))
        verdict = "BUY" if b >= int(S["vote_min"]) else ("SELL vote — no entries" if s >= int(S["sell_vote_min"]) else "NO TRADE")
        cls = "buy" if verdict == "BUY" else ("sell" if verdict.startswith("SELL") else "flat")
        rows = "".join(f'<div class="vrow"><span class="vk">{esc(lbl)}</span>{chip(int(det.get(k, 0)))}</div>' for k, lbl in VOTE_ORDER)
        gate = "" if float(v.get("close", 0)) > float(v.get("ema200", 0)) else '<div class="why">Below the 200 EMA: no entries regardless of vote.</div>'
        vote_html += (f'<div class="asset"><div class="ahead"><b>{esc(sym)}</b> <span class="dim">daily {esc(v.get("day", ""))} · '
                      f'close {money(v.get("close"), 2)} · ATR {money(v.get("atr"), 0)} ({float(v.get("atr", 0)) / max(float(v.get("close", 1)), 1e-9) * 100:.1f}%) · RSI {float(v.get("rsi", 0)):.0f}</span></div>'
                      f'<div class="votes">{rows}</div>'
                      f'<div class="verdict {cls}">{b} bull · {s} bear → {verdict}</div>{gate}</div>')
    vote_note = f'<p class="why">Source: {src}. Entry needs {S["vote_min"]} of 6 bullish at a UTC daily close; the vote never exits a position.</p>'

    # ---- open positions ------------------------------------------------------
    positions = state.get("positions", {}) if state else {}
    if positions:
        rows = ""
        for sym, p in sorted(positions.items()):
            entry, floor, rpx = float(p["entry"]), float(p["floor"]), float(p["risk_px"])
            last = live.get("last", {}).get(sym)
            r_now = (last - entry) / rpx if (last and rpx) else None
            rows += (f"<tr><td class='sym'>{esc(sym)}</td><td class='num'>{money(entry, 2)}</td><td class='num'>{money(floor, 2)}</td>"
                     f"<td class='num'>{(floor / entry - 1) * 100:+.1f}%</td><td class='num'>{'—' if r_now is None else f'{r_now:+.2f}R'}</td>"
                     f"<td class='num'>{'✓' if p.get('partial_done') else money(p.get('partial_px'), 0)}</td></tr>")
        positions_html = ("<table class='tbl'><thead><tr><th>pair</th><th>entry</th><th>floor</th><th>floor vs entry</th><th>now</th><th>⅓ at</th></tr></thead>"
                          f"<tbody>{rows}</tbody></table>"
                          "<p class='why'>Floors only move up. The bot exits on the first 4H close below the floor; a stop-limit at floor×0.97 rests on the exchange as the backstop.</p>")
    else:
        positions_html = '<div class="empty">No open positions. Cash.</div>'

    # ---- closed trades ---------------------------------------------------------
    if trades:
        n = len(trades)
        pnl = sum(float(t["pnl"]) for t in trades)
        wins = sum(1 for t in trades if float(t["pnl"]) > 0)
        avg_r = sum(float(t["R"]) for t in trades) / n
        rows = "".join(f"<tr><td class='sym'>{esc(t['symbol'])}</td><td class='num'>{esc(str(t['t_out'])[:10])}</td>"
                       f"<td class='num'>{esc(t['reason'])}</td><td class='num'>{float(t['pnl']):+.2f}</td><td class='num'>{float(t['R']):+.2f}R</td></tr>"
                       for t in reversed(trades[-10:]))
        trades_html = (f"<p class='why'><b>{n} closed</b> · win rate {wins / n * 100:.0f}% · total {pnl:+.2f} · avg {avg_r:+.2f}R · "
                       f"backtest expects 37–40% wins, +0.20R to +0.45R</p>"
                       "<table class='tbl'><thead><tr><th>pair</th><th>closed</th><th>why</th><th>$</th><th>R</th></tr></thead>"
                       f"<tbody>{rows}</tbody></table>")
    else:
        trades_html = '<div class="empty">No closed trades yet.</div>'

    # ---- latest cycle ------------------------------------------------------------
    keep = {"entry_filled", "exit_filled", "partial_filled", "backstop_filled", "floor_ratchet", "floor_breached", "entry_blocked", "entry_skipped",
            "KILL_SWITCH", "daily_loss_limit", "weekly_loss_limit", "streak_pause", "orphan_adopted", "position_vanished", "cycle_error", "halt_lifted",
            "order_submitted", "exit_unfilled", "entry_unfilled"}
    acts = [r for r in journal if r.get("event") in keep][-14:]
    if snap:
        act_rows = "".join(f"<p class='why'>{esc(str(a.get('ts', ''))[5:16].replace('T', ' '))} <b>{esc(a['event'])}</b> {esc(a.get('symbol', ''))} "
                           f"{esc(a.get('why', a.get('reason', a.get('error', ''))))}"
                           + (f" @ {money(a.get('price'), 2)}" if a.get("price") else "")
                           + (f" floor {money(a.get('old'), 0)}→{money(a.get('new'), 0)}" if a.get("new") else "") + "</p>" for a in acts)
        session = (f"<div class='why'>Last bar processed <b>{esc(str(snap.get('bar', ''))[:16].replace('T', ' '))} UTC</b> · ledger {money(snap.get('ledger_equity'), 2)} · "
                   f"account {money(snap.get('account_equity'), 2)} · cash {money(snap.get('cash'), 2)}</div>"
                   + (f"<p class='why'><b>Recent actions</b></p>{act_rows}" if act_rows else "<p class='why'>No actions yet: every cycle so far was a vote and a snapshot.</p>"))
    else:
        session = '<div class="empty">No cycle has run yet.</div>'

    # ---- schedule ------------------------------------------------------------------
    nxt = next_ring_et(now_utc)
    et_hours = sorted({(datetime(2026, 1, 1, h, RING_MINUTE, tzinfo=timezone.utc).astimezone(now_et().tzinfo)).strftime("%I:%M %p").lstrip("0") for h in RING_UTC_HOURS})
    sched = (f"<p class='why'>Rings at :15 past every UTC hour divisible by four, which is {', '.join(et_hours)} ET right now (shifts an hour at DST). "
             f"Next ring <b>{nxt:%a %I:%M %p} ET</b>. The 00:15 UTC ring ({(datetime(2026, 1, 1, 0, 15, tzinfo=timezone.utc).astimezone(now_et().tzinfo)):%I:%M %p} ET) is the only one that can open a trade; the rest manage exits.</p>"
             f"<p class='why'>Armed {esc(str(armed.get('armed_at', ''))[:16].replace('T', ' '))} UTC at account equity {money(armed.get('account_equity_at_arm'), 2)}.</p>" if armed else "")

    updated = now_et().strftime("%b %d, %I:%M %p")
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="900">
<title>{esc(name)} — BTC/ETH trend bot</title>
<style>
.viz-root {{
  color-scheme: light;
  --surface-1:#fcfcfb; --page:#f9f9f7; --ink-1:#0b0b0b; --ink-2:#52514e;
  --muted:#898781; --grid:#e1e0d9; --axis:#c3c2b7;
  --series-1:#0e6b60; --series-2:#c86f0e;
  --good-text:#006300; --crit:#d03b3b; --ring:rgba(11,11,11,0.10);
  --pass-bg:#e6f4e6; --fail-bg:#fbe7e7; --bull-bg:#dcf0e3; --bull:#1d7a44; --bear-bg:#f6dedc; --bear:#b4302a; --neu-bg:#e6eae8; --neu:#6c7a75;
}}
@media (prefers-color-scheme: dark) {{
  :root:where(:not([data-theme="light"])) .viz-root {{
    color-scheme: dark;
    --surface-1:#1a1a19; --page:#0d0d0d; --ink-1:#ffffff; --ink-2:#c3c2b7;
    --muted:#898781; --grid:#2c2c2a; --axis:#383835;
    --series-1:#4fb8a8; --series-2:#e9962f;
    --good-text:#0ca30c; --crit:#f08078; --ring:rgba(255,255,255,0.10);
    --pass-bg:#10290f; --fail-bg:#3a1414; --bull-bg:#193426; --bull:#5fcb86; --bear-bg:#3e1f1d; --bear:#f08078; --neu-bg:#26302d; --neu:#9aa8a3;
  }}
}}
body{{margin:0;background:var(--page);color:var(--ink-1);font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}}
.viz-root{{max-width:640px;margin:0 auto;padding:18px 14px 40px}}
h1{{font-size:19px;margin:0 0 2px}}
.nav{{font-size:13px;margin-bottom:8px}} .nav a{{color:var(--ink-2)}}
.sub{{color:var(--ink-2);font-size:13px;margin-bottom:12px}}
.leader{{font-size:14px;margin:6px 0 10px}}
.gate{{padding:10px 12px;border-radius:10px}} .gate.pass{{background:var(--pass-bg)}} .gate.fail{{background:var(--fail-bg)}}
code{{font-size:12px}}
.dim{{color:var(--muted);font-size:11px}}
.tiles{{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin:12px 0}}
.tile{{background:var(--surface-1);border:1px solid var(--ring);border-radius:12px;padding:12px 14px}}
.tlabel{{color:var(--ink-2);font-size:12px}} .tvalue{{font-size:24px;font-weight:650;font-variant-numeric:tabular-nums}}
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
.asset{{padding:8px 0;border-bottom:1px solid var(--grid)}} .asset:last-child{{border-bottom:0}}
.ahead{{margin-bottom:6px}}
.votes{{display:grid;grid-template-columns:1fr 1fr;gap:4px 14px}}
.vrow{{display:flex;justify-content:space-between;align-items:center;font-size:13px}} .vk{{color:var(--ink-2)}}
.chip{{font-size:11px;letter-spacing:.05em;text-transform:uppercase;padding:1px 8px;border-radius:4px;min-width:56px;text-align:center}}
.chip.b{{background:var(--bull-bg);color:var(--bull)}} .chip.s{{background:var(--bear-bg);color:var(--bear)}} .chip.n{{background:var(--neu-bg);color:var(--neu)}}
.verdict{{margin-top:8px;font-weight:650}} .verdict.buy{{color:var(--bull)}} .verdict.sell{{color:var(--bear)}} .verdict.flat{{color:var(--ink-2)}}
.grid{{stroke:var(--grid);stroke-width:1}} .axis{{stroke:var(--axis);stroke-width:1}}
.tick{{fill:var(--muted);font-size:11px}} .endlbl{{font-size:12px;font-weight:600}}
.linechart{{width:100%;height:auto;display:block}}
.chartwrap{{position:relative}}
.xhair{{stroke:var(--axis);stroke-width:1;stroke-dasharray:3 3}}
.tooltip{{position:absolute;pointer-events:none;background:var(--surface-1);border:1px solid var(--ring);border-radius:8px;padding:6px 10px;font-size:12px;box-shadow:0 2px 8px rgba(0,0,0,.12);white-space:nowrap}}
footer{{color:var(--muted);font-size:12px;margin-top:22px}}
</style></head>
<body><div class="viz-root">
<div class="nav"><a href="index.html">← Arena scoreboard</a> · <a href="glider.html">GLIDER</a> · <a href="steward.html">STEWARD</a> · <a href="talon.html">TALON</a></div>
<h1>{esc(name)} — BTC/ETH trend bot on a $500 ledger</h1>
<div class="sub">Updated {updated} ET · Alpaca paper · daily signal, 4-hour exits · {float(R["risk_pct"]) * 100:.0f}% risk per trade</div>
{standing}
<div class="tiles">{tiles}</div>
<div class="panel">{chart}</div>
<h2>Signal board</h2>
<div class="panel">{vote_html}{vote_note}</div>
<h2>Open positions</h2>
<div class="panel">{positions_html}</div>
<h2>Closed trades</h2>
<div class="panel">{trades_html}</div>
<h2>Latest cycle</h2>
<div class="panel">{session}</div>
<h2>Schedule</h2>
<div class="panel">{sched}</div>
<footer>vote {S["vote_min"]} of 6 to enter · stop {R["stop_atr"]}×ATR · chandelier {R["trail_atr"]}×ATR, breakeven at +1R, ⅓ off at +{R["partial_R"]:.0f}R ·
cash floor {float(L["cash_floor"]) * 100:.0f}% · sleeves BTC {float(cfg["universe"]["sleeve"]["BTC/USD"]) * 100:.0f}% / ETH {float(cfg["universe"]["sleeve"]["ETH/USD"]) * 100:.0f}% ·
kill switch −{float(R["kill_dd"]) * 100:.0f}% · backtest 2021–26: +21.9%, max DD −21.7% ·
<a href="https://github.com/ccsi9401/bot-arena/blob/main/TWINCOIN.md">manual</a></footer>
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
             `$${{p[1].toLocaleString(undefined,{{maximumFractionDigits:2}})}}</div>` : '';
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
  svg.addEventListener('mouseleave', () => {{ tip.style.display = 'none'; xh.style.display = 'none'; }});
}});
</script></body></html>"""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--offline", action="store_true", help="skip the bars fetch (vote from state, no benchmark)")
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)
    cfg = yaml.safe_load((ROOT / "config" / "twincoin.yaml").read_text(encoding="utf-8"))
    armed = load_json(ROOT / cfg["paths"]["armed"], {})
    state = load_json(ROOT / cfg["paths"]["state"], {})
    journal = load_journal(ROOT / cfg["paths"]["journal_dir"])
    trades = load_trades(ROOT / cfg["paths"]["trades_csv"])
    live = {} if args.offline else fetch_live(cfg)
    page = render(cfg, armed, state, journal, trades, live, datetime.now(timezone.utc))
    out = Path(args.out) if args.out else OUT
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page, encoding="utf-8")
    print(f"wrote {out} ({len(page)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
