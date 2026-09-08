"""The TWINCOIN cycle. Mirrors backtest.py bar for bar; the backtest is the reference.

Order of operations, every run (idempotent per closed 4H bar):
  1. find the last CLOSED 4H bar t; if state.last_bar == t, do nothing
  2. sync resting orders (partial limit, backstop stop-limit) that filled since last run
  3. reconcile broker positions against state (adopt orphans, flag vanished)
  4. mark the $500 ledger: basis + (account equity now - account equity at arm)
  5. risk rules on the mark: kill switch, daily / weekly loss, losing streak
  6. floor exits: 4H close below the floor -> market sell now (= "next open" in the backtest)
  7. if t closes a UTC day: vote; ratchet floors for open positions; decide entries
  8. entries: market buy, then rest the backstop stop-limit and the partial limit
  9. record last_bar, snapshot

Fills and P&L in the trade log come from broker fill prices (fees are already inside the
account equity the ledger tracks). Dry-run mode (pulse) performs no broker mutation and
saves no state; it prints what a cycle would do.

Quantity discipline (2026-09-08): Alpaca charges crypto buy fees IN THE BASE ASSET, so a
filled buy of 0.0547 ETH leaves ~0.05456 ETH actually in the account. Sizing sell legs off
the order's filled_qty therefore overshoots the balance by the fee and Alpaca rejects the
order with HTTP 403 / code 40310000. Every sell leg is now sized from what the broker says
is HELD and AVAILABLE, never from the recorded fill quantity, and the protective backstop is
placed BEFORE the partial take-profit so that if anything has to be squeezed out by rounding
it is the profit leg and never the protection.
"""
from __future__ import annotations

import math
import time
import uuid
from datetime import timedelta

import pandas as pd

from twincoin.strategy import indicators_4h, indicators_daily, vote

H4 = timedelta(hours=4)


def iso(t) -> str | None:
    return None if t is None else pd.Timestamp(t).isoformat()


def ts(s) -> pd.Timestamp | None:
    return None if s is None else pd.Timestamp(s)


def round_down(x: float, decimals: int) -> float:
    f = 10 ** decimals
    return math.floor(x * f) / f


def new_state(cfg: dict, acct_equity: float, now) -> dict:
    basis = float(cfg["ledger"]["basis_equity"])
    return {"version": 1, "armed_at": iso(now), "acct_equity_at_arm": float(acct_equity), "basis_equity": basis,
            "hwm": basis, "halt_until": None, "kill_mode_left": 0, "no_entry_until": None, "week_block_until": None,
            "streak": 0, "streak_until": None, "day_key": None, "day_start_eq": basis, "week_key": None, "week_start_eq": basis,
            "last_bar": None, "positions": {}, "last_entry": {}, "cooldown_until": {}, "last_vote": {},
            "stats": {"n": 0, "wins": 0, "pnl": 0.0}, "reconcile_halt": False, "kills": 0}


class Engine:
    def __init__(self, cfg: dict, broker, state: dict, journal, dry_run: bool = False):
        self.cfg, self.b, self.st, self.j, self.dry = cfg, broker, state, journal, dry_run
        self.syms = list(cfg["universe"]["symbols"])
        self.R = cfg["risk"]
        self.exited_this_cycle: set[str] = set()

    # ------------------------------------------------------------------ helpers
    def ledger_equity(self, acct_equity: float) -> float:
        return float(self.st["basis_equity"]) + (float(acct_equity) - float(self.st["acct_equity_at_arm"]))

    def _broker_qty(self, sym: str) -> tuple[float, float] | None:
        """(qty, qty_available) as the broker reports it, or None in dry-run / no position."""
        if self.dry:
            return None
        bp = self.b.positions().get(sym)
        if not bp:
            return None
        q = float(bp["qty"])
        return q, float(bp.get("qty_available", q))

    def _sellable(self, sym: str, want: float, dec: int) -> float:
        """How much of `want` we can actually sell right now.

        Returns `want` when the broker has at least that much free. Otherwise returns the
        available quantity, rounded down. Polls briefly because a just-canceled order can
        take a moment to release its reservation at Alpaca -- without the poll a floor
        ratchet would replace a full backstop with a partial one.
        """
        if self.dry:
            return want
        avail = 0.0
        for attempt in range(4):
            bq = self._broker_qty(sym)
            avail = bq[1] if bq else 0.0
            if round_down(avail, dec) >= want:
                return want
            if attempt < 3:
                time.sleep(0.5)
        return round_down(avail, dec)

    def _submit(self, **kw) -> dict | None:
        notional = kw.get("qty", 0) * (kw.get("limit_price") or kw.get("ref_price") or 0)
        if kw["symbol"] not in self.syms or kw.get("qty", 0) <= 0:
            raise RuntimeError(f"order sanity: bad symbol/qty {kw}")
        if kw["side"] == "buy" and notional > float(self.R["max_order_notional"]):
            raise RuntimeError(f"order sanity: buy notional {notional:.2f} exceeds max_order_notional")
        kw.pop("ref_price", None)
        if self.dry:
            self.j.write("would_submit", **kw)
            return None
        o = self.b.submit(**kw)
        self.j.write("order_submitted", id=o.get("id"), **kw)
        return o

    def _cancel(self, order_id: str | None, why: str) -> None:
        if not order_id:
            return
        if self.dry:
            self.j.write("would_cancel", id=order_id, why=why)
            return
        self.b.cancel_order(order_id)
        self.j.write("order_canceled", id=order_id, why=why)

    def _record_trade(self, sym: str, p: dict, exit_px: float, exit_qty: float, t_out, reason: str) -> None:
        proceeds = p["proceeds"] + exit_qty * exit_px
        pnl = proceeds - p["cost"]
        R = pnl / p["risk_usd"] if p["risk_usd"] else 0.0
        row = {"symbol": sym, "t_in": p["t_in"], "t_out": iso(t_out), "entry": p["entry"], "exit": exit_px, "qty0": p["qty0"],
               "cost": round(p["cost"], 2), "proceeds": round(proceeds, 2), "pnl": round(pnl, 2), "R": round(R, 3),
               "reason": reason, "partial": p["partial_done"],
               "days": (pd.Timestamp(t_out) - pd.Timestamp(p["t_in"])).days}
        self.j.trade(row)
        s = self.st["stats"]
        s["n"] += 1
        s["wins"] += int(pnl > 0)
        s["pnl"] = round(s["pnl"] + pnl, 2)
        if pnl < 0:
            self.st["streak"] += 1
            self.st["cooldown_until"][sym] = iso(pd.Timestamp(t_out) + timedelta(days=int(self.R["cooldown_days"])))
        else:
            self.st["streak"] = 0

    # ------------------------------------------------------------- resting orders
    def _place_resting(self, sym: str, p: dict) -> None:
        """Backstop stop-limit for the bulk, then the partial limit for 1/3 (if not done).

        Protection goes on FIRST. Both legs are sized against the broker's available
        balance, so a fee-shaved position can never leave the stop unplaced.
        """
        dec = int(self.cfg["universe"]["qty_decimals"][sym])
        bq = self._broker_qty(sym)
        held = round_down(min(p["qty"], bq[0]), dec) if bq else round_down(p["qty"], dec)
        if bq and held < round_down(p["qty"], dec):
            self.j.write("qty_clamped", symbol=sym, state_qty=p["qty"], broker_qty=bq[0], used=held)
            p["qty"] = held
        q_partial = round_down(held * float(self.R["partial_frac"]), dec) if not p["partial_done"] else 0.0
        q_back = round_down(held - q_partial, dec)
        if q_back > 0:
            q_back = self._sellable(sym, q_back, dec)
        if q_back > 0:
            o = self._submit(symbol=sym, side="sell", qty=q_back, order_type="stop_limit",
                             stop_price=p["floor"] * float(self.R["backstop_stop"]),
                             limit_price=p["floor"] * float(self.R["backstop_limit"]),
                             client_order_id=f"tc-back-{uuid.uuid4().hex[:10]}")
            p["backstop_order_id"] = o["id"] if o else "dry"
            p["backstop_floor"] = p["floor"]
        else:
            self.j.write("backstop_skipped", symbol=sym, why="no sellable quantity", want=round_down(held - q_partial, dec))
        if q_partial > 0:
            q_partial = self._sellable(sym, q_partial, dec)
        if q_partial > 0:
            o = self._submit(symbol=sym, side="sell", qty=q_partial, order_type="limit", limit_price=p["partial_px"],
                             client_order_id=f"tc-partial-{uuid.uuid4().hex[:10]}")
            p["partial_order_id"] = o["id"] if o else "dry"
            p["partial_qty"] = q_partial
        elif not p["partial_done"]:
            self.j.write("partial_skipped", symbol=sym, why="no sellable quantity after backstop")

    def _replace_backstop(self, sym: str, p: dict, why: str) -> None:
        self._cancel(p.get("backstop_order_id") if p.get("backstop_order_id") != "dry" else None, why)
        p["backstop_order_id"] = None
        dec = int(self.cfg["universe"]["qty_decimals"][sym])
        want = round_down(p["qty"] - (0.0 if p["partial_done"] else p.get("partial_qty", 0.0)), dec)
        if want <= 0:
            return
        q_back = self._sellable(sym, want, dec)
        if q_back <= 0:
            self.j.write("backstop_skipped", symbol=sym, why="no sellable quantity", want=want)
            return
        if q_back < want:
            self.j.write("backstop_clamped", symbol=sym, want=want, used=q_back)
        o = self._submit(symbol=sym, side="sell", qty=q_back, order_type="stop_limit",
                         stop_price=p["floor"] * float(self.R["backstop_stop"]),
                         limit_price=p["floor"] * float(self.R["backstop_limit"]),
                         client_order_id=f"tc-back-{uuid.uuid4().hex[:10]}")
        p["backstop_order_id"] = o["id"] if o else "dry"
        p["backstop_floor"] = p["floor"]

    def sync_orders(self, now) -> None:
        for sym in list(self.st["positions"]):
            p = self.st["positions"][sym]
            # backstop first: if it filled, the trade is over
            bid = p.get("backstop_order_id")
            if bid and bid != "dry":
                o = self.b.get_order(bid)
                if o.get("status") == "filled":
                    px, q = float(o["filled_avg_price"]), float(o["filled_qty"])
                    self.j.write("backstop_filled", symbol=sym, price=px, qty=q)
                    self._cancel(p.get("partial_order_id") if p.get("partial_order_id") != "dry" else None, "backstop filled")
                    self._record_trade(sym, p, px, q, o.get("filled_at") or now, "backstop")
                    del self.st["positions"][sym]
                    continue
                if o.get("status") in ("canceled", "rejected", "expired"):
                    self.j.write("backstop_missing", symbol=sym, status=o.get("status"))
                    p["backstop_order_id"] = None
            pid = p.get("partial_order_id")
            if pid and pid != "dry" and not p["partial_done"]:
                o = self.b.get_order(pid)
                if o.get("status") == "filled":
                    px, q = float(o["filled_avg_price"]), float(o["filled_qty"])
                    p["proceeds"] += q * px
                    p["qty"] = max(0.0, p["qty"] - q)
                    p["partial_done"] = True
                    p["partial_order_id"] = None
                    p["floor"] = max(p["floor"], p["entry"] + p["risk_px"])
                    self.j.write("partial_filled", symbol=sym, price=px, qty=q, floor=p["floor"])
                    if p["floor"] > p.get("backstop_floor", 0):
                        self._replace_backstop(sym, p, "partial filled, floor ratchet")
                elif o.get("status") in ("canceled", "rejected", "expired"):
                    p["partial_order_id"] = None
            if p.get("backstop_order_id") is None and p["qty"] > 0:
                self._replace_backstop(sym, p, "backstop absent")

    # ---------------------------------------------------------------- reconcile
    def reconcile(self, bpos: dict, D: dict, now) -> None:
        for sym in list(self.st["positions"]):
            if sym not in bpos:
                self.j.write("position_vanished", symbol=sym, note="broker shows no position; state dropped; entries halted until reconcile_halt is cleared by hand")
                del self.st["positions"][sym]
                self.st["reconcile_halt"] = True
                continue
            bq = bpos[sym]["qty"]
            if bq < self.st["positions"][sym]["qty"]:
                # state can never claim more than the broker holds, or sell legs get rejected
                self.st["positions"][sym]["qty"] = bq
            if abs(bq - self.st["positions"][sym]["qty"]) / max(bq, 1e-9) > 0.02:
                self.j.write("qty_mismatch", symbol=sym, state_qty=self.st["positions"][sym]["qty"], broker_qty=bq)
                self.st["positions"][sym]["qty"] = bq
        for sym, bp in bpos.items():
            if sym in self.syms and sym not in self.st["positions"] and bp["qty"] > 0:
                atr = float(D[sym]["atr"].iloc[-1])
                entry = bp["avg_entry_price"]
                risk_px = float(self.R["stop_atr"]) * atr
                p = {"qty": bp["qty"], "qty0": bp["qty"], "entry": entry, "risk_px": risk_px, "risk_usd": bp["qty"] * risk_px,
                     "floor": entry - risk_px, "hi": max(entry, bp["current_price"]), "partial_px": entry + float(self.R["partial_R"]) * risk_px,
                     "partial_done": False, "proceeds": 0.0, "cost": bp["qty"] * entry, "t_in": iso(now), "adopted": True,
                     "partial_order_id": None, "backstop_order_id": None}
                self.j.write("orphan_adopted", symbol=sym, qty=bp["qty"], entry=entry, floor=p["floor"])
                if not self.dry:
                    n = self.b.cancel_symbol_orders(sym)
                    if n:
                        self.j.write("orders_canceled", symbol=sym, n=n, why="adopting orphan")
                self.st["positions"][sym] = p
                self._place_resting(sym, p)

    # ------------------------------------------------------------------- exits
    def exit_position(self, sym: str, reason: str, bpos: dict, now) -> None:
        p = self.st["positions"][sym]
        self._cancel(p.get("partial_order_id") if p.get("partial_order_id") != "dry" else None, reason)
        self._cancel(p.get("backstop_order_id") if p.get("backstop_order_id") != "dry" else None, reason)
        qty = bpos.get(sym, {}).get("qty_available", p["qty"]) if not self.dry else p["qty"]
        dec = int(self.cfg["universe"]["qty_decimals"][sym])
        qty = round_down(min(qty, bpos.get(sym, {}).get("qty", qty)) if not self.dry else qty, dec)
        if not self.dry:
            qty = self._sellable(sym, qty, dec)
        if qty <= 0:
            self.j.write("exit_skipped", symbol=sym, why="no qty available", reason=reason)
            del self.st["positions"][sym]
            return
        o = self._submit(symbol=sym, side="sell", qty=qty, order_type="market", ref_price=bpos.get(sym, {}).get("current_price", p["entry"]),
                         client_order_id=f"tc-exit-{uuid.uuid4().hex[:10]}")
        if self.dry:
            self.j.write("would_exit", symbol=sym, reason=reason, qty=qty, floor=p["floor"])
            return
        o = self.b.wait_fill(o["id"])
        if o.get("status") != "filled":
            self.j.write("exit_unfilled", symbol=sym, status=o.get("status"), reason=reason)
            return
        px = float(o["filled_avg_price"])
        self.j.write("exit_filled", symbol=sym, price=px, qty=qty, reason=reason, pnl_est=round(p["proceeds"] + qty * px - p["cost"], 2))
        self._record_trade(sym, p, px, qty, o.get("filled_at") or now, reason)
        del self.st["positions"][sym]
        self.exited_this_cycle.add(sym)

    def flatten_all(self, reason: str, bpos: dict, now) -> None:
        for sym in list(self.st["positions"]):
            self.exit_position(sym, reason, bpos, now)

    # ------------------------------------------------------------------ entries
    def enter(self, sym: str, price: float, atr: float, equity: float, cash: float, now) -> None:
        rp = float(self.R["kill_risk"]) if self.st["kill_mode_left"] > 0 else float(self.R["risk_pct"])
        risk_usd = equity * rp
        risk_px = float(self.R["stop_atr"]) * atr
        qty = risk_usd / risk_px
        notional = qty * price
        cap = min(equity * float(self.cfg["universe"]["sleeve"][sym]), cash - equity * float(self.cfg["ledger"]["cash_floor"]),
                  float(self.R["max_order_notional"]))
        notional = min(notional, cap)
        if notional < float(self.cfg["ledger"]["min_notional"]):
            self.j.write("entry_skipped", symbol=sym, why="notional below minimum", notional=round(notional, 2), cap=round(cap, 2))
            return
        dec = int(self.cfg["universe"]["qty_decimals"][sym])
        qty = round_down(notional / price, dec)
        o = self._submit(symbol=sym, side="buy", qty=qty, order_type="market", ref_price=price, client_order_id=f"tc-entry-{uuid.uuid4().hex[:10]}")
        if self.dry:
            self.j.write("would_enter", symbol=sym, qty=qty, notional=round(qty * price, 2), risk_usd=round(qty * risk_px, 2),
                         stop=round(price - risk_px, 2), partial_at=round(price + float(self.R["partial_R"]) * risk_px, 2))
            return
        o = self.b.wait_fill(o["id"])
        if o.get("status") != "filled":
            self.j.write("entry_unfilled", symbol=sym, status=o.get("status"))
            self._cancel(o.get("id"), "entry not filled in time")
            return
        fill, fq = float(o["filled_avg_price"]), float(o["filled_qty"])
        cost = fq * fill  # what was actually paid; the base-asset fee stays in cost, not in qty
        held = fq
        bq = self._broker_qty(sym)
        if bq and 0 < bq[0] < fq:
            held = round_down(bq[0], dec)
            self.j.write("entry_qty_adjusted", symbol=sym, filled_qty=fq, held_qty=bq[0], used=held,
                         note="broker holds less than filled qty (crypto fee taken in base asset)")
        p = {"qty": held, "qty0": held, "entry": fill, "risk_px": risk_px, "risk_usd": held * risk_px, "floor": fill - risk_px, "hi": fill,
             "partial_px": fill + float(self.R["partial_R"]) * risk_px, "partial_done": False, "proceeds": 0.0, "cost": cost,
             "t_in": iso(o.get("filled_at") or now), "partial_order_id": None, "backstop_order_id": None}
        self.st["positions"][sym] = p
        self.st["last_entry"][sym] = iso(now)
        if self.st["kill_mode_left"] > 0:
            self.st["kill_mode_left"] -= 1
        self.j.write("entry_filled", symbol=sym, price=fill, qty=held, notional=round(cost, 2), risk_usd=round(p["risk_usd"], 2),
                     floor=round(p["floor"], 2), partial_at=round(p["partial_px"], 2))
        self._place_resting(sym, p)

    # ------------------------------------------------------------------- cycle
    def cycle(self, daily: dict, h4: dict, now) -> str:
        st, R = self.st, self.R
        t = min(pd.Timestamp(h4[s].index[-1]) for s in self.syms)
        tclose = t + H4
        if st.get("last_bar") == iso(t):
            self.j.write("noop", bar=iso(t), note="already processed")
            return "noop"
        D = {s: indicators_daily(daily[s], int(self.cfg["signal"]["ema50_slope_bars"]), int(self.cfg["signal"]["obv_lookback"])) for s in self.syms}
        H = {s: indicators_4h(h4[s]) for s in self.syms}

        self.sync_orders(now)
        bpos = self.b.positions()
        self.reconcile(bpos, D, now)
        bpos = self.b.positions() if not self.dry else bpos

        acct_eq = float(self.b.account()["equity"])
        equity = self.ledger_equity(acct_eq)
        pos_value = sum(bpos[s]["market_value"] for s in st["positions"] if s in bpos)
        cash = equity - pos_value

        # risk rules on the mark
        st["hwm"] = max(float(st["hwm"]), equity)
        day_key, week_key = tclose.strftime("%Y-%m-%d"), (tclose - timedelta(days=tclose.weekday())).strftime("%Y-%m-%d")
        if st["day_key"] != day_key:
            st["day_key"], st["day_start_eq"] = day_key, equity
        if st["week_key"] != week_key:
            st["week_key"], st["week_start_eq"] = week_key, equity
        if st["halt_until"] and tclose >= ts(st["halt_until"]):
            st["halt_until"] = None
            self.j.write("halt_lifted", note=f"restart at {R['kill_risk']:.1%} risk for {R['kill_trades']} trades")
        if st["halt_until"] is None and equity <= float(st["hwm"]) * (1 - float(R["kill_dd"])):
            st["kills"] = st.get("kills", 0) + 1
            st["halt_until"] = iso(tclose + timedelta(days=int(R["kill_pause_days"])))
            st["kill_mode_left"] = int(R["kill_trades"])
            self.j.write("KILL_SWITCH", equity=round(equity, 2), hwm=round(float(st["hwm"]), 2), halt_until=st["halt_until"])
            self.flatten_all("kill", bpos, now)
            st["hwm"] = equity
        if equity <= float(st["day_start_eq"]) * (1 - float(R["daily_loss"])) and (not st["no_entry_until"] or ts(st["no_entry_until"]) < tclose):
            st["no_entry_until"] = iso(tclose + timedelta(days=1))
            self.j.write("daily_loss_limit", equity=round(equity, 2), day_start=round(float(st["day_start_eq"]), 2))
        if equity <= float(st["week_start_eq"]) * (1 - float(R["weekly_loss"])) and (not st["week_block_until"] or ts(st["week_block_until"]) < tclose):
            st["week_block_until"] = iso(pd.Timestamp(week_key, tz="UTC") + timedelta(days=7))
            self.j.write("weekly_loss_limit", equity=round(equity, 2), week_start=round(float(st["week_start_eq"]), 2))
        if st["streak"] >= int(R["streak_n"]):
            st["streak_until"] = iso(tclose + timedelta(days=int(R["streak_pause_days"])))
            st["streak"] = 0
            self.j.write("streak_pause", until=st["streak_until"])

        # floor exits on the last closed 4H bar
        for sym in list(st["positions"]):
            if t in H[sym].index and float(H[sym].loc[t, "close"]) < float(st["positions"][sym]["floor"]):
                self.j.write("floor_breached", symbol=sym, close=float(H[sym].loc[t, "close"]), floor=st["positions"][sym]["floor"])
                self.exit_position(sym, "floor", bpos, now)

        # daily close logic
        pending: list[str] = []
        if tclose.hour == 0:
            day = t.floor("D")
            for sym in self.syms:
                d = D[sym]
                if day not in d.index or t not in H[sym].index:
                    self.j.write("daily_bar_missing", symbol=sym, day=iso(day))
                    continue
                r, hr = d.loc[day], H[sym].loc[t]
                if pd.isna(r.ema200) or pd.isna(r.obv20) or pd.isna(r.atr):
                    continue
                b, s_, det = vote(r, hr, tuple(self.cfg["signal"]["rsi_bull"]), float(self.cfg["signal"]["rsi_bear"]))
                st["last_vote"][sym] = {"day": iso(day), "bull": b, "bear": s_, "detail": det, "close": float(r.close), "atr": float(r.atr),
                                        "ema20": float(r.ema20), "ema50": float(r.ema50), "ema200": float(r.ema200), "rsi": float(r.rsi)}
                self.j.write("vote", symbol=sym, day=day.strftime("%Y-%m-%d"), bull=b, bear=s_, **det)
                if sym in st["positions"]:
                    p = st["positions"][sym]
                    old = p["floor"]
                    p["hi"] = max(p["hi"], float(r.close))
                    p["floor"] = max(p["floor"], p["hi"] - float(R["trail_atr"]) * float(r.atr))
                    if float(r.close) >= p["entry"] + p["risk_px"]:
                        p["floor"] = max(p["floor"], p["entry"] * (1 + float(R["breakeven_pad"])))
                    if float(r.close) >= p["entry"] + 2 * p["risk_px"]:
                        p["floor"] = max(p["floor"], p["entry"] + p["risk_px"])
                    if p["floor"] > old:
                        self.j.write("floor_ratchet", symbol=sym, old=round(old, 2), new=round(p["floor"], 2))
                        self._replace_backstop(sym, p, "floor ratchet")
                    continue
                why = self._entry_block(sym, b, s_, r, tclose)
                if why:
                    if b >= int(self.cfg["signal"]["vote_min"]):
                        self.j.write("entry_blocked", symbol=sym, why=why)
                    continue
                pending.append(sym)

        for sym in pending:
            price = float(H[sym].loc[t, "close"])
            atr = float(D[sym].loc[t.floor("D"), "atr"])
            self.enter(sym, price, atr, equity, cash, now)
            if not self.dry and sym in st["positions"]:
                cash -= st["positions"][sym]["cost"]

        st["last_bar"] = iso(t)
        self.j.write("snapshot", bar=iso(t), ledger_equity=round(equity, 2), account_equity=round(acct_eq, 2), cash=round(cash, 2),
                     hwm=round(float(st["hwm"]), 2), positions={k: {"qty": v["qty"], "entry": round(v["entry"], 2), "floor": round(v["floor"], 2)} for k, v in st["positions"].items()},
                     halt_until=st["halt_until"], stats=st["stats"])
        return "ok"

    def _entry_block(self, sym: str, b: int, s_: int, r, tclose) -> str | None:
        st, R = self.st, self.R
        if b < int(self.cfg["signal"]["vote_min"]):
            return "vote below threshold"
        if s_ >= int(self.cfg["signal"]["sell_vote_min"]):
            return "sell vote active"
        if sym in self.exited_this_cycle:
            return "exited this cycle"
        if st.get("reconcile_halt"):
            return "reconcile halt"
        if st["halt_until"]:
            return "kill-switch halt"
        if st["no_entry_until"] and tclose < ts(st["no_entry_until"]):
            return "daily loss limit"
        if st["week_block_until"] and tclose < ts(st["week_block_until"]):
            return "weekly loss limit"
        if st["streak_until"] and tclose < ts(st["streak_until"]):
            return "losing-streak pause"
        if R.get("weekend_filter", True) and tclose.weekday() >= 5:
            return "weekend"
        if float(r.close) <= float(r.ema200):
            return "below 200 EMA"
        if float(r.atr) / float(r.close) > float(self.cfg["universe"]["vol_gate"][sym]):
            return "volatility gate"
        le = st["last_entry"].get(sym)
        if le and (tclose - ts(le)).days < int(R["reentry_days"]):
            return "re-entry window"
        cd = st["cooldown_until"].get(sym)
        if cd and tclose < ts(cd):
            return "post-loss cooldown"
        return None