# STEWARD pre-launch validation

## STEWARD 3y weekly

- total_return_pct: 157.13
- cagr_pct: 11.25
- max_drawdown_pct: -15.21
- sharpe_daily_ann: 1.08
- avg_cash_weight_pct: 15.4
- cash_gap_mean_pp: -0.16
- cash_gap_worst_pp: 2.7
- weeks_cash_over_target_1pp: 20
- rebalance_weeks: 448
- index_residue_weeks: 13
- index_residue_weeks_pct: 2.9
- index_residue_mean_pp: 30.0
- index_residue_to: defensive
- window_start: 2017-10-11
- window_end: 2026-08-28
- trading_days: 2232
- rebalance_trades: 377

## SPY buy & hold (same window)

- total_return_pct: 245.4
- cagr_pct: 15.02
- max_drawdown_pct: -33.72
- sharpe_daily_ann: 0.84

## counterfactual — index_residue_to: cash

- total_return_pct: 155.71
- cagr_pct: 11.18
- max_drawdown_pct: -14.91
- sharpe_daily_ann: 1.07
- avg_cash_weight_pct: 16.3
- cash_gap_mean_pp: -0.16
- cash_gap_worst_pp: 2.7
- weeks_cash_over_target_1pp: 20
- rebalance_weeks: 448
- index_residue_weeks: 13
- index_residue_weeks_pct: 2.9
- index_residue_mean_pp: 30.0
- index_residue_to: cash
- would_pass_gate: True
- rebalance_trades: 367
- max_dd_delta_pp: 0.3

**GATE: PASSED** {'positive_return': True, 'max_dd_under_20pct': True, 'sharpe_at_least_0_4': True}
