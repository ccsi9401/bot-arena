"""Circuit breakers and data-integrity guards.

Nothing here generates a signal. All of it decides whether the signals are
allowed to reach the broker. If the feed is stale, everything else in this
package is worthless -- so the staleness check runs before anything else.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import pandas as pd


@dataclass
class RiskVerdict:
    may_enter: bool
    must_flatten: bool
    halted: bool
    reasons: List[str]

    def __bool__(self) -> bool:
        return self.may_enter


def _now() -> datetime:
    return datetime.now(timezone.utc)


def bars_are_fresh(bars: Dict[str, pd.DataFrame], cfg: dict, now: Optional[datetime] = None) -> tuple[bool, List[str]]:
    """A frozen feed that still returns 200 OK is the failure mode that turns a
    working bot into a losing one. Every cycle checks bar age before acting."""
    now = now or _now()
    limit = timedelta(minutes=cfg["risk"]["max_bar_age_minutes"])
    stale = []
    for sym, df in bars.items():
        if df is None or df.empty:
            stale.append(f"{sym}: no bars")
            continue
        last = df.index[-1]
        if last.tzinfo is None:
            last = last.tz_localize("UTC")
        # A daily bar stamped at 00:00 UTC is current all day; measure from the
        # END of the bar it represents.
        age = now - (last + timedelta(days=1))
        if age > limit:
            stale.append(f"{sym}: last bar {last.date()} is {age.total_seconds()/3600:.1f}h past close")
    return (len(stale) == 0), stale


def evaluate(state: dict, equity: float, bars: Dict[str, pd.DataFrame], cfg: dict,
             now: Optional[datetime] = None) -> RiskVerdict:
    now = now or _now()
    reasons: List[str] = []
    may_enter = True
    must_flatten = False
    halted = False

    if state.get("manual_halt"):
        return RiskVerdict(False, False, True, ["manual halt is set in state; clear it to resume"])

    fresh, stale = bars_are_fresh(bars, cfg, now)
    if not fresh:
        # Stale data means we do not TRADE, in either direction. Flattening on
        # bad data is just as wrong as entering on it.
        return RiskVerdict(False, False, True, ["stale data: " + "; ".join(stale[:4])])

    hwm = float(state.get("high_water_equity") or equity)
    if equity > hwm:
        hwm = equity
    state["high_water_equity"] = hwm

    dd = 0.0 if hwm <= 0 else (hwm - equity) / hwm
    if dd >= cfg["risk"]["drawdown_kill_pct"]:
        return RiskVerdict(False, True, True, [
            f"drawdown {dd:.1%} from high-water breached the {cfg['risk']['drawdown_kill_pct']:.0%} kill switch; "
            "flattening and halting until manually re-armed"
        ])

    prev_eq = state.get("equity_at_last_cycle")
    if prev_eq:
        day_move = (equity - float(prev_eq)) / float(prev_eq)
        if day_move <= -cfg["risk"]["daily_loss_halt_pct"]:
            may_enter = False
            reasons.append(f"daily loss {day_move:.1%} exceeded the halt threshold; exits only this cycle")

    fails = int(state.get("consecutive_order_failures") or 0)
    if fails >= cfg["risk"]["max_consecutive_order_failures"]:
        return RiskVerdict(False, False, True, [f"{fails} consecutive order failures; halting for inspection"])

    used = _roundtrips_this_month(state, now)
    if used >= cfg["risk"]["monthly_roundtrip_budget"]:
        may_enter = False
        reasons.append(
            f"turnover budget spent ({used}/{cfg['risk']['monthly_roundtrip_budget']} round trips this month); "
            "exits only. At 50bps a turn, this cap is the difference between a strategy and a fee generator."
        )

    return RiskVerdict(may_enter, must_flatten, halted, reasons)


def _roundtrips_this_month(state: dict, now: datetime) -> int:
    stamp = now.strftime("%Y-%m")
    return int((state.get("roundtrips") or {}).get(stamp, 0))


def record_roundtrip(state: dict, now: Optional[datetime] = None) -> None:
    now = now or _now()
    stamp = now.strftime("%Y-%m")
    rt = state.setdefault("roundtrips", {})
    rt[stamp] = int(rt.get(stamp, 0)) + 1


def reconcile(state: dict, broker_positions: Dict[str, dict]) -> List[str]:
    """Compare what we think we hold against what the broker says. Report the
    gap; never silently trade to close it -- an unexplained position is a bug,
    and trading on top of a bug makes it more expensive, not less."""
    ours = set((state.get("floors") or {}).keys())
    theirs = {s for s, p in broker_positions.items() if abs(float(p.get("qty", 0))) > 0}
    notes = []
    for s in sorted(theirs - ours):
        notes.append(f"UNTRACKED position at broker: {s} (not in PAWL state)")
    for s in sorted(ours - theirs):
        notes.append(f"PHANTOM position in state: {s} (broker shows none) -- clearing from state")
        (state.get("floors") or {}).pop(s, None)
    return notes
