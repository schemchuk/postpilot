# 020. Pause Ukrainian LinkedIn posts

Status: ACCEPTED (Volodymyr, 2026-11-25)

## Context

Every approved item is published on LinkedIn twice, as two separate posts: German (`linkedin`) and
Ukrainian (`linkedin-ua`, ADR 009). Own videos (ADR 019) and opt-in HeyGen video posts (ADR 012 amendment)
do the same with `linkedin-ua-video`. The job search targets Germany first (Ukraine only from 2029-01-01).
On 2026-11-25 Volodymyr decided to stop the Ukrainian LinkedIn posts for now. The stop is temporary,
so it must be cheap to undo.

## Decision

1. **One switch in code.** `postpilot.channels.paused.PAUSED_CHANNELS = frozenset({"linkedin-ua",
   "linkedin-ua-video"})`. A paused channel is not built: `build_channels` skips `linkedin-ua`, the
   publish-reels job skips `linkedin-ua-video`. Undo = remove the names in one small PR. No env variable,
   so no workflow or secret change.
2. **Everything else stays.** Volodymyr still writes in Ukrainian in Telegram; generation still produces
   UA and DE variants (the UA text is the original or the base for review). DE LinkedIn, Threads (DE),
   Reels and the HeyGen pipeline are unchanged.
3. **No holds, no retries.** Own-video items do not wait for a paused channel (it is not a missing
   target). Items with an unfinished `linkedin-ua` publication are not retried; existing rows stay as
   history.
4. **Preview tells the truth.** The Telegram preview lists only active target channels and marks the UA
   text `UA (not published: LinkedIn UA paused)`.
5. Engagement (IMAP, replies) and `/stats` keep reading past UA posts; no change.

## Consequences

- Half the LinkedIn posting volume per item; no Ukrainian presence on LinkedIn while paused.
- Resuming does not republish items approved during the pause.
