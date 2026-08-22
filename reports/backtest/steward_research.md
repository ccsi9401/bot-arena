# STEWARD research — what the gate cannot tell you

Window: 2016-08-22 to 2026-08-21. SPY buy & hold over the same span: 246.29%.

| variant | question | return | max DD | Sharpe | vs baseline |
|---|---|---|---|---|---|
| `baseline` | what the gate measures | 255.49% | -19.4% | 0.96 | +0.00 pts |
| `index_only` | does stock picking beat the index at the same risk? | 156.65% | -14.91% | 1.08 | -98.84 pts |
| `static_70_20_10` | does ANY of the machinery earn its keep? | 171.72% | -24.41% | 0.92 | -83.77 pts |
| `frozen_universe` | how much is hindsight in the universe? | 219.17% | -19.4% | 0.91 | -36.32 pts |
| `drop_top3` | is the edge broad, or three lucky names? | 191.15% | -15.66% | 0.9 | -64.34 pts |
| `pessimistic_fills` | does the edge survive realistic execution? | 202.52% | -22.01% | 0.84 | -52.97 pts |
| `honest_worst_case` | all of the above at once | 120.62% | -20.19% | 0.72 | -134.87 pts |
| **SPY buy & hold** | the thing to beat | 246.29% | -33.72% | 0.84 | -9.20 pts |

- **frozen_universe**: dropped ['PANW', 'PLTR', 'TSLA', 'UBER'] — joined the index after 2017-10-04, so the strategy could not have been picking them
- **drop_top3**: dropped ['AMD', 'MU', 'NVDA'] — best performers, knowable only after the fact
- **pessimistic_fills**: filled at next open, 25bps each way