# Pre-launch backtest validation

## SCALPEL 6mo hourly

- n_trades: 282
- total_return_pct: -8.88
- max_drawdown_pct: -9.88
- sharpe_daily_ann: -2.29
- win_rate_pct: 44.3
- profit_factor: 0.75
- avg_r: -0.033
- expectancy_per_trade: -17.51
- **GATE: FAILED** {'min_30_trades': True, 'positive_expectancy': False, 'max_dd_under_15pct': np.True_}

## GLIDER 3y daily

- n_trades: 275
- total_return_pct: 20.01
- max_drawdown_pct: -18.42
- sharpe_daily_ann: 0.55
- win_rate_pct: 47.6
- profit_factor: 1.12
- avg_r: 0.042
- expectancy_per_trade: 30.74
- **GATE: FAILED** {'min_30_trades': True, 'positive_expectancy': np.True_, 'max_dd_under_15pct': np.False_}
