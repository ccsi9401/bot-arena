#!/usr/bin/env python3
"""Pre-launch validation: backtest both bots and apply the DESIGN.md gate.

Runs on the GitHub Actions runner (needs network for yfinance).
Writes reports/backtest/{scalpel,glider}.json + summary.md and exits non-zero
if either bot fails its gate — the launch workflow depends on this passing.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.common import load_config
from backtest import data as bd
from backtest import metrics
from backtest.engine_glider import run as run_glider
from backtest.engine_scalpel import run as run_scalpel

OUT = Path(__file__).resolve().parent.parent / "reports" / "backtest"
OUT.mkdir(parents=True, exist_ok=True)


def main() -> int:
    gcfg = load_config("glider")
    scfg = load_config("scalpel")
    uni = sorted(set(gcfg["universe"]["stocks"] + gcfg["universe"]["etfs"]))

    print("Fetching daily history (3y)...")
    ddata = bd.daily_history(uni, "3y")
    print(f"  {len(ddata)} symbols")
    print("Running GLIDER backtest...")
    g_curve, g_trades = run_glider(ddata, gcfg)
    g = metrics.summarize(g_curve, g_trades, "GLIDER 3y daily")
    (OUT / "glider.json").write_text(json.dumps(
        {"summary": g, "trades": g_trades}, indent=2, default=str))

    print("Fetching hourly history (6mo)...")
    hdata = bd.hourly_history(uni, "6mo")
    print(f"  {len(hdata)} symbols")
    print("Running SCALPEL backtest...")
    s_curve, s_trades = run_scalpel(hdata, scfg)
    s = metrics.summarize(s_curve, s_trades, "SCALPEL 6mo hourly")
    (OUT / "scalpel.json").write_text(json.dumps(
        {"summary": s, "trades": s_trades}, indent=2, default=str))

    md = ["# Pre-launch backtest validation\n"]
    for r in (s, g):
        md.append(f"## {r['label']}\n")
        for k, v in r.items():
            if k not in ("label", "gate"):
                md.append(f"- {k}: {v}")
        md.append(f"- **GATE: {'PASSED' if r['gate']['passed'] else 'FAILED'}** "
                  f"{r['gate']['checks']}\n")
    (OUT / "summary.md").write_text("\n".join(md))
    print("\n".join(md))

    ok = s["gate"]["passed"] and g["gate"]["passed"]
    print("VALIDATION", "PASSED" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
