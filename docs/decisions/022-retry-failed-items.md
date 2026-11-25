# 022. Retry failed items from Telegram

Status: ACCEPTED (Volodymyr, 2026-11-25)

## Context

An item moves to FAILED when any target channel ends in a definite error (`publications.status =
'FAILED'`). `core/states.py` already allows FAILED -> PUBLISHING and FAILED -> SKIPPED, and the
`Publisher` already skips published channels and retries the rest. But no job ever selects a FAILED item
again: `list_unfinished_item_ids` returns PUBLISHING items and FAILED items whose only failed rows are paused
channels (ADR 020). On 2026-11-25 reel 002 failed on Instagram twice (missing ffmpeg, then an intermittent
rupload error) and was moved FAILED -> PUBLISHING by hand both times.

Today a failure is visible only as a red GitHub Actions run (text jobs) or a Reels alert line; the reason
stays in `publications.error`, and the job log shows only the exception type.

Options:
- A. **Retry button in Telegram.** On failure the bot sends one message with the failed channels and their
  sanitized errors, plus `Retry` and `Skip`. Chosen by Volodymyr: a person decides, so a permanent error
  (bad token, rejected media) is not hammered, and nothing is published twice.
- B. Automatic retry with a limit. No human step, but repeats permanent errors and needs a backoff store.
  Rejected.
- C. Both. More code for little gain at this volume (a few items a week). Rejected.

## Decision

1. **Failure message.** When `publish_item` leaves an item in FAILED, the job sends one Telegram message:
   `Publishing failed · <item short id> · <source>` and one line per failed channel
   `<channel>: <publications.error>` (the stored text is already sanitized by the channel adapters; it is
   cut to 300 chars per line). Buttons `Retry` (`retry:<item_id>`) and `Skip` (`skipfail:<item_id>`).
   Sent once per transition to FAILED (a later failure after a retry sends a new message). A Telegram
   send failure is logged and does not fail the job.
2. **Retry.** In poll-telegram, `retry:<id>` moves the item FAILED -> PUBLISHING (conditional, so a second
   press or a stale message gets `Already handled`) and publishes it in the same run with the existing
   Publisher. Text targets run there; for own videos the Reels targets stay held for publish-reels, which
   already picks PUBLISHING own_video items on its next hourly run. Only failed or missing channels run;
   published ones are never repeated (existing idempotency, ADR 013).
3. **Skip.** `skipfail:<id>` moves FAILED -> SKIPPED. Published channels stay published; the item is closed.
4. **Only Volodymyr.** Both callbacks come through the webhook inbox (ADR 021), which already drops other
   users.
5. **Out of scope.** HeyGen video publications (`video_publications`) keep the existing `--retry` flag of
   publish-reels; paused channels keep ADR 020 behaviour (never retried).

## Consequences

- No more manual DB transitions after a failed publication.
- Failure reasons reach Telegram, not only the database.
- One new message type and two callback prefixes; no migration, no workflow or secret change.
