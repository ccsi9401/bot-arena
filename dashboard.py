#!/usr/bin/env python3
"""Generates reports/dashboard.html — a self-contained cockpit showing, for the
latest run: the SCANNER snapshot (what the market looked like), the ANALYZER's
intents and reasoning, the VALIDATOR's verdicts, execution acks, and the
competition equity curves. Static HTML, no server; regenerated each cycle and
by the nightly scoreboard, committed to the repo.
"""
from __future__ import annotations

import html
import json
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from core.common import ROOT, now_et  # noqa: E402

OUT = ROOT / "reports" / "dashboard.html"


# ---------------------------------------------------------------- data loading
def latest_run(bot: str) -> dict | None:
    runs = sorted(ROOT.glob(f"journal/{bot}_*"), reverse=True)
    for rdir in runs:
        out = {"run_id": rdir.name}
        for stage in ("meta", "scan", "analysis", "validation", "execution",
                      "skipped", "error"):
            f = rdir / f"{stage}.json"
            if f.exists():
                try:
                    out[stage] = json.loads(f.read_text(encoding="utf-8"))
                except Exception:
                    pass
        if "scan" in out or "skipped" in out or "error" in out:
            return out
    return None


def equity_series(slug: str) -> list[tuple[str, float]]:
    f = ROOT / "state" / slug / "equity_curve.json"
    if not f.exists():
        return []
    by_day: dict[str, float] = {}
    for pt in json.loads(f.read_text(encoding="utf-8")):
        by_day[pt["ts_et"][:10]] = pt["equity"]
    return sorted(by_day.items())


# ---------------------------------------------------------------- svg helpers
def line_chart(series: list[dict], w=860, h=280, pad=58) -> str:
    """series: [{label, color, points:[(date, value)]}] -> svg + hover layer."""
    pts_all = [v for s in series for _, v in s["points"]]
    dates = sorted({d for s in series for d, _ in s["points"]})
    if not pts_all or len(dates) < 2:
        return ('<div class="empty">Equity curves appear after the first '
                'scored trading day.</div>')
    lo, hi = min(pts_all), max(pts_all)
    if hi - lo < 1e-9:
        lo, hi = lo * 0.999, hi * 1.001
    lo -= (hi - lo) * 0.08
    hi += (hi - lo) * 0.08
    xi = {d: i for i, d in enumerate(dates)}
    X = lambda d: pad + (w - pad - 84) * xi[d] / max(1, len(dates) - 1)
    Y = lambda v: h - 30 - (h - 30 - 14) * (v - lo) / (hi - lo)

    grid, labels = [], []
    for k in range(5):
        v = lo + (hi - lo) * k / 4
        y = Y(v)
        grid.append(f'<line x1="{pad}" y1="{y:.1f}" x2="{w-84}" y2="{y:.1f}" class="grid"/>')
        labels.append(f'<text x="{pad-6}" y="{y+4:.1f}" class="tick" text-anchor="end">'
                      f'${v:,.0f}</text>')
    step = max(1, len(dates) // 6)
    for d in dates[::step]:
        labels.append(f'<text x="{X(d):.1f}" y="{h-10}" class="tick" text-anchor="middle">'
                      f'{d[5:]}</text>')

    paths, endlabels, dots_js = [], [], []
    for s in series:
        pts = [(X(d), Y(v)) for d, v in s["points"]]
        dstr = "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in pts)
        paths.append(f'<path d="{dstr}" fill="none" stroke="{s["color"]}" '
                     f'stroke-width="2" stroke-linejoin="round"/>')
        ex, ey = pts[-1]
        endlabels.append(f'<text x="{ex+4:.1f}" y="{ey+4:.1f}" class="endlbl" '
                         f'fill="{s["color"]}">{html.escape(s.get("short", s["label"]))}</text>')
        dots_js.append({"label": s["label"], "color": s["color"],
                        "points": [[d, v, round(X(d), 1), round(Y(v), 1)]
                                   for d, v in s["points"]]})

    return f'''
<div class="chartwrap" data-series='{html.escape(json.dumps(dots_js))}'>
<svg viewBox="0 0 {w} {h}" class="linechart" role="img"
     aria-label="Equity curves">
  {"".join(grid)}
  <line x1="{pad}" y1="{h-30}" x2="{w-84}" y2="{h-30}" class="axis"/>
  {"".join(labels)}
  {"".join(paths)}
  {"".join(endlabels)}
  <line class="xhair" x1="0" y1="14" x2="0" y2="{h-30}" style="display:none"/>
</svg>
<div class="tooltip" style="display:none"></div>
</div>'''


# ---------------------------------------------------------------- html blocks
def chip(ok: bool, label: str) -> str:
    cls, icon = ("good", "✓") if ok else ("crit", "✕")
    return f'<span class="chip {cls}">{icon} {html.escape(label)}</span>'


def scanner_table(scan: dict, mode: str, limit=20) -> str:
    syms = scan.get("symbols", {})
    rows = []
    if mode == "intraday":
        ranked = sorted(
            ((s, f) for s, f in syms.items() if f.get("session")),
            key=lambda kv: kv[1]["session"].get("rs_percentile") or 0, reverse=True)
        head = ("<th>Sym</th><th>Last</th><th>RS pct</th><th>vs OR-high</th>"
                "<th>vs VWAP</th><th>Vol pace</th><th>ATR14</th>")
        for s, f in ranked[:limit]:
            ses = f["session"]
            rs = ses.get("rs_percentile") or 0
            bar = (f'<div class="rsbar"><i style="width:{rs*100:.0f}%"></i></div>'
                   f'<span class="num">{rs:.2f}</span>')
            or_ok = ses["last"] > ses["or_high"]
            vw_ok = ses["last"] > ses["vwap"]
            rows.append(
                f"<tr><td class='sym'>{s}</td><td class='num'>{ses['last']:,.2f}</td>"
                f"<td>{bar}</td>"
                f"<td>{chip(or_ok, 'above' if or_ok else 'below')}</td>"
                f"<td>{chip(vw_ok, 'above' if vw_ok else 'below')}</td>"
                f"<td class='num'>{ses['volume_pace']:.2f}×</td>"
                f"<td class='num'>{f['atr14']:.2f}</td></tr>")
    else:
        ranked = sorted(syms.items(),
                        key=lambda kv: (kv[1]["close"] - kv[1]["sma200"]) /
                        max(kv[1]["sma200"], 1e-9), reverse=True)
        head = ("<th>Sym</th><th>Close</th><th>Uptrend</th><th>% off 52wk hi</th>"
                "<th>RSI(2)</th><th>ATR14</th>")
        for s, f in ranked[:limit]:
            up = f["sma50"] > f["sma200"] and f["close"] > f["sma200"]
            rows.append(
                f"<tr><td class='sym'>{s}</td><td class='num'>{f['close']:,.2f}</td>"
                f"<td>{chip(up, 'yes' if up else 'no')}</td>"
                f"<td class='num'>{f['pct_below_52wk_high']:.1f}%</td>"
                f"<td class='num'>{f['rsi2']:.1f}</td>"
                f"<td class='num'>{f['atr14']:.2f}</td></tr>")
    return (f"<table class='tbl'><thead><tr>{head}</tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table>")


def analyzer_block(analysis: dict) -> str:
    intents = analysis.get("intents", [])
    if not intents:
        notes = "; ".join(analysis.get("notes", [])) or "no setups passed the filters"
        return f'<div class="empty">No trade intents this cycle — {html.escape(notes)}.</div>'
    cards = []
    for it in intents:
        if it["action"] == "buy":
            checks = "".join(chip(v, k.replace("_", " "))
                             for k, v in it.get("checks", {}).items())
            cards.append(f'''<div class="card">
  <div class="cardhead"><span class="sym">{it["symbol"]}</span>
    <span class="act buy">BUY</span></div>
  <div class="lvls num">entry ≤ {it["entry_limit"]:,.2f} · stop {it["stop"]:,.2f}
    · target {it["target"]:,.2f}</div>
  <div class="chips">{checks}</div>
  <p class="why">{html.escape(it.get("reasoning", ""))}</p></div>''')
        else:
            cards.append(f'''<div class="card"><div class="cardhead">
  <span class="sym">{it.get("symbol", "ALL")}</span>
  <span class="act mgmt">{it["action"].replace("_", " ").upper()}</span></div>
  <p class="why">{html.escape(it.get("reasoning", ""))}</p></div>''')
    return f'<div class="cards">{"".join(cards)}</div>'


def validator_block(validation: dict) -> str:
    if not validation:
        return '<div class="empty">No validation record for this run.</div>'
    parts = []
    halts = validation.get("halts", [])
    if halts:
        parts.append('<div class="halts">' + " ".join(
            f'<span class="chip crit">⚠ {html.escape(h)}</span>' for h in halts) + "</div>")
    rows = []
    for o in validation.get("approved", []):
        what = (f"{o.get('qty','')} sh, risk ${o.get('risk_dollars',0):,.0f}"
                if o["action"] == "buy" else o["action"].replace("_", " "))
        rows.append(f"<tr><td class='sym'>{o.get('symbol','ALL')}</td>"
                    f"<td>{chip(True, 'approved')}</td><td>{html.escape(str(what))}</td></tr>")
    for o in validation.get("rejected", []):
        rows.append(f"<tr><td class='sym'>{o.get('symbol','?')}</td>"
                    f"<td>{chip(False, 'rejected')}</td>"
                    f"<td>{html.escape(o.get('reject_reason',''))}</td></tr>")
    if rows:
        parts.append("<table class='tbl'><thead><tr><th>Sym</th><th>Verdict</th>"
                     "<th>Detail</th></tr></thead><tbody>" + "".join(rows) +
                     "</tbody></table>")
    else:
        parts.append('<div class="empty">Nothing to validate this cycle.</div>')
    return "".join(parts)


def tiles(comp: dict, curves: dict) -> str:
    out = []
    palette = {0: "var(--series-1)", 1: "var(--series-2)"}
    for i, c in enumerate(comp["competitors"]):
        pts = curves.get(c["slug"], [])
        eq = pts[-1][1] if pts else None
        start = c.get("starting_equity", 50000)
        ret = (eq / start - 1) * 100 if eq else None
        delta = ""
        if ret is not None:
            up = ret >= 0
            delta = (f'<span class="delta {"up" if up else "down"}">'
                     f'{"▲" if up else "▼"} {ret:+.2f}%</span>')
        out.append(f'''<div class="tile" style="border-top:3px solid {palette[i%2]}">
  <div class="tlabel">{html.escape(c["label"])}</div>
  <div class="tvalue">{f"${eq:,.0f}" if eq else "—"}</div>{delta}</div>''')
    return f'<div class="tiles">{"".join(out)}</div>'


# ---------------------------------------------------------------- assembly
def build() -> str:
    comp = yaml.safe_load((ROOT / "config" / "competition.yaml").read_text(encoding="utf-8"))
    managed = [c for c in comp["competitors"] if c["kind"] == "managed"]
    bot = managed[0]["bot"] if managed else "scalpel"
    run = latest_run(bot) or {}
    curves = {c["slug"]: equity_series(c["slug"]) for c in comp["competitors"]}
    colors = ["var(--series-1)", "var(--series-2)"]
    chart = line_chart([
        {"label": c["label"], "short": c["label"].split()[0], "color": colors[i % 2], "points": curves[c["slug"]]}
        for i, c in enumerate(comp["competitors"]) if curves[c["slug"]]])

    scan = run.get("scan", {})
    mode = scan.get("mode", "intraday")
    ts = run.get("meta", {}).get("started_et", "")[:16].replace("T", " ")
    status = ""
    if "error" in run:
        status = '<span class="chip crit">✕ last run errored — see journal</span>'
    elif "skipped" in run:
        status = '<span class="chip mgmtc">market closed — run skipped</span>'

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Bot Arena — cockpit</title>
<style>
.viz-root {{
  color-scheme: light;
  --surface-1:#fcfcfb; --page:#f9f9f7; --ink-1:#0b0b0b; --ink-2:#52514e;
  --muted:#898781; --grid:#e1e0d9; --axis:#c3c2b7;
  --series-1:#2a78d6; --series-2:#eb6834;
  --good:#0ca30c; --good-text:#006300; --crit:#d03b3b;
  --ring:rgba(11,11,11,0.10); --seq:#2a78d6;
}}
@media (prefers-color-scheme: dark) {{
  :root:where(:not([data-theme="light"])) .viz-root {{
    color-scheme: dark;
    --surface-1:#1a1a19; --page:#0d0d0d; --ink-1:#ffffff; --ink-2:#c3c2b7;
    --muted:#898781; --grid:#2c2c2a; --axis:#383835;
    --series-1:#3987e5; --series-2:#d95926;
    --good:#0ca30c; --good-text:#0ca30c; --crit:#d03b3b;
    --ring:rgba(255,255,255,0.10); --seq:#3987e5;
  }}
}}
body{{margin:0;background:var(--page);color:var(--ink-1);
  font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}}
.viz-root{{max-width:920px;margin:0 auto;padding:20px 16px 48px}}
h1{{font-size:20px;margin:0 0 2px}} h2{{font-size:15px;margin:26px 0 8px}}
.sub{{color:var(--ink-2);font-size:13px;margin-bottom:14px}}
.panel{{background:var(--surface-1);border:1px solid var(--ring);
  border-radius:10px;padding:14px 16px;margin-bottom:6px;overflow-x:auto}}
.tiles{{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));
  gap:10px;margin:14px 0}}
.tile{{background:var(--surface-1);border:1px solid var(--ring);border-radius:10px;
  padding:12px 14px}}
.tlabel{{color:var(--ink-2);font-size:12px}} .tvalue{{font-size:26px;font-weight:600}}
.delta{{font-size:13px}} .delta.up{{color:var(--good-text)}} .delta.down{{color:var(--crit)}}
.tbl{{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}}
.tbl th{{text-align:left;color:var(--muted);font-weight:500;font-size:12px;
  padding:4px 10px 6px 0;border-bottom:1px solid var(--axis)}}
.tbl td{{padding:6px 10px 6px 0;border-bottom:1px solid var(--grid)}}
.sym{{font-weight:600}} .num{{font-variant-numeric:tabular-nums}}
.rsbar{{display:inline-block;width:64px;height:6px;background:var(--grid);
  border-radius:3px;margin-right:6px;vertical-align:middle}}
.rsbar i{{display:block;height:100%;background:var(--seq);border-radius:3px}}
.chip{{display:inline-block;font-size:12px;padding:1px 8px;border-radius:99px;
  border:1px solid var(--ring);margin:1px 2px}}
.chip.good{{color:var(--good-text)}} .chip.crit{{color:var(--crit)}}
.chip.mgmtc{{color:var(--ink-2)}}
.cards{{display:grid;gap:10px}}
.card{{border:1px solid var(--ring);border-radius:10px;padding:10px 12px}}
.cardhead{{display:flex;justify-content:space-between;align-items:center}}
.act{{font-size:12px;font-weight:600;padding:1px 8px;border-radius:99px}}
.act.buy{{color:var(--good-text);border:1px solid var(--good-text)}}
.act.mgmt{{color:var(--ink-2);border:1px solid var(--ring)}}
.lvls{{color:var(--ink-2);font-size:13px;margin:4px 0}}
.why{{color:var(--ink-2);font-size:13px;margin:6px 0 2px}}
.chips{{margin:4px 0}}
.empty{{color:var(--muted);padding:16px 4px;font-size:13px}}
.halts{{margin-bottom:8px}}
.grid{{stroke:var(--grid);stroke-width:1}} .axis{{stroke:var(--axis);stroke-width:1}}
.tick{{fill:var(--muted);font-size:11px}} .endlbl{{font-size:12px;font-weight:600}}
.linechart{{width:100%;height:auto;display:block}}
.chartwrap{{position:relative}}
.xhair{{stroke:var(--axis);stroke-width:1;stroke-dasharray:3 3}}
.tooltip{{position:absolute;pointer-events:none;background:var(--surface-1);
  border:1px solid var(--ring);border-radius:8px;padding:6px 10px;font-size:12px;
  box-shadow:0 2px 8px rgba(0,0,0,.12);white-space:nowrap}}
footer{{color:var(--muted);font-size:12px;margin-top:28px}}
</style></head>
<body><div class="viz-root">
<h1>{html.escape(comp["title"])}</h1>
<div class="sub">Cockpit generated {now_et():%Y-%m-%d %H:%M} ET ·
  latest run <b>{html.escape(run.get("run_id","—"))}</b> {status}</div>

{tiles(comp, curves)}

<h2>Equity curves</h2>
<div class="panel">{chart}</div>

<h2>1 · Scanner — market snapshot the analyzer saw <span class="sub">({ts} ET)</span></h2>
<div class="panel">{scanner_table(scan, mode) if scan.get("symbols") else
                    '<div class="empty">No scan yet — appears after the first cycle.</div>'}</div>

<h2>2 · Analyzer — trade intents &amp; reasoning</h2>
<div class="panel">{analyzer_block(run.get("analysis", {}))}</div>

<h2>3 · Validator — risk-gate verdicts</h2>
<div class="panel">{validator_block(run.get("validation", {}))}</div>

<h2>4 · Executor — broker acks</h2>
<div class="panel">{exec_block(run.get("execution", {}))}</div>

<footer>scan → analyze → validate → execute · every stage journaled ·
RULES.md governs the round</footer>
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
    const px = data[0].points.find(q => q[0] === best.date) ||
               data.flatMap(s=>s.points).find(q => q[0] === best.date);
    xh.setAttribute('x1', px[2]); xh.setAttribute('x2', px[2]);
    xh.style.display = '';
    tip.innerHTML = `<b>${{best.date}}</b>${{rows}}`;
    tip.style.display = 'block';
    const lx = px[2] * r.width / vb.width;
    tip.style.left = Math.min(lx + 12, r.width - tip.offsetWidth - 4) + 'px';
    tip.style.top = '20px';
  }});
  svg.addEventListener('mouseleave', () => {{
    tip.style.display = 'none'; xh.style.display = 'none';
  }});
}});
</script></body></html>"""


def exec_block(execution: dict) -> str:
    res = (execution or {}).get("results", [])
    if execution.get("dry_run"):
        return '<div class="empty">Dry run — no orders sent.</div>'
    if not res:
        return '<div class="empty">No orders this cycle.</div>'
    rows = []
    for r in res:
        ok = bool(r.get("ok"))
        detail = r.get("error", "") if not ok else \
            (f"order {r.get('id','')}" if r.get("id") else "done")
        rows.append(f"<tr><td class='sym'>{html.escape(str(r.get('symbol','ALL')))}</td>"
                    f"<td>{html.escape(r.get('action',''))}</td>"
                    f"<td>{chip(ok, 'placed' if ok else 'failed')}</td>"
                    f"<td>{html.escape(str(detail))}</td></tr>")
    return ("<table class='tbl'><thead><tr><th>Sym</th><th>Action</th><th>Status</th>"
            "<th>Detail</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table>")


def main() -> int:
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(build(), encoding="utf-8")
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
