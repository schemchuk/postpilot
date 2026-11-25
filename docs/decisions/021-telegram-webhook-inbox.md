# 021. Telegram webhook inbox and a soft humanizer gate for own videos

Status: ACCEPTED (Volodymyr, 2026-11-25; receiver host: Vercel Python function, the recommended option)

## Context

Live run of the beekeeper reel 002 on 2026-11-25 (`/work ai`, 565-char interview, then Generate):

1. **Button presses are lost.** Generate was pressed six times between 13:18 and 14:40 UTC. Measured
   with a per-second peek at `getUpdates` (offset 0, nothing confirmed): every `callback_query` update
   left the queue 1.7 to 2.5 minutes after the press (update 081: in 13:58:26, gone 14:00:07; update
   084: in 14:36:45, gone 14:39:19) while no postpilot run was active and `bot_state.telegram_offset`
   did not move. Text messages stayed in the queue for minutes and were all handled. The bot token was
   revoked at 14:07 UTC to rule out a foreign consumer; presses kept disappearing afterwards. Only
   presses that a running poll fetched within that window arrived (update 082: run 14:10, pressed
   14:11:16). Telegram does not document this, but the behaviour is reproducible: with `poll-telegram`
   every 5 minutes (ADR 017) most button presses never reach postpilot, and the press shows no reaction.
   Earlier presses worked by luck of timing.
2. **The humanizer blocks own videos.** When a press did arrive, generation ended in
   `Generation failed (humanizer)`. A local repro with the same interview passed the German gate in 1 of
   2 runs. The reviewer flagged Volodymyr's own phrases from the interview ("Рутину боту, мені сон",
   "квітуче поле") as slogans and sales language, and once filled `unsupported_facts` with entries it
   marked itself as covered ("gedeckt, kein Problem"), which fails the gate automatically (ADR 007).
   Both languages must pass, so one press succeeds roughly one time in three.
3. Volodymyr (beekeeper request, 2026-11-25) wants visible feedback on every button press and less
   waiting between steps. Today every step waits up to 5 minutes for the next poll.

## Options for 1

- A. Poll more often (every minute). Still misses presses (runner start 20-60 s plus the 1.7-minute
  window), 5x Actions runs, and the pre-check would wake Neon more often. Rejected.
- B. Long-poll for ~4.5 minutes inside every run. Near-continuous Actions use for a bot, gaps between runs
  remain, borderline with GitHub's Actions terms. Rejected.
- C. Always-on VM for the bot. A new server to run and patch; against ADR 001 (no server of our own). Rejected.
- D. **Telegram webhook to a small serverless receiver that stores every update in Postgres and triggers
  `poll-telegram`.** Telegram pushes each update within a second and retries until the receiver answers
  200, so nothing expires. Chosen.

## Decision

1. **Receiver.** A serverless HTTPS function (host: open question below) is the bot's webhook
   (`setWebhook` with `secret_token`, `allowed_updates = ["message", "callback_query"]`). Per request it:
   (a) rejects a missing or wrong `X-Telegram-Bot-Api-Secret-Token` with 401;
   (b) ignores updates from any user other than `TELEGRAM_ALLOWED_USER_ID` (answers 200, stores nothing);
   (c) inserts the raw update into `telegram_inbox (update_id PK, received_at, payload jsonb, processed_at)`
       with `ON CONFLICT DO NOTHING`; if the insert fails it answers 500, so Telegram retries;
   (d) for a `callback_query` calls `answerCallbackQuery` right away with `Received, working on it`
       (best effort), so every press shows feedback within a second;
   (e) dispatches `poll-telegram` via `workflow_dispatch` (best effort; the hourly run is the safety net);
   (f) answers 200. It holds no business logic: no AI calls, no state changes, no publishing.
2. **poll-telegram reads the inbox, not `getUpdates`.** It handles unprocessed rows in `update_id` order
   through the existing `ApprovalHandler`, and sets `processed_at` per row; a failing row is logged and
   retried on the next run like today. Existing per-update idempotency (own-video claims, state checks)
   stays. Processed rows are deleted after 7 days (they hold message text). Handlers no longer answer
   callbacks a second time when the receiver already did (a late answer only fails silently, as now).
3. **Trigger and schedule.** Each update dispatches one run within seconds; the workflow's concurrency
   group already serialises runs, and any run drains all pending rows, so a cancelled pending run loses
   nothing. The cron-job.org job for `poll-telegram` moves from every 5 minutes to hourly at :00 (the
   full-run duties of ADR 017 decision 5 and a safety net). The `getUpdates` pre-check is removed; an
   hourly run opens Neon once an hour, each update wakes it once, which is below today's usage.
4. **Rollback.** `deleteWebhook` makes `getUpdates` work again; reverting the T23b PR and the cron
   interval restores the ADR 017 flow. Updates received while switching stay in the inbox or in
   Telegram's queue, nothing is lost.
5. **Soft humanizer gate for own videos (S3).** For S3 only, the interview is Volodymyr's own text and he
   approves every post in Telegram anyway. So:
   (a) the reviewer gets the interview as the author's own wording: phrases taken from it are never
       reported as AI tells or unsupported facts;
   (b) for every source, the reviewer prompt states that `unsupported_facts` holds only claims that are
       not supported, quoted from the post, with no comments, and that a covered claim is left out. The
       gate keeps treating any entry as a failure (no guessing from free text);
   (c) if after 3 rounds a language still fails only the AI review (hard rule checks pass: language,
       length, no dashes, no markdown, URLs, hashtags), S3 keeps the best candidate (lowest `ai_score`)
       and the preview says `Humanizer: not passed (score N), check the text`. Hard rule failures still
       end in `Generation failed (humanizer)`.
   S1 (weekly GitHub draft, no human-written source) keeps the strict gate of ADR 007.

## Open question for Volodymyr

Host of the receiver (needs an account in your name; free tier is enough):
- Vercel Python function in this repo (recommended: same language, reuses `postpilot` code and tests,
  deploys from GitHub), or
- Cloudflare Worker (TypeScript, separate small code base).

## Consequences

- Button presses are never lost and show feedback at once; a step's result arrives in about 1-3 minutes
  instead of up to 5-10.
- A new runtime piece outside GitHub Actions (amends ADR 001) with its own secrets: bot token, webhook
  secret, `DATABASE_URL`, allowed user ID and a GitHub token that may only dispatch postpilot workflows.
- Fewer Actions runs (hourly cron instead of every 5 minutes).
- S3 posts may reach the preview with a humanizer warning; Volodymyr decides.
- Interim until T23b ships: a press survives only if a run fetches it within ~1.5 minutes.
