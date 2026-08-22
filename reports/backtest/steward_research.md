# STEWARD research — what the gate cannot tell you

Tested window: **2017-10-04 to 2026-08-21** (8.9y, 2232 trading days). Data was fetched from 2016-08-22; the momentum lookback and warmup consume the difference. SPY buy & hold over the tested span: **246.29%**, max drawdown **-33.72%**.

| variant | question | fills | return | max DD | Sharpe | vs baseline |
|---|---|---|---|---|---|---|
| `baseline` | what the gate measures | close +5bps | 255.49% | -19.4% | 0.96 | +0.00 pts |
| `index_only` | does stock picking beat the index at the same risk? | close +5bps | 156.65% | -14.91% | 1.08 | -98.84 pts |
| `static_70_20_10` | does ANY of the machinery earn its keep? | close +5bps | 171.72% | -24.41% | 0.92 | -83.77 pts |
| `index_only_real` | the gate on index funds, at the same cost | next open +25bps | 144.12% | -16.74% | 1.02 | -111.37 pts |
| `static_real` | the do-nothing blend, at the same cost | next open +25bps | 171.16% | -24.26% | 0.91 | -84.33 pts |
| `frozen_universe` | how much is hindsight in the universe? | close +5bps | 219.17% | -19.4% | 0.91 | -36.32 pts |
| `drop_top3` | is the edge broad, or three lucky names? | close +5bps | 191.15% | -15.66% | 0.9 | -64.34 pts |
| `pessimistic_fills` | does the edge survive realistic execution? | next open +25bps | 202.52% | -22.01% | 0.84 | -52.97 pts |
| `bias_corrected` | the closest thing to an unbiased estimate | next open +25bps | 172.07% | -22.02% | 0.79 | -83.42 pts |
| `honest_worst_case` | all of the above at once | next open +25bps | 120.62% | -20.19% | 0.72 | -134.87 pts |
| **SPY buy & hold** | the thing to beat | none (hold) | 246.29% | -33.72% | 0.84 | -9.20 pts |

- **index_only_real**: index_only charged the same execution as bias_corrected — the only honest way to compare a low-turnover variant against a high-turnover one.
- **static_real**: static_70_20_10 charged the same execution as bias_corrected. THIS is the row that says whether any of the machinery earns its keep.
- **frozen_universe**: dropped ['PANW', 'PLTR', 'TSLA', 'UBER'] — joined the index after 2017-10-04, so the strategy could not have been picking them
- **drop_top3**: dropped ['AMD', 'MU', 'NVDA'] — best performers, knowable only after the fact
- **pessimistic_fills**: filled at next open, 25bps each way
- **bias_corrected**: frozen universe AND realistic fills — both are corrections for things that genuinely bias the result, with no stress test stacked on top. This is the row to weigh against SPY.
- **honest_worst_case**: a FLOOR, not an estimate — it stacks the drop_top3 stress test on top of the real corrections, penalising the stock sleeve twice. The true bias-corrected figure sits between this and bias_corrected.

## Regime-gate parameter sweep

The whole risk-reduction result rests on one number: the length of the trend filter. If only 200 works, it is a curve fit that caught one crash. All rows use the index sleeve, realistic fills, and an identical window.

| trend SMA | return | max DD | Sharpe |
|---|---|---|---|
| 100d | 100.69% | -19.96% | 0.84 |
| 125d | 128.39% | -20.88% | 0.98 |
| 150d | 143.08% | -19.43% | 1.04 |
| 175d | 138.53% | -17.39% | 1.02 |
| 200d | 133.75% | -16.74% | 0.98 |
| 225d | 115.98% | -18.6% | 0.9 |
| 250d | 125.92% | -20.13% | 0.93 |
| 300d | 114.03% | -20.62% | 0.82 |

**PLATEAU — the result does not depend on the exact length.** Best Sharpe at 150d (1.04); spread across the band 0.22.