"""Backtest metrics + the pre-launch validation gate from DESIGN.md."""
from __future__ import annotations

import numpy as np
import pandas as pd


def summarize(equity_curve: pd.Series, trades: list[dict], label: str) -> dict:
    eq = equity_curve.dropna()
    rets = eq.pct_change().dropna()
    dd = (eq / eq.cummax() - 1).min()
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = -sum(t["pnl"] for t in losses)
    r_multiples = [t["pnl"] / t["risk"] for t in trades if t.get("risk", 0) > 0]
    out = {
        "label": label,
        "n_trades": len(trades),
        "total_return_pct": round((eq.iloc[-1] / eq.iloc[0] - 1) * 100, 2),
        "max_drawdown_pct": round(dd * 100, 2),
        "sharpe_daily_ann": round(float(rets.mean() / rets.std() * np.sqrt(252)), 2)
        if len(rets) > 2 and rets.std() > 0 else None,
        "win_rate_pct": round(len(wins) / len(trades) * 100, 1) if trades else None,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
        "avg_r": round(float(np.mean(r_multiples)), 3) if r_multiples else None,
        "expectancy_per_trade": round(sum(t["pnl"] for t in trades) / len(trades), 2)
        if trades else None,
    }
    out["gate"] = gate(out)
    return out


def gate(s: dict) -> dict:
    """DESIGN.md pre-launch gate: expectancy>0 on ≥30 trades, backtest DD < 15%."""
    checks = {
        "min_30_trades": (s["n_trades"] or 0) >= 30,
        "positive_expectancy": (s["expectancy_per_trade"] or 0) > 0,
        "max_dd_under_15pct": abs(s["max_drawdown_pct"] or 100) < 15,
    }
    return {"checks": checks, "passed": all(checks.values())}
