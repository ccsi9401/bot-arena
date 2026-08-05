# Pre-launch backtest validation

## SCALPEL 6mo hourly

- n_trades: 117
- total_return_pct: 3.48
- max_drawdown_pct: -2.9
- sharpe_daily_ann: 1.21
- win_rate_pct: 58.1
- profit_factor: 1.21
- avg_r: 0.022
- expectancy_per_trade: 14.86
- **GATE: PASSED** {'min_30_trades': True, 'positive_expectancy': True, 'max_dd_under_15pct': np.True_}

## GLIDER 3y daily

- n_trades: 284
- total_return_pct: 14.93
- max_drawdown_pct: -14.64
- sharpe_daily_ann: 0.53
- win_rate_pct: 47.9
- profit_factor: 1.12
- avg_r: 0.056
- expectancy_per_trade: 23.15
- **GATE: PASSED** {'min_30_trades': True, 'positive_expectancy': np.True_, 'max_dd_under_15pct': np.True_}
