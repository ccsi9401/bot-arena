# Pre-launch backtest validation

## SCALPEL 6mo hourly

- n_trades: 180
- total_return_pct: -1.57
- max_drawdown_pct: -4.72
- sharpe_daily_ann: -0.42
- win_rate_pct: 53.9
- profit_factor: 0.94
- avg_r: -0.007
- expectancy_per_trade: -4.37
- **GATE: FAILED** {'min_30_trades': True, 'positive_expectancy': False, 'max_dd_under_15pct': np.True_}

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
