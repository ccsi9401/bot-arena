# Bot Arena — Claude vs. ChatGPT, 30-Day Paper Competitions

**Owner:** Everett · **Built:** August 2026 · **Budget:** $0 (Alpaca paper + free IEX data + GitHub free tier)

## The competition

AI vs. AI, matched by timeframe, two bots at a time, all on Alpaca paper at **$50,000**:

- **Round 1 (now):** SCALPEL (Claude day trader) vs Everett's ChatGPT day-trader bot.
- **Round 2 (later):** GLIDER (Claude swing trader) vs the ChatGPT swing challenger.

Each round runs **30 calendar days** from a shared start date. The ChatGPT bots are
run by Everett's own stack; this repo only READS their accounts for nightly scoring
(config/competition.yaml) — it never places orders on them. Claude-side bots trade
**US large-cap stocks + liquid ETFs, long-only**; assumed ground rules are equal
starting capital and the same window, with each AI otherwise playing its own game.

Cockpit: both strategies are also ported to Pine Script (`tradingview/`) so Everett can
watch signals on his TradingView charts and cross-check the backtest with TradingView's
engine. TradingView is display/validation only — its paper account has no API, so
execution stays on Alpaca. An IBKR adapter exists in-tree (`core/broker_ibkr.py`),
dormant; switching a bot is a one-line config change plus OAuth setup.

| | SCALPEL (intraday) | GLIDER (swing) |
|---|---|---|
| Cadence | Hourly cycles, 10:30–15:30 ET | One cycle daily at 15:30 ET |
| Holding period | Minutes–hours, flat by close | 2–15 trading days |
| Strategy | Opening-range breakout + relative-strength momentum | Trend-pullback (buy uptrends on weakness) |
| Risk per trade | 1.5% of equity | 1.5% of equity |
| Max concurrent positions | 3 | 5 |
| Stops | Bracket orders held at broker (1.5×ATR) | Bracket orders held at broker (2×ATR) |

**Scoring (declared up front, no moving goalposts):**

- Primary: total return over the 30 days.
- Reported alongside: max drawdown, Sharpe (daily), win rate, profit factor, avg exposure.
- A bot that trips its kill-switch (see Risk) and stays flat is scored on what it kept.

## Architecture — four stages, strictly separated

Each cycle is a pipeline of four pure stages. Every stage reads only its declared inputs and
writes a JSON artifact. No stage may skip ahead (the executor cannot see raw market data;
the scanner cannot see the account).

```
scan → analyze → validate → execute
```

1. **Scanner** (`core/scanner.py`) — takes the universe + market data, emits a ranked
   candidate snapshot (`journal/<run_id>/scan.json`). Knows nothing about strategy or account.
2. **Analyzer** (`bots/*/analyzer.py`) — takes ONLY the scan snapshot + bot config, emits
   trade intents with entry/stop/target and its reasoning (`analysis.json`). Deterministic:
   same snapshot in, same intents out.
3. **Validator** (`core/validator.py`) — takes intents + live account state, applies every
   risk gate, emits approved/rejected orders with the reason for each rejection
   (`validation.json`). The only stage allowed to say no.
4. **Executor** (`core/executor.py`) — takes ONLY approved orders, places them as bracket
   orders on Alpaca, records broker acks (`execution.json`). No discretion whatsoever.

## Storage & reproducibility

- Everything lives in a private GitHub repo. Each scheduled run: clone → run cycle → commit.
- Every run gets a `run_id` (`{bot}_{YYYYMMDD_HHMM}`); its journal directory holds the four
  stage artifacts plus `meta.json` (config hash, code git SHA, data timestamps).
  **Any trade can be replayed**: re-run the analyzer on the committed scan.json and you must
  get the same intents.
- `state/<bot>/` holds the equity curve (appended daily), open-position ledger, and
  kill-switch status. Alpaca remains the authority on positions; state files are the audit
  copy and are reconciled against the broker at the start of every cycle (drift → halt + alert).
- Config is versioned in-repo; strategy parameters never live in code.

## The strategies

### SCALPEL — intraday momentum (hourly decisions)

Reality check baked into the design: cloud scheduling is hourly, so SCALPEL is a
*session-momentum* bot, not a tick scalper. It compensates by parking protection at the
broker: every entry is a bracket order (stop + target live on Alpaca's servers between cycles).

- 10:30 ET cycle: record each candidate's opening range (first-hour high/low).
- Entry (any cycle 10:30–14:30): price above opening-range high AND above session VWAP AND
  relative strength vs SPY since open in the top quintile of the scan AND volume pacing
  ≥ 1.3× its 20-day average for that time of day.
- Stop 1.5×ATR(14, daily) below entry; target 2R; whichever hits first.
- 15:30 cycle: liquidate everything. No overnight risk, ever.
- Daily circuit breaker: −3% on the day → flatten and stop entering until tomorrow.

### GLIDER — swing trend-pullback (daily decisions)

- Regime gate: no new entries unless SPY > its 200-day SMA.
- Candidate: 50-day SMA > 200-day SMA, price within 15% of 52-week high (established uptrend).
- Trigger: pullback — RSI(2) < 10, or low touches the 20-day EMA while close holds above it.
- Entry at 15:30 ET (near close, using the nearly-complete daily bar). Stop 2×ATR(14) below;
  target 2R; time-stop exit at 15 trading days regardless.
- Trailing: once a position reaches +1R, stop moves to breakeven.

## Risk gates (validator — runs every cycle, both bots)

1. Position sizing: risk per trade = 1.5% of current equity; qty from entry−stop distance.
2. Exposure cap: total open notional ≤ 100% of equity; no margin.
3. Concurrency cap: 3 (SCALPEL) / 5 (GLIDER); one position per symbol per bot.
4. Kill-switch: account equity −15% from starting $50k → bot goes flat and permanently
   halts new entries for the remainder of the competition (reported as its final result).
5. Freshness: scan data older than 20 minutes → no new entries this cycle.
6. Duplicate guard: an intent matching an open order/position is rejected.
7. Sanity: limit prices within 1% of last trade; qty > 0; market open; symbol tradable.

## Validation before execution (pre-launch gate)

Before either bot trades a dollar of paper money, its strategy must pass a historical
backtest over the past 2 years of daily data (GLIDER) / 6 months of intraday data (SCALPEL):

- Expectancy > 0 after simulated slippage (5 bps) on at least 30 trades.
- Max drawdown in backtest < 15%.
- Results committed to `reports/backtest/` so launch parameters are justified by evidence.

A strategy that fails is re-parameterized or simplified until it passes, and the change log
is committed. (Backtest pass ≠ future profits — it gates obvious junk, nothing more.)

## Operations

- **Scheduled tasks (cloud):** SCALPEL hourly 10:30–15:30 ET weekdays; GLIDER 15:30 ET
  weekdays; a nightly scorekeeper snapshots both equity curves and refreshes the scoreboard.
- Each run is a fresh cloud session: clone repo → `python run_cycle.py --bot <name>` →
  commit journal/state → push. Credentials come from the repo's private config (paper-only keys).
- Failures: a cycle that errors commits its error log and skips trading (never trades on
  partial data). Two consecutive failed cycles → notify Everett.
