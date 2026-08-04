# Bot Arena — Official Competition Rules

**Round 1: Day Traders — Claude ("SCALPEL") vs. ChatGPT ("GPT-DAY")**
Version 1.0 · August 2026 · Commissioner: Everett Jones

These rules are shared verbatim with both AIs before the opening bell. Once the round
starts, the rules are frozen; anything not written here is decided by the Commissioner,
whose rulings are final.

## 1. The match

1.1 One bot per AI. Round 1 pits each AI's **day-trading** bot against the other.
Round 2 will repeat these rules with each AI's **swing** bot (§10).

1.2 Each bot trades its own dedicated Alpaca **paper** account. No live money.

1.3 The round runs **30 calendar days** from the start date declared by the
Commissioner. Trading begins at the first market open on or after the start date and
ends at the market close of the final trading day inside the window.

## 2. Equal footing

2.1 Both accounts are reset to exactly **$50,000** before the start and begin flat
(no positions, no open orders).

2.2 Both bots begin trading the same day. If one side is not ready, the start date
moves; the clock never starts uneven.

2.3 Neither AI may see the other's positions, orders, or signals during the round.
Public scoreboard data (equity and summary stats, §6) is visible to both.

## 3. Instruments and market

3.1 Permitted: **US-listed common stocks and ETFs** priced above $5.00 with average
daily dollar volume above $10M. Long positions only — no short selling.

3.2 Prohibited: options, futures, crypto, OTC/pink sheets, leveraged/inverse ETFs
above 2x, and margin borrowing (each bot trades only its cash; no position may be
entered that requires borrowing).

3.3 Day-trader identity (Round 1): positions must be closed by the end of the same
trading session — **no overnight holds**. A position accidentally held overnight must
be closed at the next open and the incident is logged; three violations forfeit the
round.

## 4. Risk limits (identical for both)

4.1 Max risk per trade: no single trade may risk more than **2%** of current account
equity (entry-to-stop distance × shares).

4.2 Every entry must carry a protective stop at the broker at all times.

4.3 Max concurrent positions: **5**. One position per symbol at a time.

4.4 Daily halt: a bot down **3% or more on the day** must flatten and stop entering
until the next session.

4.5 Stop-out line: if an account touches **−15% from starting equity** ($42,500),
that bot's round ends immediately; it is scored at its equity when flattened. The
other bot may finish the full 30 days.

## 5. Autonomy — the point of the whole thing

5.1 The bots trade **autonomously**. After the opening bell, no human may add,
modify, cancel, or close a trade, adjust parameters, or feed the bot ad-hoc
instructions. The AIs are the traders; the humans are spectators.

5.2 Strategy code and parameters are **frozen at start**. Permitted during the round:
restarting crashed processes, fixing infrastructure (schedulers, API connectivity),
and correcting bugs that cause the bot to *fail to act as designed* — provided the fix
does not change trading logic and is logged (§6.2). Any strategy-logic change during
a round forfeits the round.

5.3 An operational outage (missed cycles, downtime) is that side's own loss; the
clock does not pause. Exchange holidays and market-wide halts pause both sides
equally.

## 6. Transparency and audit

6.1 Both sides expose their account to the scorekeeper via **read-only** use of API
keys (account + positions endpoints only). The scorekeeper never places orders.

6.2 Each side keeps a decision log adequate to answer, after the fact, "why did the
bot take this trade?" (For SCALPEL this is the committed scan/analysis/validation/
execution journal; for GPT-DAY, its own logs.) Logs for any disputed trade are shared
with the Commissioner on request.

6.3 A nightly scoreboard publishes, for both bots: equity, total return, day P&L,
max drawdown, annualized Sharpe, and open positions at the snapshot time.

## 7. Scoring

7.1 **Primary metric: total return** over the round (final equity ÷ $50,000 − 1).

7.2 Tiebreakers, in order (used if final returns are within 0.25 percentage points):
higher Sharpe ratio (daily, annualized); smaller max drawdown; higher win rate.

7.3 Context metrics (reported, not scored): profit factor, number of trades, average
exposure, best/worst day.

7.4 A stopped-out bot (§4.5) or forfeiting bot (§3.3, §5.2) loses unless the other
bot also stops out earlier or worse.

## 8. Disqualification

Automatic forfeit for: trading prohibited instruments (§3.2); short exposure;
exceeding risk limits in §4 on more than three occasions; human intervention in
trading decisions (§5.1); undisclosed strategy changes mid-round (§5.2); or
tampering with the other bot's account, data, or infrastructure.

## 9. Result

9.1 The winner is declared the day after the round ends, with a final report showing
the full equity curves, all scored metrics, and each bot's complete trade list.

9.2 One 30-day round is a small sample. The declared winner earns bragging rights,
not a claim of a superior AI; the final report must include both bots' risk-adjusted
figures alongside the headline return.

## 10. Round 2 — swing bots

Same rules, with these substitutions: §3.3 (no-overnight rule) is replaced by a
**maximum holding period of 15 trading days** per position; §4.4 (daily halt) is
waived; overnight and weekend holding is expected. All other sections apply
unchanged.

---
*Agreed before the opening bell by both AIs' operators. Good luck — may the better
trader win.*
