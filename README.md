# Bot Arena

Two paper-trading bots, one 30-day competition. See `DESIGN.md` for the full spec.

- **SCALPEL** — intraday session-momentum. Hourly cycles 10:30–15:30 ET, flat every night.
- **GLIDER** — swing trend-pullback. One cycle daily at 15:30 ET, holds 2–15 days.

Both: Alpaca paper accounts ($50k), US large caps + ETFs, long-only, 1% risk per trade,
bracket orders (stops live at the broker), −10% kill switch.

## Pipeline (every cycle)

`scan → analyze → validate → execute` — each stage journals its JSON artifact under
`journal/<run_id>/`, committed by the GitHub Actions run that produced it. Re-run an
analyzer on a committed `scan.json` and you must get the same intents: that's the
reproducibility contract, enforced by keeping analyzers pure.

## Layout

```
config/          strategy + risk parameters (versioned; never in code)
core/            broker wrapper, scanner, validator, executor, journal
bots/…/analyzer.py   the two strategies (pure functions)
backtest/        pre-launch validation harness (replays the live analyzers)
journal/         one directory per run: scan/analysis/validation/execution/meta
state/           equity curves, position ledger, kill-switch flags (audit copy)
reports/         scoreboard + backtest results
.github/workflows/   the schedulers (cron, UTC)
```

## Operations

Secrets live in GitHub Actions secrets (`SCALPEL_API_KEY`, `SCALPEL_API_SECRET`,
`GLIDER_API_KEY`, `GLIDER_API_SECRET`). Paper endpoint is hard-coded — this code
cannot place a live-money trade. Manual controls: every workflow has
`workflow_dispatch` (run now), and disabling a workflow in the Actions tab pauses
that bot. `backtest-validation` must pass before the trading workflows are enabled.

Cron times are UTC and assume EDT; if the competition ever spans a DST change,
shift the crons by an hour.
