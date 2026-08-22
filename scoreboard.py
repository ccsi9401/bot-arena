#!/usr/bin/env python3
"""Nightly scorekeeper for the AI-vs-AI matchup defined in config/competition.yaml.

Managed competitors (our bots) are read via their full config; external competitors
(the ChatGPT bots) are scored READ-ONLY — this script only ever calls account() and
positions() on them, never an order endpoint. Writes reports/scoreboard.{json,md}.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.common import ROOT, State, load_config, now_et
from core.broker import make_broker, AlpacaBroker

REPORTS = ROOT / "reports"
REPORTS.mkdir(exist_ok=True)


def competitor_stats(c: dict) -> dict:
    if c["kind"] == "managed":
        cfg = load_config(c["bot"])
        broker = make_broker(cfg)
        start = cfg["starting_equity"]
        state = State(c["bot"])
        kill = state.kill_switch_tripped()
    else:  # external — read-only scoring
        broker = AlpacaBroker(c["env_prefix"])
        start = c.get("starting_equity", 50000)
        state = State(c["slug"])          # equity-curve audit copy only
        kill = None

    acct = broker.account()
    state.append_equity_point(acct["equity"], acct["cash"], note="nightly scoreboard")
    by_day: dict[str, float] = {}
    for pt in state.read("equity_curve", []):
        by_day[pt["ts_et"][:10]] = pt["equity"]
    eqs = list(by_day.values())
    rets = np.diff(eqs) / np.array(eqs[:-1]) if len(eqs) > 1 else np.array([])
    peak = np.maximum.accumulate(eqs) if eqs else [1]
    max_dd = float(((np.array(eqs) - peak) / peak).min() * 100) if len(eqs) > 1 else 0.0

    return {
        "slug": c["slug"],
        "label": c["label"],
        "equity": acct["equity"],
        "cash": acct["cash"],
        "total_return_pct": round((acct["equity"] / start - 1) * 100, 2),
        "day_pl_pct": round((acct["equity"] / acct["last_equity"] - 1) * 100, 2)
        if acct["last_equity"] else 0.0,
        "max_drawdown_pct": round(max_dd, 2),
        "sharpe_daily_ann": round(float(rets.mean() / rets.std() * np.sqrt(252)), 2)
        if len(rets) > 2 and rets.std() > 0 else None,
        "trading_days": len(by_day),
        "open_positions": [p["symbol"] for p in broker.positions()],
        "kill_switch": kill,
    }


def main() -> int:
    comp = yaml.safe_load((ROOT / "config" / "competition.yaml").read_text(encoding="utf-8"))
    rows = [competitor_stats(c) for c in comp["competitors"]]
    leader = max(rows, key=lambda r: r["total_return_pct"])
    board = {"round": comp["round"], "title": comp["title"],
             "start_date": comp.get("start_date"),
             "asof_et": now_et().isoformat(), "competitors": rows,
             "leader": leader["label"]}
    (REPORTS / "scoreboard.json").write_text(json.dumps(board, indent=2), encoding="utf-8")

    md = [f"# {comp['title']}",
          f"\n_As of {board['asof_et'][:16]} ET"
          + (f" · started {comp['start_date']}" if comp.get("start_date") else "") + "_",
          f"\n**Leader: {board['leader']}**\n",
          "| | " + " | ".join(r["label"] for r in rows) + " |",
          "|---|" + "---|" * len(rows)]
    for key, label in [("equity", "Equity"), ("total_return_pct", "Total return %"),
                       ("day_pl_pct", "Today %"), ("max_drawdown_pct", "Max DD %"),
                       ("sharpe_daily_ann", "Sharpe (ann.)"),
                       ("trading_days", "Days scored"), ("open_positions", "Open"),
                       ("kill_switch", "Kill switch")]:
        md.append(f"| {label} | " + " | ".join(str(r[key]) for r in rows) + " |")
    (REPORTS / "scoreboard.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print("\n".join(md))
    return 0


if __name__ == "__main__":
    sys.exit(main())
