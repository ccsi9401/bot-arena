# Pre-launch backtest validation

## SCALPEL 6mo hourly

- n_trades: 184
- total_return_pct: -6.37
- max_drawdown_pct: -8.53
- sharpe_daily_ann: -2.2
- win_rate_pct: 43.5
- profit_factor: 0.74
- avg_r: -0.027
- expectancy_per_trade: -20.04
- **GATE: FAILED** {'min_30_trades': True, 'positive_expectancy': False, 'max_dd_under_15pct': np.True_}

## GLIDER 3y daily

- n_trades: 280
- total_return_pct: 15.13
- max_drawdown_pct: -15.03
- sharpe_daily_ann: 0.51
- win_rate_pct: 48.2
- profit_factor: 1.11
- avg_r: 0.06
- expectancy_per_trade: 23.45
- **GATE: FAILED** {'min_30_trades': True, 'positive_expectancy': np.True_, 'max_dd_under_15pct': np.False_}
