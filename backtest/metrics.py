"""Backtest metrics + the pre-launch validation gate from DESIGN.md."""
from __future__ import annotations

import numpy as np
import pandas as pd


def summarize(equity_curve: pd.Series, trades: list[dict], label: str,
              bench_curve: pd.Series | None = None, dd_margin_pct: float = 5.0) -> dict:
    """bench_curve (optional): benchmark price series over the same days. When given,
    the gate becomes benchmark-relative (see gate()) - used by GLIDER since its core
    sleeve makes it fully invested, so an absolute 15% DD cap is meaningless."""
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
    if bench_curve is not None:
        b = bench_curve.reindex(eq.index).ffill().dropna()
        if len(b) > 1:
            out["bench_total_return_pct"] = round((b.iloc[-1] / b.iloc[0] - 1) * 100, 2)
            out["bench_max_drawdown_pct"] = round((b / b.cummax() - 1).min() * 100, 2)
            out["excess_return_pct"] = round(out["total_return_pct"] - out["bench_total_return_pct"], 2)
    out["gate"] = gate(out, dd_margin_pct)
    return out


def gate(s: dict, dd_margin_pct: float = 5.0) -> dict:
    """DESIGN.md pre-launch gate: expectancy>0 on >=30 trades, plus a drawdown test.

    Absolute form (no benchmark in the summary): backtest DD < 15%.
    Benchmark-relative form (summary carries bench_*): |DD| <= |benchmark DD| + margin,
    AND total return >= the benchmark's over the same window - the point of a fully
    invested core-satellite book is to beat the index, not to be smoother than it."""
    checks = {
        "min_30_trades": (s["n_trades"] or 0) >= 30,
        "positive_expectancy": (s["expectancy_per_trade"] or 0) > 0,
    }
    if "bench_max_drawdown_pct" in s:
        checks["max_dd_within_benchmark"] = bool(
            abs(s["max_drawdown_pct"] or 100) <= abs(s["bench_max_drawdown_pct"]) + dd_margin_pct)
        checks["return_ge_benchmark"] = bool((s["total_return_pct"] or -1e9) >= s["bench_total_return_pct"])
    else:
        checks["max_dd_under_15pct"] = bool(abs(s["max_drawdown_pct"] or 100) < 15)
    return {"checks": checks, "passed": all(checks.values())}
