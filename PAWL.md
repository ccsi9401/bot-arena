# PAWL

A pawl is the tooth in a ratchet that lets the wheel turn one way and blocks it
from turning back. That is the whole bot in one part: a floor that rises and
never falls. The name also happens to describe the `ratchet` command it already
had.

A long-only crypto trend bot, venue-pluggable, paper-first.

---

## The venue question, answered properly

The venue is not a fee question. It is a question about **what the venue lets
the strategy be**, and the answer changed the shape of this bot twice.

`python run_pawl.py venues` prints this table live:

| Venue | RT taker | Floor mode | Short | Perps | Paper |
|---|---|---|---|---|---|
| Alpaca crypto spot | 0.50% | resting ratchet | no | no | **yes** |
| Kraken Pro spot | 1.60% | native trailing | yes | no | no |
| **Kraken Derivatives US** | **0.10%** | native trailing | **yes** | **yes** | no |
| Interactive Brokers | 0.36% | **bot-side only** | no | no | yes |
| Simulated | target's | inherits target | — | — | yes |

**Interactive Brokers looks like the winner and isn't.** 0.12–0.18% per side is
roughly *half* Alpaca, with a good 20-asset universe. But per IBKR's own TWS API
docs, crypto supports **market and limit only**, time-in-force IOC or a 5-minute
limit expiry. No stop orders. No GTC. You cannot leave a protective order
resting at the exchange at all — so PAWL's floor collapses to a bot-side poll,
and a bot-side poll on a 4-hour Actions schedule is not protection, it is a
rumour of protection. Halving the commission does not buy that back. There is a
test that fails if anyone tries to "fix" this from the fee table alone.

**Kraken Pro spot is worse than what we have.** The July 2026 tier rework put
entry tier at 0.40% maker / 0.80% taker — a 1.60% round trip, over three times
Alpaca. It's partly redeemable because tiers now also qualify on *Assets on
Platform*, assessed in real time, so holding $20k+ moves you down the schedule
without trading a thing. Native trailing stops and margin shorting are real
advantages. No spot sandbox is a real cost.

**The actual upgrade is an instrument change, not a broker change.** In June
2026 Kraken launched **CFTC-regulated perpetual futures for US clients** —
listed on Bitnomial, cleared through NinjaTrader Clearing. BTC, ETH, SOL, XRP,
ADA, LINK, DOGE, LTC, AVAX, on an 8-hour funding cycle. That is the first
onshore US venue where the highest-evidence tier of the strategy manual is
reachable at all: **you can short, and you can collect funding.** Perp fees run
an order of magnitude under spot.

Everything PAWL had to delete because Alpaca is long-only spot comes back —
funding carry, basis, real pairs trading, market-neutral anything.

Two warnings that belong here and not in a footnote:

1. **Leverage is not the feature.** Perps offer it; PAWL does not take it.
   `broker.max_notional_x_equity` is 1.0 and the adapter refuses to exceed it.
   Use the venue for instrument access and nothing else.
2. **It is three months old.** Separate futures onboarding, an API with almost
   no public history, and no paper environment I could confirm.

**So the recommendation is sequenced, not singular:** prove the strategy on
Alpaca paper, because it is the only venue that combines a free paper account
with `stop_limit` + GTC — exactly what the resting floor needs. Rehearse the
target venue with `venue: sim`, which keeps a local ledger filled at real prices
but charges *Kraken's* fee schedule. Move only when both have run clean.

The gate is costed at whatever venue is configured, so the economics re-run
themselves on a one-line change. On identical synthetic data and effectively
identical trade counts, moving from Alpaca's 50bps to perps' 10bps took the
strategy from 5.6% to 10.0% CAGR. **The venue was worth more than any parameter
in the config.**

---

## What the long-only venue deleted

Working from the strategy manual, the current venue removes most of it:

**No shorting, no perps → every market-neutral strategy is gone.** Funding
carry, dated-futures basis, funding-extreme fades, real pairs trading — the
entire highest-confidence tier needs a short leg. Anyone claiming
"delta-neutral crypto carry" on a spot-only account is not doing that.

**50bps round trip → everything intraday is gone.** Volatility breakout,
lead–lag, liquidation fades, scalping: edge per trade is smaller than the toll.
Arithmetic, not judgement — and it's why PAWL runs daily bars with a hard cap
on how often it is *allowed* to trade. At 10bps on perps, that calculus changes
and some of these come back.

**No trailing_stop order type → the floor has to be synthesised.** Covered
below, and it's the most interesting part of the build.

What survives is trend-following plus risk overlays — narrower than the manual,
but honest, and it happens to be the best-evidenced part anyway.

---

## The three clocks

| Loop | Cadence | Job |
|---|---|---|
| **cycle** | daily, 00:10 UTC | Signals, ranking, sizing, rebalance |
| **ratchet** | every 4 hours | Move resting floors **up**, never down |
| *(the floor itself)* | **continuous, exchange-side** | Actually stops you out |

The third row is the point. Alpaca has no trailing-stop order type, so PAWL
builds one: a **resting `stop_limit` sell sits at the broker**, and the bot
raises it by cancel-and-replace.

This is strictly better than the 1–5 second polling loop the manual
prescribes, and for a reason worth internalising:

- The resting order is enforced in the matching engine. Your protection does
  not depend on this process being alive, on GitHub Actions firing on time, or
  on a network hop succeeding. A bot-side stop protects you only while the bot
  is running. This one protects you always.
- **Moving a floor is never urgent, because it only ever moves up.** A missed
  ratchet leaves you protected at a slightly lower price. It never leaves you
  unprotected. That asymmetry is what buys you a 4-hour cadence where a
  bot-side stop would need seconds.

Two details that matter in practice:

- The limit sits **1% below the stop**, so the order becomes marketable
  instead of resting unfilled while price runs straight through it. A
  `stop_limit` with the limit *at* the stop is a stop that doesn't work when
  you most need it.
- A resting sell **holds the quantity**, so anything that wants to sell or
  resize must cancel the floor first. `cancel_floor_for()` does this
  everywhere it's needed. The cancel/replace window is the one genuine gap,
  which is why the bot refuses to ratchet when price is already within one ATR
  of the floor — precisely when you least want to be momentarily naked.

---

## The strategy stack

Everything lives in `pawl/strategy.py` as pure functions. **The backtest calls
the same functions the live runner calls.** There is no second copy of the
sizing logic, and there must never be one — a backtest that reimplements the
strategy is testing the reimplementation.

**1. Regime gate (hard switch).** BTC above its 200-day SMA, or nothing
trades and everything exits. This carries most of the weight. It is also the
one element that survived bias correction in your earlier research, which is
the main reason it's the foundation here rather than a refinement on top.

**2. Asset trend filter.** Each asset must be above its own 100-day SMA.

**3. Momentum with a buffer band — not a zero-line cross.** Enter above
**+5%** trailing 28-day return; exit below **−2%**. The gap between those is a
hold zone, and it is the single most important anti-whipsaw device in the
system. A bare cross generates a stream of enter/exit pairs around the
threshold, and at 50bps a turn, that's how trend systems die. Held positions
are judged against the *exit* threshold; new candidates against the *entry*
threshold. Same asset, two different bars, depending on whether you already
own it.

**4. Ranking by risk-adjusted momentum.** 28-day return ÷ realised vol, top 3.
Raw return ranking systematically hands you the wildest asset in the universe.

**5. Volatility targeting.** Weight ∝ 35% target vol ÷ the asset's own
realised vol, capped at 35% per position and 85% invested total. Small in
chaos, large in calm — the opposite of what instinct does. Cheapest
risk-adjusted-return improvement available.

**6. Three ways out, any one fires.**
- Floor: high-water daily close − 3 × ATR(14), ratcheting, resting at broker
- Momentum: 28-day return below −2%
- Regime: BTC loses its 200-day → flat everything

**7. Drift bands + a turnover cap.** A rebalance only happens when the gap
exceeds 5% of equity *or* 25% of the target weight. On top of that there's a
hard budget of **5 round trips per month** (exits always exempt). That number
is derived, not guessed: one round trip on an average 28% position costs
0.28 × 50bps ≈ 14bps of equity, so 5/month ≈ 8.5%/yr of fee drag — already a
large bite. Twelve a month would be ~20%/yr, which no long-only crypto trend
system earns back.

**8. Re-entry cooldown, 7 days.** (Introduced to stop floor-driven churn; with
the floor moved out to catastrophe distance it now rarely binds.)

Original note: After any exit, that asset is ineligible for
a week. *This one came out of running the gate, not out of reasoning* — the
first build stopped out on a wick and bought straight back in the next
morning, 1,452 times. Adding the cooldown and the turnover cap cut trades by
72% and fee drag from 539% to 8.1%/yr.

---

## Circuit breakers

| Trigger | Response |
|---|---|
| Any bar older than 2h past its close | **No trading at all** — not entries, not exits |
| Daily loss > 6% | Exits only for the cycle |
| Drawdown > 25% from high-water | Flatten everything, halt, require manual re-arm |
| 2 consecutive order failures | Halt |
| Position at broker that state doesn't know about | **Halt and report** — never trade around it |

The stale-data guard runs before anything else. A frozen feed that still
returns `200 OK` is the failure mode that turns a working bot into a losing
one, and it's the reason the guard blocks *exits* too — flattening on bad data
is exactly as wrong as entering on it.

The reconcile rule is deliberate: an unexplained position is a bug, and
trading on top of a bug makes it more expensive, not less.

---

## The gate

The bot **refuses to run a cycle** without a passing gate artifact less than
45 days old. `run_pawl.py gate` runs five arms on identical cost assumptions:

| Arm | What it isolates |
|---|---|
| `pawl_full` | Full universe with rotation |
| `pawl_core` | **BTC + ETH only — the bias-free control** |
| `bh_btc` | Buy and hold BTC |
| `bh_5050` | 50/50 BTC/ETH |
| `btc_regime` | BTC with the 200-day filter only, no floor, no rotation |

`pawl_core` is the arm to trust. The full universe is today's Alpaca list
replayed backwards, which *is* survivorship-biased; BTC and ETH led the market
for the whole window, so they carry no such question. And `btc_regime` exists
to answer the uncomfortable question honestly: **how much of this is just the
200-day filter?** If PAWL barely beats it, the extra machinery isn't earning
its keep.

The gate also decides the rotation sleeve on evidence. If `pawl_full` doesn't
beat `pawl_core` on Sharpe by more than 0.15, **rotation is switched off and
PAWL runs core-only** — written into state automatically. That isn't a
failure, it's the gate doing its job, and it's the same conclusion your
earlier stock-picking sleeve reached the hard way.

Signals read bar *t*'s close and execute at bar *t+1*'s open. There's a unit
test that proves it: appending a bar at the end must not change any prior
equity value. If it did, the engine was peeking.

---

## Running it

```bash
pip install -r requirements.txt
python -m pytest tests/ -q            # 23 tests, no network needed

python run_pawl.py venues             # capability sheet + the reasoning

export PAWL_API_KEY=...               # whichever venue is configured
export PAWL_API_SECRET=...

python run_pawl.py gate               # must pass before anything else works
python run_pawl.py pulse              # read-only status
python run_pawl.py cycle              # daily
python run_pawl.py ratchet            # every 4h
python run_pawl.py flatten            # panic button; sets manual_halt
```

GitHub Actions secrets: `PAWL_API_KEY`, `PAWL_API_SECRET`. The workflow picks
its mode from which cron fired, and commits `state/` and `journal/` with the
same hardened retry-and-rebase push the other bots use.

---

## What I have and haven't verified

Run on **real daily BTC/ETH bars, Feb 2020 - Sep 2026** (2,194 days tested),
costed at Alpaca's 25bps taker + 10bps slippage:

| Arm | Return | CAGR | Max DD | Sharpe | Trades |
|---|---|---|---|---|---|
| **PAWL (core)** | **+546%** | **36.4%** | **-25.1%** | **1.35** | 106 |
| Buy & hold BTC | +653% | 35.9% | -76.7% | 0.82 | 1 |
| Buy & hold 50/50 | +714% | 37.6% | -77.3% | 0.81 | 2 |
| BTC + 200d filter only | +512% | 31.7% | -65.8% | 0.88 | 53 |
| *PAWL with a tight 3x floor* | *+156%* | *16.9%* | *-24.7%* | *0.80* | *136* |

**It matches buy-and-hold BTC's return while cutting the worst drawdown from
-77% to -25%.** That is the trade this bot exists to make.

### The tight trailing floor was wrong, and the data said so

The original build put a 3x-ATR trailing floor under every position -- the
"rising floor", the thing the bot is named for. On real data it cost **~20
points of CAGR and bought zero drawdown protection** (-24.7% vs -25.1%). Worse,
in the COVID crash window -- precisely the scenario a trailing stop exists for
-- it made drawdown *worse*: **-17.8% with the floor vs -9.9% without.**

The mechanism is clear once you see it. The floor sells normal crypto
volatility near the low, and the re-entry cooldown then locks the bot out of
the rebound. It realises the loss and misses the recovery. Meanwhile the
**regime gate was already doing all the defensive work** -- it sat in cash for
the whole of 2022 and lost nothing.

Sweeping the multiple confirmed it: 2x -> 0.95 Sharpe, 3x -> 0.80, 4x -> 1.14,
6x -> 1.32, 10x -> 1.35, none -> 1.35. That surface is *unstable* in the tight
range and only settles once the floor is far enough away to stop firing --
which by this manual's own rule ("if small parameter changes swing results
wildly, you found noise") means no tight setting is trustworthy.

So the floor moved out rather than away. `mode: catastrophe` puts it at
whichever of 10x ATR or -35% from high-water is *further* from price. It never
fires on noise. It is still there if a gap or an exchange event happens while
the daily loop is asleep. The gate now picks the mode on evidence, with one
deliberate thumb on the scale: if the catastrophe floor is within 0.10 Sharpe
of going bare, it keeps the floor -- **insurance you cannot measure is still
worth buying when it is free.**

The ratchet the bot is named for still exists. It just ratchets a disaster
stop instead of selling every pullback.

### What is still unverified

- **The catastrophe floor never fires in this window.** Its arm is numerically
  identical to no floor at all. It is untested insurance, kept on the argument
  above and not on evidence.
- **The rotation sleeve has not been tested.** It shows a +0.000 Sharpe edge
  only because this run had no alt data -- not because it was measured and
  found wanting. Re-run the gate with the full universe before believing that
  verdict either way.
- **One window, roughly 1.5 cycles.** It contains the COVID crash, the 2021
  bull, the 2022 bear and the recovery, which is more regimes than most crypto
  backtests see -- and still not many.
- **BTC and ETH are the two survivors of the era.** Choosing between two
  winners is an easier problem than choosing among fifty.
- **The bars came from Coinbase, not Alpaca**, and were reconstructed from
  delta-encoded log returns (1bp precision). Re-run `gate` against your own
  Alpaca feed before arming; the reference run is in
  `reports/pawl_gate_coinbase_reference.json`.

23 unit tests pass, covering the floor's never-descends invariant, the buffer
band, the caps, the drift filter, every circuit breaker, the venue capability
model, and a no-lookahead proof (appending a bar must not change any prior
equity value).

## Before real money

The gate passing is necessary and nowhere near sufficient.

1. Paper through a real drawdown, not a quiet month. You are testing plumbing
   — rate limits, reconnects, partial fills, clock drift — more than strategy.
2. Confirm the floor actually fires. Deliberately set a floor just under spot
   in paper and watch the resting order fill.
3. Break the feed on purpose and confirm the bot halts instead of trading on a
   frozen price.
4. Check real fills against the 25bps + 10bps cost model, especially on the
   rotation names. If real slippage is worse, re-run the gate with the true
   number before trusting any of it.
5. Then start small and expect live results below backtest. That's the normal
   outcome, not a sign something broke.

This is a paper bot by configuration, and `meta.mode: live` additionally
requires `PAWL_ALLOW_LIVE=yes` in the environment. Two locks, deliberately.

I'm not a financial advisor and none of this is advice. Crypto moves far
enough to take an account, and a long-only bot with no short leg has no hedge
when it does — its only defence is being flat, which is exactly what the
regime gate and the floor are for.

---

## 2026-09-23 revision: BTC/ETH only, 70/30 benchmark, one live bug fixed

**Scope.** Rotation list emptied, so this is a BTC/ETH bot. Small coins crash together and
fill badly, so adding them gives correlated risk, not diversification. The rotation code
still exists, and the gate re-tests it if names are ever added back.

**New benchmark arm.** `blend`: 70% BTC / 30% ETH, always invested, rebalanced monthly
or when a weight drifts 5 points, with the same costs. New gate check `sharpe_vs_blend`:
if PAWL can't beat a fixed 70/30 on Sharpe, it isn't earning its complexity.

**Bug: the liquidity screen locked out BTC and ETH on Alpaca.** Alpaca bars report only
Alpaca's own volume, about $150k a day for BTC since 2023, against a $3M screen. On
Alpaca data the bot made 28 trades in 5 years and returned 0.5% a year. Live, it would
never have entered anything. Core names now skip the screen; rotation names still face it.

**Fee check fixed.** It divided dollar fees by *starting* equity, so on a long winning
window it failed just because the account had compounded. It now uses average equity.

**Results after the fixes.** Alpaca costs are 25bps + 10bps a side. Signal on today's
close, fill at tomorrow's open.

| Window | PAWL CAGR / DD / Sharpe | 70/30 blend | BTC buy & hold | Gate |
|---|---|---|---|---|
| Coinbase 2017-06 to 2026-09 | 40.9% / -29.8% / 1.40 | 72.7% / -84.5% / 1.13 | 58.2% / -83.8% / 1.01 | PASS |
| Coinbase 2020 to 2026 | 36.1% / -25.1% / 1.31 | 40.6% / -76.3% / 0.86 | 37.1% / -76.7% / 0.82 | PASS |
| Coinbase 2017 to 2021 | 68.9% / -29.8% / 1.73 | 166% / -84.5% / 1.60 | 116% / -83.8% / 1.35 | PASS |
| Coinbase 2022 to 2026 | 17.7% / -15.5% / 0.96 | 6.5% / -76.3% / 0.40 | 7.7% / -76.7% / 0.41 | PASS |
| **Alpaca 2021 to 2026** | **14.9% / -19.1% / 0.80** | 24.2% / -76.3% / 0.66 | 20.2% / -76.7% / 0.61 | PASS |

Read this honestly. PAWL wins on Sharpe and drawdown in every window. It **gives up
raw return in strong bull runs**: at most 70% is invested (two names capped at 35%
each), and it's in cash for a while at every trend turn. It exists to trade return for
never taking a -77% hit. If you want the higher return, the 70/30 blend is the
alternative, and it comes with the -77% to -85% drawdowns.

**Rejected: 4-hour dip-buying inside an uptrend.** Buy when RSI(2) on 4-hour bars drops
below 5/10/15 while the daily regime is up; exit on a close above the 5-bar average or
after 6 or 12 bars. Tested on Coinbase 1h bars resampled to 4h, 2018 to 2026, BTC and
ETH, 12 variants. The average trade *before costs* ranged from -0.18% to +0.15%. After
Alpaca costs, every variant lost 83-98%. Even at perps fees (5bps a side) all but one
lost money. There is no edge here for costs to eat, so it isn't built.

**Deferred: funding-rate carry.** It needs a short perp leg. Alpaca has no perps, and the
Kraken US perps adapter is not wired up and has no paper environment. If it's built, it
should be a separate market-neutral bot on its own venue, not a PAWL sleeve.

**Account.** PAWL needs its **own** Alpaca paper account. Twin-Coin already holds BTC/ETH
on PA3W63AXACS1, and PAWL's reconcile rule halts when the broker shows a position it
doesn't know about.
