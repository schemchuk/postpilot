# 014. Instagram and Facebook Reels publishing

Status: ACCEPTED (Volodymyr, 2026-11-19)

## Context

ADR 012 stores the approved, labelled MP4 in Postgres (`video_renders.video`, status APPROVED).
The spec (F5.4, F6.4) requires a second Telegram approval before Reels, separate Instagram and
Facebook publishing (no auto-share or crossposting), and the AI label on every channel (ADR 006).
There is no public file host: GitHub Actions runners are ephemeral.

Meta documentation checked on 2026-11-19:
- Instagram Content Publishing: `upload_type=resumable` (bytes sent to
  `rupload.facebook.com/ig-api-upload/<container-id>`) is "only for apps that have implemented
  Facebook Login for Business". With Instagram Login a Reel needs a public `video_url`.
  Container `status_code` is `EXPIRED`, `ERROR`, `FINISHED`, `IN_PROGRESS` or `PUBLISHED`;
  only `media_publish` publishes and returns the media ID. `is_ai_generated=true` is accepted on
  container creation. Limit: 100 API posts per 24 h.
- Facebook Reels Publishing API: `POST /{page-id}/video_reels` with `upload_phase=start` returns a
  `video_id`; bytes go to `rupload.facebook.com/video-upload/<version>/<video-id>` (resumable via
  `offset`); `upload_phase=finish&video_state=PUBLISHED&description=...` publishes. The reel's ID
  is the `video_id`. `GET /{video-id}?fields=status` reports uploading, processing and publishing
  phases. Needs a Page access token with `pages_show_list`, `pages_read_engagement`,
  `pages_manage_posts`. Reels only on Pages, 3-90 s, 9:16, min 540x960. Limit: 30 per 24 h.
  No AI-label parameter is documented.

## Decision

1. **Upload from the database, no public hosting.** Both channels send the MP4 bytes from
   `video_renders.video` to `rupload.facebook.com`. Instagram therefore uses the Instagram API with
   Facebook Login for Business (host `graph.facebook.com`), which requires the Instagram
   professional account to be linked to Volodymyr's Facebook Page.
2. **Separate credentials.** Two token providers, `facebook` (Page access token) and `instagram`
   (token used for the IG user ID), each imported on its own and checked by `check-tokens`
   (`debug_token`; alert before expiry, ADR 005). Two independent API flows, no crossposting and
   no Instagram auto-share to Facebook. Exactly which token type Instagram accepts (Page token or
   long-lived user token) is confirmed live at the secret gate and recorded in the runbook.
3. **Only approved videos.** A new job `publish-reels` publishes renders in status APPROVED only.
   One render per run; Instagram and Facebook fail and retry independently.
   A new workflow runs it every 15 minutes plus `workflow_dispatch`.
4. **Idempotency per (render, channel)** in a new table `video_publications`, with the same claim,
   stale-claim and `remote_ref` rules as `publications` (ADR 013). The remote reference is saved
   before any call that can publish:
   - Facebook: save `video_id` after `start`. The reel ID is the `video_id`, so recovery never
     duplicates: read `status`; published -> done with that ID; upload incomplete -> resume upload
     at `bytes_transfered`, then `finish`; processing or publishing in progress -> pending;
     error -> new session.
   - Instagram: save the container ID after creation, upload, poll `status_code` (bounded, about
     5 minutes per run; otherwise pending for the next run), then `media_publish`. Recovery by
     status as in ADR 013: `FINISHED` -> publish again with the same container; `PUBLISHED` ->
     find the media ID among the account's media created after the claim time by exact caption
     equality, single match only, else pending for manual reconciliation; `IN_PROGRESS` -> pending,
     and a container stuck for over an hour is abandoned for a new one (an unpublished container
     publishes nothing); `ERROR` or `EXPIRED` -> new container.
5. **Caption and label.** Caption = the spoken German script (`video_renders.script`), a blank line,
   then `KI-generierter Avatar / AI-generated avatar`. No visible item marker. Instagram also sends
   `is_ai_generated=true`. Facebook has no documented AI flag; the burned-in label and the caption
   line cover it. Channels reject a render without the burned-in label flag or caption line.
6. **Telegram reports** the result per channel (published link or failure), as for text channels.

## Consequences

- Volodymyr must have a Facebook Page (not only a personal profile) and an Instagram professional
  account linked to it. If he wants Instagram without a Page, the alternative is Instagram Login
  plus a short-lived public URL for the MP4, which needs a file host (rejected for now).
- The Meta app needs the Facebook Login for Business product and the Page/Instagram use cases;
  whether the existing Threads app `postpilot-2` can carry them or a second app is needed is checked
  at the secret gate. Posts made by an app in Development mode may be visible only to app roles; the
  first live post is checked logged out, and switching the app to Live mode is a separate step.
- Migration 0009 (`video_publications`), a new workflow (needs Volodymyr's OK per AGENTS.md; standing
  merge authorization covers it once the secrets exist), two channel adapters, two token providers.
- Split into T14a (table, job, workflow, Facebook Reels) and T14b (Instagram Reels) to keep PRs small.
- Open risk: Instagram recovery after `PUBLISHED` relies on caption equality, as Threads does.
- Secret gate result (2026-11-20, T14b): Instagram accepts a long-lived USER token of app
  `postpilot-2` (Facebook Login for Business; scopes `instagram_basic`,
  `instagram_content_publish`, `pages_show_list`, `pages_read_engagement`, `business_management`).
  `debug_token` reports no fixed expiry but a `data_access_expires_at` about 90 days out; the token
  is stored with that date so `check-tokens` warns 14 days ahead. Meta labels "API setup with
  Facebook login" as legacy (still supported, no new features); revisit if it is deprecated.
