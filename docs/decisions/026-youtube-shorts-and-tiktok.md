# 026. YouTube Shorts and TikTok video channels

Status: PROPOSED (Claude Code, 2026-11-26; Volodymyr's answers of 2026-11-26 included: factory reels go to
TikTok and YouTube, category Science & Technology for all, YouTube also without a description (default title), HeyGen
renders stay off both. Would amend the spec outputs and the video channel sets of ADRs 019, 023 and 024)

## Context

On 2026-11-26 Volodymyr connected two new accounts (runbook "YouTube Shorts setup", "TikTok setup"):

- **YouTube** channel `shemchuk`, Google Cloud project `postpilot-515916`, OAuth app in production (unverified,
  one user), scope `youtube.upload`, refresh token in `.env`. Checked in the docs the same day: `videos.insert`
  has its own bucket of 100 calls per day; uploads from unverified API projects are not restricted to private,
  so no YouTube audit is needed. A vertical video up to 3 minutes is a Short; the API has no Shorts flag.
  `snippet.title` is at most 100 characters, `snippet.description` at most 5000 bytes, both without `<` and
  `>`. `status.containsSyntheticMedia` discloses realistic altered or synthetic content. `thumbnails.set`
  accepts `youtube.upload`; for a Short it must run after processing has succeeded and needs a channel with
  intermediate features (phone verification, not yet confirmed). A resumable upload session answers a status
  query with the original `201` and the video resource once the upload has completed; an expired session
  answers `404`.
- **TikTok** account `@volodymyr.shemchuk`, developer app "Shemchuk Postpilot" (Draft, not submitted) and
  sandbox `postpilot-test`. Login Kit (redirect = the existing `/callback/` page) and Content Posting API with
  Direct Post; scopes `user.info.basic`, `video.publish`, `video.upload`. Sandbox test: `creator_info/query`
  ok; a Direct Post (`FILE_UPLOAD`, `SELF_ONLY`) reached `PUBLISH_COMPLETE`, but only after the account was
  made private: unaudited clients post only `SELF_ONLY` to private accounts (403
  `unaudited_client_can_only_post_to_private_accounts`). The audit rules say apps must not be "for private
  or personal use", so approval is uncertain. Direct Post takes `title` (caption), `privacy_level` (one of
  `creator_info.privacy_level_options`), `disable_comment|duet|stitch`, `video_cover_timestamp_ms` and
  `is_aigc`; TikTok's UX rules forbid preselecting privacy, comment, duet or stitch. The inbox flow
  (`video.upload`) sends a draft the owner finishes in the app; it takes no caption or cover, allows at most 5
  pending shares per 24 h, and the docs do not say whether the unaudited restrictions apply to it. Access
  tokens live 24 h, refresh tokens 365 days and may rotate on refresh. About 15 posts per creator per day.

postpilot already publishes the same approved MP4 to Instagram and Facebook Reels from three sources:
HeyGen renders (S1, ADR 012/014), own videos from `/work` (S3, ADR 019/023) and factory reels (S4, ADR 024).
The video channel adapters upload bytes, keep one `video_publications` row per (item, channel) and recover by
remote status (ADR 014).

## Decision

1. **Two new video channels, `youtube` and `tiktok`,** implemented as further video channel adapters next to
   `instagram` and `facebook`. They join the same publishing path (`publish-reels`) and the same
   idempotency table. The core stays network-agnostic.

2. **Which items go where.**
   - S3 own videos: Instagram, Facebook, YouTube, TikTok.
   - S3 videos without a description (ADR 023): Instagram, Facebook, TikTok (empty caption) and YouTube with
     the default title (decision 3) (Volodymyr, 2026-11-26).
   - S4 factory reels (Volodymyr, 2026-11-26): LinkedIn DE, Threads, IG, FB as in ADR 024, plus YouTube and
     TikTok, with the verbatim text and the cover. Under ADR 025 these become the factory's allowlisted
     channels.
   - S1 HeyGen renders: unchanged, no YouTube and no TikTok (Volodymyr, 2026-11-26).
   - The Telegram preview or the factory intake notice lists every target channel.

3. **YouTube text.** Title = the first line of the German text if it fits 100 characters, else the first
   sentence if it fits, else the default title `Volodymyr Shemchuk · <DD.MM.YYYY>` (Berlin date of
   publishing). The text is never truncated. Videos without a description get the default title and an empty
   description (only the AI label line when AI generated). Description = the full German text plus the AI label line when the item is AI
   generated. `<` and `>` are replaced by `‹` `›` because YouTube rejects them. `privacyStatus=public`,
   `selfDeclaredMadeForKids=false`, `categoryId=28` (Science & Technology, also for the factory cartoons),
   `containsSyntheticMedia` from the item's AI flag.
   Factory reels: `post_de.txt` gives title and description by the same rule, so the text stays verbatim. An
   optional release asset `title_de.txt` (at most 100 characters), if the factory adds it, is used as the
   title instead; without a fitting title the default title is used and the intake notice says so.

4. **YouTube upload and idempotency.** Resumable upload of the bytes. The session URI is stored in
   `remote_ref` before the first byte is sent. Recovery queries the session: `308` resumes at the reported
   offset, `200/201` takes the video ID from the returned resource (no second upload), `404` starts a new
   session. Once the ID is known it replaces the session URI. Cover: `thumbnails.set` with the item's cover
   (factory `cover.png`, else none) after `processingDetails.processingStatus=succeeded`, best effort like
   the Facebook cover (ADR 024 decision 7): a failure is a warning, the Short stays published.

5. **TikTok has two modes, chosen by one config value `TIKTOK_MODE`.**
   - `draft` (default until the audit passes): the inbox flow (`video.upload`). The video lands in Volodymyr's
     TikTok inbox; the Telegram result line carries the caption to copy and says "finish in the TikTok app".
     Before this mode is enabled, the sandbox must show that an inbox upload works for a public account. If it
     does not, `tiktok` stays in `PAUSED_CHANNELS` (ADR 020 switch) until the audit.
   - `direct` (after the audit): Direct Post with `FILE_UPLOAD`. The caption is the German text (TikTok limit
     2200 characters, longer text skips TikTok with a note) plus the AI label line; `is_aigc` from the item's
     AI flag; `video_cover_timestamp_ms=0` (frame 0 is the factory cover). To meet TikTok's UX rules the
     Telegram preview gets a TikTok row: privacy buttons built from `creator_info.privacy_level_options` and
     comment, duet and stitch toggles, none preselected. Approve waits until a privacy level is chosen. The
     choice is stored per item.
   - Factory reels (approved without a tap, ADR 024) in `direct` mode: the intake notice gets the same TikTok
     row. A choice made before `release_at` posts TikTok directly at release time with the other channels. No
     choice by `release_at` sends the reel to the TikTok inbox as a draft instead (and says so), so the other
     channels are never held. In `draft` mode factory reels always go to the inbox, and the release-time
     notice carries the caption to copy.
   - Idempotency for both: `publish_id` is stored before the upload. Recovery reads `status/fetch`:
     `PUBLISH_COMPLETE` done, processing states pending, `FAILED` or an expired upload URL (1 h) starts again.

6. **Tokens (ADR 005).** Two token providers, `youtube` and `tiktok`, stored encrypted in the `tokens` table.
   - `youtube`: the refresh token is imported once from `.env` (`postpilot youtube-import-token`); access tokens
     (1 h) are fetched per run and not stored. `check-tokens` tries a refresh daily and alerts on failure
     (revoked consent). Renewal: the same local consent run as in the runbook.
   - `tiktok`: the refresh token is imported once (`postpilot tiktok-import-token`). Every refresh writes back a
     rotated refresh token. `check-tokens` alerts 30 days before the 365-day expiry; renewal uses the existing
     Telegram `/renew` flow with the provider in the signed state (Login Kit redirects to `/callback/`).
   - `TIKTOK_ENV=sandbox|production` picks the key pair, so the same code runs the sandbox demo and later
     production.
   - Client IDs and secrets go to GitHub Secrets with the usual `.env` pipe command when the task starts.

7. **TikTok audit.** After the `direct` mode works in the sandbox, Claude records the demo (Telegram preview with
   the TikTok row, approve, the private post in TikTok), replaces the placeholder video, and Volodymyr submits
   with the honest description already in the form. A rejection changes nothing: `draft` mode stays.

8. **Limits and failures.** Both channels publish at most one item per run like the Reels channels, fail and
   retry independently (ADR 022 Retry/Skip), and never block Instagram or Facebook. A quota or rate error
   (`quotaExceeded`, `rate_limit_exceeded`, `spam_risk_too_many_posts`) is held for the next run, not
   failed.

## Consequences

- Spec outputs gain YouTube Shorts and TikTok; the video channel sets of S3 (ADRs 019, 023) and S4 (ADR 024)
  grow by two. Factory reels' YouTube and TikTok posts follow the factory's release time; only the TikTok
  privacy choice may need a tap, and missing it never delays the other channels.
- The factory is told (via its requests doc) about the new channels and the optional `title_de.txt`.
- One migration: per-item TikTok settings (privacy, comment, duet, stitch) for `direct` mode.
- New secrets: `YOUTUBE_CLIENT_ID`, `YOUTUBE_CLIENT_SECRET`, `TIKTOK_CLIENT_KEY`, `TIKTOK_CLIENT_SECRET` (and the
  sandbox pair while testing); refresh tokens live only in the encrypted `tokens` table.
- TikTok depends on an audit we may not pass; until then TikTok is either a one-tap finish in the app or paused.
- YouTube custom covers depend on intermediate channel features; without them YouTube picks a frame.
- Proposed tasks, each one PR, after acceptance:
  - T28a `youtube` token provider and import, `check-tokens`.
  - T28b `youtube` channel: title rules, resumable upload with session recovery, AI disclosure, wiring into S3
    and factory reels (`title_de.txt`).
  - T28c YouTube cover via `thumbnails.set`, best effort.
  - T29a `tiktok` token provider (rotation, `/renew`), `TIKTOK_ENV`, sandbox check of the inbox flow on a public
    account.
  - T29b `tiktok` channel, `draft` mode, wiring into S3 and factory reels.
  - T29c `direct` mode: Telegram TikTok row (preview and factory intake notice), per-item settings, Direct Post,
    inbox fallback for factory reels without a choice; then the demo recording and the audit submission
    (Volodymyr submits).
