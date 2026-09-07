# TWINCOIN — Twin-Coin Trend Bot (BTC/ETH, Alpaca paper, $500 ledger)

Long-only daily trend follower on BTC/USD and ETH/USD. Enters when five of six daily
indicators agree, sizes each position so a stop-out costs 2% of a $500 ledger, exits on a
3-ATR chandelier floor that only moves up, takes one-third off at +2R, and sits in cash about
60% of the time. Spec and backtest: the published page "Twin-Coin Trend Bot" and
`Documents/Alpaca Trading EJ/Twin-Coin-Trend-Bot/backtest/`. Backtest on Alpaca bars
2021-2026: +21.9%, max drawdown −21.7% (that drawdown is running now, from the 2025 peak).

Paper only. `twincoin/broker.py` has no live switch and refuses any config that is not
`mode: paper`.

## Phone page

https://ccsi9401.github.io/bot-arena-board/twincoin.html — rebuilt and pushed by the workflow
after every ring (`twincoin_board.py` → `board/twincoin.html` → the public bot-arena-board Pages
repo, same as the other bots). Shows standing, the $500 ledger against BTC buy-and-hold, the
live six-vote per coin, open positions with floors, closed trades with R multiples, the latest
cycle's actions, and the next ring. Auto-refreshes every 15 minutes; add it to the home screen.
Any workflow_dispatch (even mode `signal`, which needs no keys) republishes it immediately.

## Layout

| Path | What |
|---|---|
| `config/twincoin.yaml` | every parameter; must match the backtest's `P` dict |
| `twincoin/strategy.py` | indicators + six-vote, verbatim from the backtest; `tests/test_twincoin.py` pins parity |
| `twincoin/data.py` | Alpaca daily/4H bars, Coinbase cross-check that clips phantom wicks |
| `twincoin/broker.py` | Alpaca REST, paper endpoint hard-coded |
| `twincoin/engine.py` | the cycle (sync fills → reconcile → mark → risk rules → floor exits → daily vote → entries) |
| `run_twincoin.py` | modes: signal · preflight · arm · pulse · cycle · flatten · clear-halt |
| `state/twincoin/twincoin_state.json` | ledger, positions, floors, resting order ids; committed by the workflow |
| `state/twincoin/armed.json` | the trade permit; written only by `--mode arm` |
| `journal/twincoin/*.jsonl`, `trades.csv` | every decision with the numbers behind it; also the tax record |
| `.github/workflows/twincoin.yml` | the runner; `bell.yml` rings it at :15 past every UTC hour divisible by 4 |

## The $500 ledger inside a bigger paper account

Paper accounts start at $100k and cannot be funded with $500 unless you reset them to a
custom amount. The bot does not care: `--mode arm` records the account equity at that
moment, and from then on `ledger_equity = 500 + (account_equity_now − equity_at_arm)`.
Sizing, sleeve caps, the cash floor, the loss limits and the kill switch all use the ledger.
The account's other $99.5k is never touched, and nothing else may trade on this account
(KEY-REGISTRY Rule 1: one bot per account).

## Setup — in this order

1. **Pick or create a paper account for TWINCOIN alone.** Check `KEY-REGISTRY.md` in
   `Documents/Alpaca Trading EJ` first: Alpaca caps paper accounts, and regenerating a key
   while the wrong account is selected has killed another bot's key before (GLIDER, 2026-09-06).
   PA34YCDTEQMA is listed as free inventory with a dead key. Confirm crypto is enabled on it.
2. **Generate the API key yourself** with that account selected in the Alpaca dashboard.
   Do not paste it into chat, a file in this repo, or the registry (prefix only there).
3. **Add repo secrets** `TWINCOIN_API_KEY` and `TWINCOIN_API_SECRET`
   (GitHub → bot-arena → Settings → Secrets and variables → Actions). Until they exist the
   workflow skips green with a notice.
4. **Preflight:** Actions → twincoin → Run workflow → mode `preflight`. It checks auth, that
   `crypto_status` is ACTIVE, that BTC/USD and ETH/USD are tradable, that bars load, and that
   the account is flat. Read the log. Locally: put the two keys in `bot-arena/.env`
   (git-ignored) and run `python run_twincoin.py --mode preflight`.
5. **Pulse:** mode `pulse`. A full dry run that prints what a cycle would do right now
   (`would_enter`, `entry_blocked`, floors) and touches nothing.
6. **Arm:** mode `arm`. Writes the ledger zero point and `armed.json`, commits them. From
   the next ring, scheduled runs trade. Arm only when the account is flat.
7. **Watch the first week** in the Actions log and `journal/twincoin/`. The first entry
   will likely be ETH (5 of 6 on 2026-09-06; BTC was 4 of 6). Expect most cycles to end in
   `vote` + `snapshot` with no order.

Spec step 9 says the first 20 live trades run at 1% risk before stepping to 2%. On paper
that ramp is optional; `risk.risk_pct` in the config is the knob.

## Operating notes

- **Cadence.** Decisions happen on the run after each UTC 00:00 daily close (the 00:15
  ring). The other five rings a day only manage exits. Extra rings are no-ops.
- **Resting orders.** After every entry the bot rests a limit sell for one-third at +2R and a
  stop-limit for the rest at floor×0.97 / floor×0.95. Alpaca's crypto feed prints phantom
  wicks, which is why it is a stop-limit and not a stop-market. When the floor ratchets, the
  stop-limit is cancelled and replaced. If you see one order per position instead of two,
  the partial has filled.
- **Floor exits are close-based.** The bot sells at market on the first run after a 4H close
  below the floor. The exchange stop is the backstop for the bot being dead, not the primary.
- **Reconcile.** An orphan position on the account is adopted with a fresh 2-ATR floor. A
  position that vanished from the account halts new entries until `--mode clear-halt`.
- **Kill switch.** Ledger equity 25% below its high-water mark: flatten, 30-day halt, then
  ten trades at 0.5% risk. It is journaled as `KILL_SWITCH`. Do not edit the state file to
  shorten the halt.
- **Flatten.** mode `flatten` cancels every order and closes every position, keeping the
  state file (positions cleared). Use it before re-arming or before handing the account to
  another bot.
- **Changing a parameter.** Edit `config/twincoin.yaml`, mirror it in the backtest `P` dict,
  re-run the backtest, commit both. One knob per quarter after 30 closed trades (spec §6).

## Reading the journal

Each cycle writes `data`, one `vote` per coin on daily closes, any `floor_ratchet`,
`floor_breached`, `entry_filled`, `partial_filled`, `backstop_filled`, `TRADE_CLOSED`, and a
`snapshot` with ledger equity, account equity, positions and floors. `trades.csv` is the
closed-trade log with R multiples; the backtest's `trades_FINAL_*.csv` files use the same
columns so live and tested distributions can be compared after 30 trades.
