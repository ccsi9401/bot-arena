# The ringer and its token

The entire fleet's schedule hangs on ONE external heartbeat:
cron-job.org POSTs every 15 minutes to

    https://api.github.com/repos/ccsi9401/bot-arena/actions/workflows/bell.yml/dispatches

with body `{"ref":"main"}` and an `Authorization: Bearer <PAT>` header.
bell.yml then dispatches whatever its ET bucket table says is due.
GitHub's own cron for this repo died 2026-08-29 and never recovered
(re-arm commit b1ff9dd) — there is no fallback schedule. If the ringer
stops, EVERYTHING stops: no scalpel cycles, no steward rebalance/pulse,
no glider session, no scoreboard, no learner.

## Token ledger

| Token | Lives in | Expires | Purpose |
|---|---|---|---|
| `arena-2027` (fine-grained: bot-arena + bot-arena-board; Actions RW + Contents RW) | 1) cron-job.org bell job `Authorization` header, 2) bot-arena Actions secret `BOARD_TOKEN` | **2027-08-31** | both duties: dispatch bell.yml + push phone boards |

Consolidated 2026-08-31: the old pair — `bell-ringer` (fine-grained, was to
die 2026-11-06) and `board publisher` (classic public_repo, was to die
2026-11-03) — were replaced by this single token and deleted. The calendar
reminder lives on 2027-08-27. One token, one bell job, one reminder.
Also deleted the same day: three orphan cron-job.org jobs (`ring scoreboard`,
`ring steward pulse`, `ring steward friday cycle`) that predated bell — two
had been failing with HTTP errors since creation and one would have
double-run the scoreboard daily alongside bell's 17:15 bucket.

## Rotating the ringer PAT

1. GitHub → Settings → Developer settings → Personal access tokens.
   Two workable types:
   - **Fine-grained (recommended):** Repository access = Only select
     repositories → `ccsi9401/bot-arena` (add `bot-arena-board` if this
     token will also serve as BOARD_TOKEN). Permissions → Repository →
     **Actions: Read and write** (Metadata: read comes automatically;
     BOARD_TOKEN duty additionally needs **Contents: Read and write**).
     Expiry: the maximum offered (typically 1 year). Requires an annual
     rotation — keep the calendar reminder alive.
   - **Classic, no expiration:** scope `repo` (safe superset; `public_repo`
     may suffice for these public repos — verify with a test ring before
     trusting it). Never expires; broader blast radius if leaked. Fits a
     "never think about this again" preference.
2. Copy the new token ONCE into the cron-job.org job: edit the job →
   headers → replace the value after `Bearer `. Save. Do not change URL,
   body, method, or schedule.
3. If rotating BOARD_TOKEN too: bot-arena → Settings → Secrets and
   variables → Actions → BOARD_TOKEN → update value.
4. Verify (takes ≤16 min, zero risk):
   - cron-job.org job History: next execution shows **204**.
   - github.com/ccsi9401/bot-arena/actions: a `bell` run at the next
     quarter-hour, conclusion success.
   - If BOARD_TOKEN changed: next steward/glider/scoreboard run's
     "Publish ... page" step is green, and the board page timestamp moves
     (https://ccsi9401.github.io/bot-arena-board/).
5. Update the token ledger above and move the calendar reminder to
   (new expiry − 4 days). Delete the old PAT in GitHub once the first 204
   lands.

## Backstops

- cron-job.org job → notifications on failure = ON. An expired token turns
  into 401 rows and an email instead of a silently frozen fleet.
- Manual override, no token needed on this machine: edit `ops/kick.json`
  and push — kick.yml dispatches with the run's own GITHUB_TOKEN.
- Token-free forever alternative (not built): a droplet cron force-pushing
  a tick branch via a write **deploy key** (deploy keys never expire),
  with bell triggered on that push. Worth it only if PAT rotation ever
  actually bites.
