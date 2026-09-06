# TALON — operating manual

Long-only crypto trend/dip ensemble with a rising floor. Alpaca **paper** account,
seven `/USD` pairs, daily bars, one cycle every four hours via GitHub Actions.

**Status (2026-09-06, evening).** `gate.json` reads `passed: false` on one check:
the first sub-window (April 2023 to December 2024) has Sharpe 1.31 against 1.35 for
the equal-weight basket, a basket lifted by SOL's ten-fold run. Every other check
passes: Sharpe 0.91 over 3.4 years, 26% max drawdown against 72% for the basket,
80 trades, the second half beats the basket 0.33 to -0.21. The bot does not trade
until the gate passes; loosening the gate to get there is not on the table. Details
in section 7.

This manual teaches the rules, not just the API. Every number quoted below lives in
`config/talon.yaml`; nothing in a `.py` file decides how much to risk or when to exit.

---

## 1. What TALON is trying to do

Crypto trends hard and then gives most of it back. Buy-and-hold across a basket has
made money over most multi-year windows, but with 70–90% drawdowns that no operator
holds through. TALON's bet is narrow and testable:

1. **Only be long when the market is in an uptrend** (regime gate).
2. **Enter on evidence that a trend is starting or resuming** (three voters).
3. **Concentrate in what is already moving** (cross-sectional momentum).
4. **Never give a winning trade back below break-even, and never give the account's
   gains back** (position floor + account floor).

Everything else is plumbing. The backtest gate exists to ask the one question that
matters: does this beat simply holding the basket, on risk-adjusted terms, after
pessimistic costs? If not, TALON does not trade.

---

## 2. The strategies and why each is in the book

Every strategy is a function `(df, cfg) -> Series of 0/1` aligned to the bar index.
The live cycle takes the last value; the backtest takes the whole series from the
*same* function. That is what makes the backtest an honest rehearsal of the live code.

### 2.1 `donchian_breakout` (weight 1.0)

Close above the highest high of the **previous** 40 bars, and above the 100-day SMA.

*Why:* the oldest trend rule there is (Turtles, 1980s). A new 40-day high in a
market that is already above its 100-day average is the cleanest observable sign
that a trend is under way. It fires rarely and late, which is a feature: late
entries in real trends beat early entries in fake ones.

*The shift:* the breakout level is computed on the 40 bars **before** today
(`.shift(1)`). Without it, today's close can never exceed a window that contains
today's high, and the strategy is silently dead. The test suite asserts on a
strictly rising series that the signal fires every bar once warm — only possible
with the shift.

### 2.2 `ema_trend` (weight 1.0)

21-day EMA above the 55-day EMA, **and** the 55-day EMA higher than it was 10 bars
ago.

*Why:* the Donchian rule needs a fresh high; this one does not. It keeps voting
through the middle of a trend, where a pullback would leave Donchian silent. The
slope condition kills the classic failure mode of EMA crosses: a fast line drifting
above a flat slow line in a range, which is a chop signal, not a trend signal.

### 2.3 `dip_reversion` (weight 0.0, switched off, kept in the book)

Close above the 100-day SMA, **and** (RSI(14) < 35 **or** 20-day z-score < -1.5).

*Why it was included:* in an established uptrend, sharp two-to-three-week washouts are
where the best risk/reward entries are, because the initial stop sits close to the
entry and the trend does the work. The uptrend condition is **mandatory**. Remove it
and this becomes "buy whatever fell hardest", which in crypto is how accounts die. The
test suite feeds a steady downtrend with a sharp dip and asserts zero signals.

*Why it is off:* the ablation on 2023-04 to 2026-09 improved Sharpe from 0.88 to 1.03
with it removed, and the mechanism is plausible rather than noise. It fires when RSI is
below 35, exactly when the two trend voters are weakest, so the ensemble reaches
agreement at the worst moments. Mean reversion inside a trend book dilutes the book.
It stays at weight 0 so the experiment is one edit to reverse.

### 2.4 `momentum_score` (weight 0.5, not a voter)

`0.5 × 30-day return + 0.5 × 90-day return`, ranked **across** the universe each bar.
The top 5 names with positive momentum get this weight added to their score; the
result is re-normalised so scores stay in 0–1.

*Why:* the three voters look at each coin in isolation. Cross-sectional momentum is
the best-documented anomaly in crypto (as in equities): the coins that led over the
past one to three months keep leading. This term breaks ties in favour of leaders
and away from laggards that merely happen to be above their averages. It needs the
whole panel, so it lives in the planner rather than in `strategies.py`.

### 2.5 The ensemble

`score = sum(weight x signal) / sum(weight)` over the active voters, then blended with
the momentum flag. With the two trend voters at 1.0 and momentum at 0.5:

| voters firing | momentum top-5 | score |
|---|---|---|
| 1 of 2 | no | 0.40 |
| 1 of 2 | yes | 0.60 |
| 2 of 2 | no | 0.80 |
| 2 of 2 | yes | 1.00 |

With `min_score` at 0.30 a single trend voter is now enough to be a candidate; the
momentum rank and the second voter decide the ordering. Set a weight to 0 to switch a
strategy off without deleting it (the test suite covers this).

### 2.6 The regime gate

BTC/USD above its **200-day SMA**. When off, exits still run but no new positions
are opened. 200 is fixed by policy and must not be swept for a "better" value: it is
the one number every trend-follower on earth uses, so it is the least likely to be
an artefact of our data.

*Why:* crypto's worst drawdowns happen with BTC below its 200-day. Sitting those
periods out costs some upside at the turn and removes most of the pain. STEWARD's
equity backtest reached the same conclusion for SPY.

---

## 3. The position floor — worked example

A floor is a stop that moves up and never down. Every open position has one,
stored in state and re-evaluated every cycle.

Setup: entry **100.00**, ATR(14) **2.50**, `initial_atr_mult` 2.0, `min_r_pct` 0.5%,
`fee + slippage` 40 bps.

- `r_unit = max(2.0 × 2.50, 100 × 0.005) = 5.00` — one R is $5 of price.
- `cost_pad = 100 × 40/10000 = 0.40` — break-even must also cover the round trip.
- initial `floor = 100 − 5 = 95.00`, stage `basement`.

Each update pushes `high_water` up, computes `r_now = (high_water − entry)/r_unit`,
builds candidate floors and takes the **max**. The current floor is always one of
the candidates — that single line is what makes the ratchet monotonic.

| price | high_water | r_now | candidates (max wins) | floor | stage |
|---|---|---|---|---|---|
| 100.00 | 100 | 0.0 | 95.00 | **95.00** | basement |
| 103.00 | 103 | 0.6 | 95.00 | 95.00 | basement |
| 105.00 | 105 | 1.0 | 95.00 · **100.40** (break-even) · 86.10 (trail) | **100.40** | breakeven |
| 110.00 | 110 | 2.0 | 100.40 · **105.00** (lock 1R) · 90.20 | **105.00** | locked_1R |
| 115.00 | 115 | 3.0 | 105.00 · **110.00** (lock 2R) · 94.30 | **110.00** | locked_2R |
| 120.00 | 120 | 4.0 | 110.00 · 98.40 | 110.00 | locked_2R |
| 125.00 | 125 | 5.0 | 110.00 · **117.50** (lock 3.5R) · 102.50 | **117.50** | locked_3.5R |
| 140.00 | 140 | 8.0 | 117.50 · 114.80 | 117.50 | locked_3.5R |
| 150.00 | 150 | 10.0 | 117.50 · **123.00** (trail 82%) | **123.00** | trailing |
| 200.00 | 200 | 20.0 | 123.00 · **164.00** | **164.00** | trailing |
| 180.00 | 200 | 20.0 | 164.00 · 164.00 | 164.00 | trailing |
| 163.00 | 200 | 20.0 | — | 164.00 | **breached → exit ≈ +12.6R** |

Read the table top to bottom and notice what each rule buys you:

- **Basement (0 to +1R).** The trade can lose one R and no more. The trail is
  *not* active here: an 18% trail on a fresh entry would be a looser stop than the
  ATR stop, which is backwards.
- **Break-even at +1R.** Once the trade has made what it risked, it may no longer
  lose. The `cost_pad` means "break-even" includes the fees, so a scratch is a real
  scratch.
- **Locked steps at +2R/+3R/+5R.** Profit is banked in chunks. A trade that reaches
  +5R can only ever finish at +3.5R or better. This is the difference between
  "was up 5R" and "made 3.5R".
- **Trail at 82% of high-water.** Beyond the last step the ratchet stops, and the
  trail takes over so that a runaway winner is still followed. It only wins the
  max once high-water is far enough above the last lock — on this trade around +10R.
- **The pullback rows.** From 200 to 180 the floor does not move. It never moves
  down. The suite runs a trade to +8R, feeds it a descending sequence and asserts
  the floor never changes.

Exits are evaluated on the close of the last completed daily bar, not the live mark
(`evaluate_on: completed_bar`): a floor that the day's low pierced but the close did
not is not a breach. That is a deliberate choice, measured in section 7, and it is the
same convention the backtest fills on. A breach is a market sell on the first cycle
after the bar closes. Positions found at the broker
with no floor in state (orphans) get a fresh floor constructed at their average
entry rather than being ignored.

---

## 4. The account floor — worked example

The position floor protects one trade. The account floor protects the account.

Rules (`rising_floor.account`): once equity is at least **10%** above `basis`, sweep
**40%** of the eligible gain into `locked`, counting the gain only in whole **5%**
steps of basis; then raise `basis` to `equity − sweep`. Tradeable equity is
`equity − locked`, and every new position is sized on tradeable equity.

| event | equity | basis | gain | steps | sweep | locked | new basis | tradeable |
|---|---|---|---|---|---|---|---|---|
| start | 10,000 | 10,000 | — | — | — | 0 | 10,000 | 10,000 |
| +9% | 10,900 | 10,000 | 9.0% | 1 | **0** (below activation) | 0 | 10,000 | 10,900 |
| +10% | 11,000 | 10,000 | 10.0% | 2 | 40% × 1,000 = **400** | 400 | **10,600** | 10,600 |
| tick up | 11,240 | 10,600 | 6.0% | 1 | 0 (no nibbling) | 400 | 10,600 | 10,840 |
| +13.2% | 12,000 | 10,600 | 13.2% | 2 | 40% × 1,060 = **424** | 824 | 11,576 | 11,176 |
| drawdown | 9,000 | 11,576 | −22% | — | 0 | **824** | 11,576 | 8,176 |

Three things to notice:

- **Whole steps only.** At +9% nothing happens, and at +12.4% only the 10% step is
  swept. A sweep is a decision, not a running tally, so the reserve grows in
  discrete moves you can see in the journal.
- **Basis rises to `equity − sweep`.** After the first sweep the account must make a
  fresh 10% on top of $10,600 before the next one. The same gain is never swept twice.
- **`locked` never decreases.** In the drawdown row the account is 25% off its high
  and the reserve is untouched. There is no method on `AccountFloor` that lowers it;
  the attribute itself refuses a smaller value (`ValueError`), and the suite drives a
  random equity path through 450 updates asserting monotonicity. Releasing the
  reserve is a human decision, made by editing `state/talon/talon_state.json`.

The practical effect: after a good run, the bot is trading a smaller book than the
account shows, and a subsequent bad run can only draw down the tradeable part.

---

## 5. Sizing

For each selected name:

1. `risk_dollars = tradeable_equity × 1%`
2. `r_unit = max(2 × ATR, 0.5% × price)` — the same function the floor uses
3. `qty = risk_dollars / r_unit` — a stop-out costs exactly 1% of tradeable equity
4. inverse-vol scale across the selected set: `1/vol`, normalised to mean 1.0,
   clipped to [0.5, 2.0]. Total risk budget is unchanged; it is redistributed so a
   quiet name and a wild name carry similar risk.
5. cap at 25% of tradeable equity per position; drop anything under $25 notional
6. gross cap: if existing + new exposure exceeds 75% of tradeable equity, haircut
   every **new** position pro rata. Never drop the tail — the tail is the
   diversification.

All of this is `planner.plan()`. The backtest calls the same function per bar; the
test suite asserts `backtest.plan is talon.planner.plan` and that no sizing
vocabulary appears in `backtest.py`.

---

## 6. Risk controls

| control | setting | what it does | why |
|---|---|---|---|
| regime gate | BTC > 200-day SMA | no new entries when off | the big drawdowns live below the 200-day |
| min score | 0.30 | one lone voter is not enough | ensembles are for agreement, not coverage |
| max positions | 5 | at most five open names | seven-coin universe, most of it at once is the ceiling |
| risk per trade | 1% of tradeable | initial stop distance × qty = 1% | survive 20 straight losers with the book intact |
| position cap | 25% of tradeable | notional cap per name | a low-ATR name would otherwise size huge |
| gross cap | 75% of tradeable | pro-rata haircut | always some cash; positions plus reserve never exceed equity |
| position floor | 2 ATR, ratchet, 18% trail | per-trade stop that only rises | see §3 |
| account floor | 10% / 40% / 5% steps | locks realised account gains | see §4 |
| daily loss breaker | -4% from UTC day start | halt for the rest of the day; `daily_breaker_action` decides whether it also flattens (default: block entries only, floors manage exits) | circuit breakers stop new risk; stops handle existing risk |
| kill switch | −18% from equity high-water | halt + flatten until a human resets | the "something is broken" stop |
| costs | 25 bps fee + 15 bps slippage | applied to every backtest fill, adversely | pessimistic on purpose |
| gate | see §7 | no orders without a passed `gate.json` | the strategy earns the right to trade |
| live-money guard | two switches | paper unless config **and** env both say live | one typo cannot move real money |
| order isolation | try/except per order | one rejected order does not abort the cycle | the other four names still get managed |
| same-cycle re-entry | excluded | a name stopped out this cycle is not re-bought this cycle | whipsaw protection; the backtest does the same per bar |

The halt semantics deserve a sentence. A halt is cleared at the next UTC day, but
the drawdown check re-fires against the same high-water mark, so an 18% drawdown
keeps the bot flat until a human sets `equity_high_water` in state to the current
equity. That is intended: the kill switch is a request for review, not a timer. In
the backtest that review is modelled as 30 flat bars (`backtest.kill_switch_restart_bars`)
followed by a high-water reset, and the count of such events is reported.

---

## 7. The backtest gate

`python backtest/backtest.py` fetches `backtest.lookback_bars` (1,500) daily bars, puts
them on a **point-in-time** panel (union calendar, NaN where a name has no bar; a name
is scored once it has 250 of its own consecutive bars; a held name that loses its bars
is force-closed), and walks forward from bar 250. For every bar `i`:

1. floors set at the previous close are checked against bar `i`'s **low**; a touch
   fills at the floor, a gap through fills at the open (`backtest.exit_check:
   intrabar_low`, the daily-bar twin of the live cycle's six checks a day);
2. mark to market, account floor, kill switches;
3. floors ratchet on the close; a halt that flattens fills at the next open;
4. `plan(..., i=i)` decides entries, filled at the **open of bar i+1** with 40 bps
   against. Signals are never filled at the close that produced them; the suite
   tampers with every bar after `i` and asserts the plan at `i` is unchanged.

Two control arms run over the identical window with the identical entry cost:

- **BTC buy-and-hold**: the thing you would do with no bot.
- **Equal-weight buy-and-hold** across the names tradeable on day one: the thing
  the bot is actually competing with, since it is long that same basket.

The gate passes only if **all** of these hold:

| check | threshold |
|---|---|
| Sharpe (365-day annualised) | >= 0.80 |
| max drawdown | <= 35% |
| total return | >= 0% |
| closed trades | >= 30 |
| years **tested** (after the 250-bar warm-up) | >= 3.0 |
| beat equal-weight on Sharpe | strictly greater |
| **each of 2 sub-windows**: drawdown <= 35% and beat equal-weight on Sharpe | both halves |

A bot that makes 20% while the basket makes 60% loses the beat-the-basket check, and
that is the point. The sub-window check exists so that one long bull leg cannot carry
a pass. Add `btc_hold` to `backtest_gate.controls_to_beat` to also require beating BTC.

**What the hardening found.** Same window, 2023-04 to 2026-09:

| configuration | Sharpe | beats basket in both halves |
|---|---|---|
| first pass: 7 names, stops checked on close, dip on | 0.88 | yes |
| + point-in-time 10-name universe | 1.08 | yes |
| + dip_reversion off | 1.03 | yes |
| + stops checked against the daily low | **0.44** | no |
| all five changes | 0.34 | no |
| all five, live exits on completed bars (current config) | 0.91 | first half 1.31 vs basket 1.35 |

The first-pass edge depended on stops that were only checked at the daily close. A
floor two ATRs below entry is touched by intraday wicks far more often than by
closes; checked six times a day it turns winners into 0.13R scratches. Widening the
floor (three ATRs, 25% trail) under intraday checking keeps drawdown at 13% but does
not restore the edge. So the live cycle now evaluates floor exits on the close of the
last completed daily bar (`rising_floor.position.evaluate_on: completed_bar`), which
is standard for daily-bar trend following, and the backtest derives its fill
convention from the same key so the two cannot drift apart again. The four-hourly
cycles still run the equity kill switch and breaker every time.

With that in place the gate fails one check: the 2023-24 half against a basket lifted
by SOL's ten-fold run, 1.31 to 1.35. That is a real result, not a bug, and the
sub-window rule is doing precisely what it was added to do. Ways forward that do not
involve weakening the gate: more history (Alpaca's crypto bars begin mid-2022, so the
window is what it is), a wider universe so the basket is less dominated by one name,
or accepting that a stop-managed long-only book does not beat a buy-and-hold basket
in a mania half. What is not acceptable is tuning knobs until the 0.04 closes.

On a pass, `state/talon/gate.json` is written with all metric sets and every check.
On a fail it is written with `passed: false` and the run exits 1. Commit it either way;
the cycle reads it.

`--synthetic` replaces the data with a geometric random walk (chop, bull, bear,
no edge by construction) so the whole engine can be exercised offline. It prints
loudly that the verdict is meaningless and **never** writes `gate.json`. A pass on
synthetic data would mean a look-ahead bug, not a discovery.

---

## 8. Setup

1. **Secrets.** In the repo's Actions secrets add `TALON_API_KEY` and
   `TALON_API_SECRET` for a dedicated paper account (one bot, one account, per
   `KEY-REGISTRY.md` rule 1). Nothing else is needed; `TALON_FORCE_LIVE` must not
   exist anywhere.
2. **Tests.** `pytest tests/test_talon.py -q` — 62 offline tests, no network.
3. **Smoke.** `python backtest/backtest.py --synthetic` — must produce trades, a
   non-zero locked reserve, and a FAIL.
4. **Earn the gate.** `python backtest/backtest.py` against real bars. Read the
   report, not just the verdict: check `regime_on_share`, `avg_r`, `kill_switch_events`
   and how the controls did. Commit `state/talon/gate.json` only if you agree with it.
5. **Pulse first.** Dispatch the `talon` workflow with `mode: pulse`. It reads the
   account, fetches bars, updates floors in memory and prints intended exits and
   entries without writing anything.
6. **Enable.** The schedule (`5 0,4,8,12,16,20 * * *`) runs `cycle`. Each run
   commits `state/talon/` and `journal/talon/YYYY-MM-DD.jsonl` back to `main` with
   the fetch + rebase retry loop, so concurrent bots do not lose journal rows.
7. **Read the journal.** One JSON row per event: `plan`, `entry`, `floor_up`,
   `exit_signal`, `exit`, `sweep`, `halt`, `order_failed`, `snapshot`. The snapshot
   row carries equity, locked, tradeable, day and total P&L, regime, open count.

Human-only actions, all done by editing `state/talon/talon_state.json` and committing:

- **Release the reserve:** lower `account_floor.locked`. The bot cannot.
- **Reset the kill switch:** set `equity_high_water` to current equity.
- **Retire the book:** dispatch `mode: flatten`; floor state is kept for the record.

Local runs use the same files: export the two env vars and run
`python run_talon.py --mode pulse`. The `cycle` mode refuses (exit 2) until
`gate.json` says `passed: true`.

---

## 9. File map

| file | role |
|---|---|
| `config/talon.yaml` | every tunable; the only place a number may live |
| `talon/strategies.py` | indicators, the three voters, momentum key, regime, ensemble |
| `talon/floor.py` | `PositionFloor`, `AccountFloor`, `r_unit_for` |
| `talon/planner.py` | `plan()` — scoring, ranking, sizing, gross cap; `kill_switch_state()` |
| `talon/data.py` | Alpaca crypto bars, pagination, retry, `align` (intersection or point-in-time), `drop_incomplete_bar` |
| `talon/broker.py` | REST adapter, two-switch live guard, slash handling |
| `run_talon.py` | `cycle` / `pulse` / `flatten` |
| `backtest/backtest.py` | walk-forward (intrabar exits, PIT universe), control arms, sub-window gate, `--synthetic` |
| `tests/test_talon.py` | the suite; every checkpoint in the build spec is an assertion here |
| `.github/workflows/talon.yml` | six cycles a day, dispatch with `mode`, hardened commit |
| `state/talon/talon_state.json` | floors, halt, high-water, day basis |
| `state/talon/gate.json` | the backtest verdict the cycle checks |
| `journal/talon/*.jsonl` | one row per event, one file per UTC day |
