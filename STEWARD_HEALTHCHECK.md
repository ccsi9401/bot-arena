# STEWARD weekly health check

The procedure for the scheduled Friday-evening check. It lives here, next to the
code, so that changing the strategy and changing the check are the same commit.
The scheduled task itself should be one line pointing at this file — if the two
ever disagree, this file wins.

Read-only. Never place a trade, never commit, never edit the repo.

Repo: `ccsi9401/bot-arena` (public, GitHub connector).

---

## Context you need before reading anything

- STEWARD is a Claude-managed paper portfolio on an Alpaca paper account.
- **Run 2 began 2026-08-24 at $5,000** — rung one of a graduated ladder. Scale up
  on elapsed reliability, never on returns.
- **Run 1 (2026-08-07 → 2026-08-22, $50k) is retired.** It lives in
  `journal/archive/run1/`. Ignore it. Never compare against it.
- **The strategy is INDEX-ONLY since 2026-08-22.** `universe.stocks` is empty.
  There is no six-name momentum sleeve. Risk-on weights are 70% index (SPY/QQQ) /
  20% defensive (IEF/GLD/SHY) / 10% cash. Do not ask which stocks were picked.
- Cadence: weekly rebalance ("cycle") anchored to Friday, plus a nightly pulse
  that only snapshots equity. Benchmarked against a SPY buy-and-hold shadow
  starting from the same $5,000 on the same day.

---

## 1. Did the rebalance happen?

Read `state/steward/cycle_status.json` first. This is the authoritative answer and
it is written on **every** run:

```json
{"due": false, "overdue": false, "week_anchor_et": "2026-09-04",
 "last_cycle_date_et": "2026-09-04", "days_since_anchor": 0}
```

- `overdue: true` → **headline the run.** Say how many days, and what
  `last_cycle_date_et` was. The portfolio is drifting unmanaged.
- `due: true, overdue: false` → normal on the anchor day itself; the slot has not
  arrived yet. Not a fault.
- File missing entirely → the new scheduling code is not deployed. Say so loudly.

Then confirm with the journal: `journal/steward_<YYYYMMDD>_<HHMM>/` for the
current week. Anything under `journal/archive/` is the retired run.

> Why this is check #1: through August 2026 the cycle was chosen by UTC clock hour,
> which gave a 59-minute window per week against cron drift of 3.5–8 hours. Two of
> roughly five cycles ever fired, and a missed one was **completely silent** — the
> pulse still ran, the report still updated, the commit was still green. The
> $5,000 restart sat in 100% cash for five days before anyone noticed.

## 2. Performance

Last entries of `state/steward/equity_curve.json` and `benchmark_curve.json`.
Report equity, the SPY shadow, the gap, and the change over the week. Both start
at $5,000 on the same inception date.

If equity is exactly flat at $5,000 with `cash == equity`, the book was never
built — that is a check-1 failure, not a performance result.

## 3. Cash — the invariant that has broken three times

Compute cash as a percentage of equity from the latest equity-curve entry.
Expect roughly 10%. Then open the cycle's `plan.json`:

- **`"sizing_version": 2`** must be present. Missing → an older planner is
  running. State that loudly.
- **`"projected_cash_weight"` vs `"cash_target"`** — a gap over ~1pp is the real
  signal, more meaningful than raw cash, because `cash_target` legitimately moves.
- **`"cash_drag_sweep"`** — `false` with cash near target is the healthy steady
  state. `true` occasionally is *fine* and means the mechanism is working. `true`
  every week across several cycles means it is not converging: flag it.

Then check `analysis.json` for **`"index_residue_pp"`**:

- `0` is normal.
- Non-zero means the index sleeve did not fill — QQQ (or SPY) is below its own
  200-day, so the 70% sleeve capped out at 40% in one ETF. With
  `index_residue_to: defensive` the remainder goes to the ballast and cash stays
  near 10%. If it is non-zero **and** cash is far above target, the setting has
  been reverted to `cash`; check `config/steward.yaml`.

The three historical failures, so a relapse is recognisable:

| When | Cause | Fix |
|---|---|---|
| Aug 14 2026 | whole-share truncation parked 5.3% in cash | dollar-based sizing (`sizing_version: 2`) |
| Aug 21 2026 | cash stuck at 15.2% — every position under target but inside the 1.5% band, so nothing triggered | portfolio-level cash-drag sweep |
| Aug 29 2026 | index sleeve capped at 40% with one leg below trend → **40% cash in a risk-on regime**, invisible because `cash_target` matched it | `index_residue_to: defensive` |

The dust floor is `min_order_notional_pct` (a fraction of equity), which is what
keeps the sweep working at $5,000.

## 4. Execution

The cycle's `execution.json`. Confirm `"failed": 0`. Quote verbatim any order
with `"ok": false` and its error. Rejected notional (dollar-denominated) orders
are a known failure mode worth calling out specifically. At $5,000 the positions
are small and fractional — a 40% slot is ~$2,000, a defensive third ~$333 — so
fractional quantities are normal, not a symptom.

## 5. What changed

`reports/steward.md` and the cycle's `analysis.json`. Report the regime
(RISK-ON / RISK-OFF) and the sleeve weights — which index ETFs are held and at
what weight, and whether the defensive sleeve is carrying index residue. Compare
against the previous cycle **in the current run only**. Note any `"notes"` or
`"halts"` in the plan.

`"peak_drawdown_pct"` is also in the plan. The kill switch trips at 20% below the
$5,000 inception floor **or** 30% off the high-water mark, whichever comes first.

## 6. Anything broken

- `journal/steward_error_*.json` from the past week.
- Recent commits for failed or missing bot runs. A `steward-bot` commit touching
  only `state/` and `reports/` is a pulse; one touching `journal/` is a cycle.
- `reports/backtest/steward.json` should contain `"planner_driven": true` — the
  gate drives the real planner rather than a copy of it. If that field disappears
  someone has reverted it, and the gate is no longer validating what actually runs.
- The same file's `counterfactual` block reports the other `index_residue_to`
  setting measured over the identical window, and `index_residue_weeks_pct` says
  how often the sleeve failed to fill.

---

## Report format

Lead with a one-line verdict: **healthy** or **needs attention**. Then equity vs
SPY shadow, cash %, sleeve changes. Then problems, with the specific file and
quoted lines. A few sentences plus the key numbers — not a wall of JSON.

If everything is normal, say so briefly rather than padding. **Send a push
notification only when something needs the operator**: an overdue cycle, a failed
order, a cash or residue anomaly, a halt, or an error file. A healthy week does
not warrant interrupting anyone.
