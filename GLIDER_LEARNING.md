# GLIDER — self-learning swing bot

GLIDER is the daily trend-pullback bot (`bots/swing/analyzer.py`). This document covers
the learning layer added Aug 2026 and how to bring the bot live on a $5k paper account.

## What "self-learning" means here (and what it doesn't)

Two feedback loops, both **gated**, both **journaled**, both **bounded**:

| loop | cadence | learns from | can change | guardrail |
|---|---|---|---|---|
| **Learner** `backtest/glider_learn.py` | monthly (1st) | 5y of daily history, walk-forward | the learnable strategy knobs in `config/glider.yaml` | must beat incumbent beyond a bootstrap noise floor, in ≥2/3 of yearly folds, AND on an untouched holdout year; ≤1 change per 28 days |
| **Reflection** `glider_reflect.py` | weekly (Sat) | GLIDER's real Alpaca fills | `risk.risk_per_trade_pct` only (base or half) | does nothing before 30 closed live trades; acts only when live mean return is below the 5th percentile of the backtest at that sample size |

It does **not** rewrite the strategy logic, add indicators, or react to the last five
trades. The five-trade "reflection" pattern in generic agent templates is exactly the
noise-chasing STEWARD's research showed to be worthless (±0.41 Sharpe noise floor).

Freeze everything with `learning.enabled: false` (e.g. during a competition round).

## Learnable knobs and the search grid

`regime_filter` {spy_above_200sma, markov2} · `pullback_rsi2_max` {5,10,15} ·
`max_pct_below_52wk_high` {10,15,25} · `stop_atr_mult` {1.5,2,2.5,3} ·
`max_hold_days` {10,15,25} · exit ∈ {target 1.5R, 2R, 3R, trail 2.5×ATR,
trail 3.5×ATR} — 1080 combos; `learning.max_candidates` random-samples them per run
(seed = year-month, so a run is reproducible; 400 of 1080 ≈ 37% coverage per run,
rotating monthly).

**New: `exit_mode: trail`.** A chandelier stop (`close − trail_atr_mult × ATR14`, ratchets
up only) replaces the fixed take-profit; the bracket's TP leg is parked at
`trail_target_r_mult` (8R) so Alpaca still gets a valid bracket. This is the structural
candidate most likely to help — a trend strategy capping winners at 2R is the leading
suspect for the weak original backtest (+14.9% / 3y, Sharpe 0.53).

## How a learner run decides

1. Fetch history, precompute features once (`engine_glider.precompute`).
2. Run the **incumbent** config. Block-bootstrap its daily returns → Sharpe std = *noise*.
3. Split the window: the last `holdout_days` (252) are the **holdout** and are never used
   for ranking. Calendar years inside the remaining **selection** window are the folds.
4. Run every candidate. Eligible only if all four hold:
   - gate: ≥30 trades, expectancy > 0, max DD < 15% (full window)
   - selection Sharpe ≥ incumbent + `noise_floor_sigmas` × noise
   - beats incumbent's yearly Sharpe in ≥ `min_fold_win_frac` of folds
   - holdout: expectancy > 0 and Sharpe ≥ incumbent's holdout Sharpe
5. Highest selection-Sharpe eligible candidate wins → its knobs are written into
   `config/glider.yaml` (comments preserved), `reports/glider_learn/reference_trades.json`
   is refreshed for reflection, and `state/glider/learn_history.json` records
   before/after with the metrics. No eligible candidate → nothing changes, report says why.

Reports: `reports/glider_learn/<date>.md` (top-15 table every run).

## Bringing GLIDER live — checklist

1. **Alpaca**: create a new paper account, set starting balance **$5,000**, copy keys →
   Actions Secrets `GLIDER_API_KEY` / `GLIDER_API_SECRET`.
2. **Land the files** (git):
   - replace `config/glider.yaml`, `bots/swing/analyzer.py`, `backtest/engine_glider.py`
   - add `backtest/glider_learn.py`, `glider_reflect.py`, `tests/test_glider_learning.py`,
     `GLIDER_LEARNING.md`
   - add `.github/workflows/glider.yml`, `glider-learn.yml`, `glider-reflect.yml`
   - patch `core/broker.py` per `core/broker_patch.md` (adds `filled_orders()`; nothing
     existing changes)
3. **Gate**: dispatch `backtest.yml`. The engine now sizes at `starting_equity` ($5k), so
   expect slightly different numbers from the $50k gate — `qty` rounds to 0 more often on
   high-priced names. Confirm GLIDER still PASSES.
4. **First learner run**: dispatch `glider-learn` with `dry_run = true` and read the
   report. If the winner looks sane, dispatch again with `dry_run = false`.
5. **Dry-run cycle**: Monday during market hours, dispatch `glider-cycle` with
   `dry_run = true`; check `journal/glider_*/validation.json` for rejections (expect a few
   "qty rounds to 0" on expensive stocks at $5k — that's fine).
6. **Go live**: dispatch again with `dry_run = false`, or just let the 15:30 ET schedule
   take it. Add a cron-job.org bell-ringer at **3:30 pm ET Mon–Fri** → `workflow_dispatch`
   on `glider.yml`, same as SCALPEL/STEWARD, since GitHub's cron drops runs and the YAML
   cron is UTC (it will drift an hour at the Nov 1 DST change without the bell-ringer).
7. **Health check**: `state/glider/reflection.json` each Saturday tells you how many
   closed live trades exist and, once ≥30, where live sits inside the backtest distribution.

## Markov 2.0 regime gate (optional, shipped dark 2026-08-29)

`strategy.regime_filter: markov2` swaps the 200SMA gate for a Markov 2.0 signal
(`core/markov2.py`): SPY days label BULL/BEAR/SIDEWAYS when the 20-day return
beats ±1.1× its trailing-1y vol band (shifted 1 bar, no lookahead); a
**stride-sampled** (non-overlapping, honest) transition matrix gives
P(bull next) − P(bear next) from today's state; entries allowed when the signal
> `markov2_min_signal` (default 0.0). Live, the scanner fetches ~5.6y of SPY for
the matrix; in the engine the signal is walk-forward per day. While the matrix is
immature (< 24 stride samples) both paths **fall back to the 200SMA gate**.

**Why it ships dark:** 10y walk-forward A/B (2026-08-29, real data, 5 bps slip)
— 200SMA gate +63.3% / DD −24.7% / Sharpe 0.45 vs markov2 +49.8% / DD −34.2% /
Sharpe 0.43. Markov2 wins only the recent 5y slice (+19.3% vs +15.9%) with worse
DD — recent-window shine, not evidence. It stays off by default, but since
2026-08-29 `regime_filter` IS a learner grid dimension: the monthly learner may
promote markov2 only if a candidate clears all four bars (gate, noise floor,
fold consistency, holdout) against the incumbent. Inside the learner's 5y window
a markov2 candidate uses the 200SMA fallback until the matrix matures (~first
3y), exactly as live would. Tests: `tests/test_markov2.py`.

## Sizing note at $5k

0.9% risk = $45/trade. A stock with a $9 stop distance gets 5 shares; one with a $50 stop
distance gets 0 and is rejected. That's the validator working as designed; the learner
backtests at the same $5k so its choices already account for it.
