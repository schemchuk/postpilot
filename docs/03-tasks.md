# Tasks: postpilot

Status: APPROVED by Volodymyr (2026-11-18).
`feat/<id>-<slug>`, one PR per task under about 400 non-test lines, CI green before merge.
Every task ships tests; no real network in CI. "Secret gate" = Volodymyr supplies a secret first;
Claude Code gives a step-by-step guide with a verified link at that moment (AGENTS.md).
Tasks touching workflows or secret handling need Volodymyr's explicit OK before merge.
Every task from T29c on has an `ADRs:` line naming the ADRs (and AGENTS.md hard constraints) it touches.
Each binding rule from them that the task's code reaches becomes its own Accept item with a test; a rule that
is not written into Accept is not checked in review. After each task series, an ADR audit task compares the
ADRs with the code (as T27e1 did for ADR 025).

## Phase 0: foundation

**T01 Skeleton + CI.** Package layout from docs/02-architecture.md, pyproject (ruff, mypy, pytest),
pre-commit with secret scan, GitHub Actions CI, README stub.
Accept: `pytest`, `ruff`, `mypy` green locally and in CI; a deliberately leaked fake key fails the scan.
Needs OK: workflow.

**T02 Store + models + state machine.** Postgres access, migrations, `Item`/`Variant`/`Publication`,
state transitions (ADR 004). Secret gate: `DATABASE_URL` (provider picked here, free tier limits checked).
Accept: illegal transitions raise; only APPROVED can enter PUBLISHING; migration up/down tested.

**T03 Publisher + fake channel + Channel protocol.** Idempotent fan-out, per-channel retry, contract
test suite for adapters.
Accept: rerun posts nothing twice; one failing channel does not block others; crash-after-send test.

## Phase 1: approval and operations

**T04 Telegram notifier + approval.** New bot, allowed-user filter, preview, buttons (approve, edit,
regenerate, skip), approval recorded. Secret gate: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.
Accept: foreign user ignored; nothing publishes without approve; edit flow works (fake Bot API).

**T05 Token lifecycle (ADR 005).** `tokens/` TokenManager interface, encrypted `tokens` table,
`check-tokens` daily job, 7-day alerts, held-item release, silent refresh path, LinkedIn assisted renewal
(static callback page, code exchange via bot, `state` check), local fallback `auth linkedin --open`.
Secret gate: `TOKEN_ENCRYPTION_KEY`; guide to register the redirect URI in the LinkedIn app.
Accept: expiry math, refresh and failure paths tested with fakes; expired token holds items instead of
failing them; renewal releases them. Needs OK: workflow, secret handling. May be split in two PRs.

**T06 Failure alerts + job entry points.** `cli.py` commands and scheduled workflows with concurrency
groups; every failure produces a redacted Telegram alert. Needs OK: workflows.

## Phase 2: content

**T07 AI client + prompts + humanizer.** Single Claude module, German/Ukrainian prompts ported from
agent_prompts.py, fail-closed humanizer (S1 only). Accept: failing text never reaches the queue; tests
with a stubbed model.

**T08 GitHub source + series + repo-safety.** Weekly activity, project pick, no-repeat, series state,
safety gate (public, README, LICENSE; auto-fix off by default). Accept: gate tests per condition.

**T09 Short links + traffic snapshot.** Ported link tracker and daily snapshot. Secret gate:
short.io key (reuse of the old one needs Volodymyr's OK).

**T10 Telegram inbox (S2).** Poll with stored offset, UA text to item, DE translation, both shown in
preview, edit DE, publish right after approval. Accept: text unchanged apart from translation and limits.

## Phase 3: channels

**T11 LinkedIn channel (DE, UA).** Posts API client ported, length limits, held when token expired.
**T12 Threads channel (DE, short).** Secret gate: Meta developer app, Threads token (app review check).
**T13 HeyGen video.** v3 API, avatar of Volodymyr, DE voice, burned-in DE subtitles, AI label, monthly
cap, second approval. Secret gates: new `HEYGEN_API_KEY`, `HEYGEN_AVATAR_ID`, `HEYGEN_VOICE_ID`.
**T14 Instagram + Facebook Reels.** Separate credentials, container flow with polling, resumable upload.
Secret gates: IG and FB tokens. Accept: contract suite green; label present; no video without approval.
Split per ADR 014: **T14a** `video_publications` table (migration 0009), `publish-reels` job and
workflow, Facebook Page Reels channel, `facebook` token import and check (secret gate: Page token).
**T14b** Instagram Reels channel via Facebook Login for Business with resumable upload,
`instagram` token import and check (secret gate: IG token).

## Phase 4: extras and cutover

**T15 Comments + engagement.** IMAP notifications to AI reply drafts, engagement log, /stats.
Split per ADR 015 (scope: LinkedIn + Threads; IG/FB comments later): **T15a** migration 0010
`engagement_events`, Gmail IMAP source for LinkedIn notification emails, AI extraction + reply draft,
`poll-engagement` job and workflow, "Another variant" callback (secret gate: `IMAP_USER`,
`IMAP_PASSWORD`). **T15b** Threads replies via `/{media-id}/conversation`. **T15c** `/stats` and
the 90-day purge of comment text.
**T16 Fork/star watch + profile advisor.** Both ported per ADR 016: **T16a** fork/star watch as a
`poll-engagement` source (migration 0011 kinds `star`/`fork`, counts in `bot_state`, baseline without
events, Telegram message per event, `github.token` in the workflow). **T16b** profile advisor:
`/profile`, `/profile_add`, `/profile_clear`, `/advise` in `poll-telegram`, answer as a Markdown document.
**T19 Reliable trigger + one-click Threads replies** (ADR 017, inserted before T17 on 2026-11-20).
**T19a** external trigger: cron-job.org jobs call `workflow_dispatch` for every scheduled workflow,
fine-grained PAT (postpilot only, Actions read/write) stored only in cron-job.org, GitHub `schedule`
kept as fallback, no workflow change; runbook section; acceptance: for 1 hour every `poll-telegram`
and `poll-engagement` slot has a `workflow_dispatch` run (secret gate: PAT + cron-job.org account,
Volodymyr). **T19b** one-click Threads replies: migration 0012 reply columns, `eng:send:<id>`
callback in `poll-telegram` with SENDING claim, container ID stored before publish and status-based
recovery, edit by Telegram reply (verbatim), "Sent" + permalink, buttons removed; LinkedIn unchanged;
one summary line per source in `poll-engagement`. Acceptance: tests for duplicate press, crash after
publish, API/permission errors, edit-by-reply, LinkedIn unchanged; live: one real reply sent.
**T20 Weekly GitHub draft (S1 wiring)** (ADR 018, inserted before T17 on 2026-11-20: `draft-weekly`
was never implemented). **T20a** migration 0013 `weekly_drafts`, `draft-weekly` job (public repos via
`/users/{owner}/repos`, active series first, else `pick_project`; repo safety, DE+UA generation,
humanizer, tracked links, preview with source block, older S1 drafts -> SKIPPED), `draft-weekly.yml` +
cron-job.org job Wed 18:00 Europe/Berlin, `publish-due` records published S1 items (series part or
posted repo). Accept: tests for rerun in the same week, no activity, model skip (plain and in a series),
humanizer failure (no item, no row), link blocked vs allowed, short.io failure, expiry of the old draft,
recording once after publication; live: one manual dispatch produces a preview in Telegram.
**T20b** Regenerate for S1 from stored facts (failure keeps the old draft), `/series`,
`/series_start <repo> [parts]`, `/series_stop`, poll-telegram timeout 10 min. Accept: tests for
regenerate success/failure, series commands incl. invalid repo and an already active series.
T20b also fixes the first live run (ADR 018 amendment): count up to 100 commits per repo for
`pick_project` (prompt still gets at most 20 messages); generation prompts ask for plain paragraphs
without bullet lists or dashes; the failure alert names the failed step and language (no post text).
Live: temporary manual dispatch gives a preview in Telegram (Volodymyr presses Skip), workflow disabled
again afterwards.
**T17 Docs + portfolio.** README with architecture diagram, ADR index, demo, note that the LinkedIn app
is reused and why.
**T18a Take over the handmake series (ADR 018 amendment 2026-11-21).** Owner: Codex. Reviewer: Claude Code.
Migration 0014 `series_planned_parts`; `SeriesStore` reads the planned text for the current part;
`draft-weekly` uses a planned text as is (no AI, no humanizer; link gates, short links, length check
stay; source block says `planned text`); Regenerate on a planned part is refused and the preview resent;
CLI `postpilot series-import <file.json>` (series at a given part, published parts, planned parts, posted
repos, one transaction, refuses an active series). Accept: tests for a planned part (no model call,
texts unchanged except links), a series part without a planned text (generation as before), link blocked,
regenerate refused, import success, import with an active series, invalid import files; live: import on
Neon, draft on 2026-11-25 shows part 3 in Telegram.
**T18b Queued next series (ADR 018 amendment 2026-11-21, second).** Owner: Claude Code. Reviewer:
Volodymyr. Migration 0015 (`queued` status, one queued row); `SeriesStore.queue`, `get_queued`,
`cancel_queued`; promotion inside `record_part` (last part), `complete` and `stop`; Telegram
`/series_next <repo> [parts]`, `/series_next_cancel`, `/series` shows the queue. Accept: unit tests for
the commands, integration tests for promotion on each path and for the one-queued limit; live:
`/series_next postpilot 5` while handmake is active, `/series` shows it.
**T18c Run tracing (ADR 018 amendment 2026-11-21, second).** Owner: Claude Code. Reviewer: Volodymyr.
Trace lines in draft-weekly, publish-due (publisher claims, channel results, recording, series
transitions), poll-telegram (approval callbacks, state changes) and the job runner (start, end, duration,
error type, HTTP status). No text, tokens or query strings. Accept: tests that trace lines appear and
that no post text or secret is printed; live: logs of the 2026-11-25 draft and 2026-11-27 publication.
**T18 Cutover.** Run both agents side by side for two weeks, then disable and delete work_in_Linkedin
(Volodymyr confirms). Also delete social_bridge and "lumara" Projects if not yet done.
T18 is not before 2026-12-11 (end of the old agent's handmake series, ADR 018 amendment): copy the old
agent's posted repos into `posted_repos`, `/series_start postpilot 5`, enable `draft-weekly.yml` and
cron-job.org job 8906614 (first draft Wed 2026-12-16).
T18 owner: Claude Code alone (Volodymyr, 2026-11-20: Codex is not available by then). Claude Code
implements and checks; Volodymyr reviews and gives the OK before any merge (author never merges own work).
T18 DONE 2026-11-24 (no code left): posted repos imported and draft-weekly + cron enabled in T18a (2026-11-21); old agent stopped, deleted after backup, repo archived; social_bridge and empty Projects removed (2026-11-21); the two-week side-by-side run was dropped by Volodymyr to avoid duplicates. Remaining, Volodymyr's action: send `/series_start postpilot 5` in Telegram after part 5 of the handmake series (2026-12-11).
**T21 Own video via `/work` (ADR 019, source S3).** Owner: Codex. Reviewer: Claude Code.
**T21a** Intake + generation + preview: migration 0016 (`own_videos`: item_id, file_id, file_unique_id
unique, size, duration, width, height, ai flag; item source S3); poll-telegram handles a video/mp4
document with caption `/work [ai] [text]`, text replies (message or `.txt`) to the "Video received"
message, `Generate` button; 20 MB and duplicate refusals; one Claude call UA+DE + humanizer; preview
(video by file_id, texts, `AI label: yes|no`, target channels) with Approve/Edit/Regenerate/Skip;
`render-video` skips S3. Accept: tests for caption parsing (`/work`, `/work ai`, text, no text),
foreign sender ignored, over 20 MB, duplicate video, `.txt` reply, joined replies, generation and
humanizer failure (no item), regenerate, no S3 render; live: one test video gives a preview.
**T21b** LinkedIn video posts for S3: initializeUpload, 4 MB parts with ETags, finalizeUpload, poll
`AVAILABLE` (bounded, else pending for the next run), URN stored before the post, post with
`content.media.id`, AI text line only when flagged. Accept: tests for upload success, part failure retry,
`PROCESSING_FAILED`, rerun reuses stored URN, no duplicate post; live: one S3 post on LinkedIn.
**T21c** Reels + Threads for S3: IG/FB read bytes via Telegram `getFile`, ffmpeg fit to 1080x1920 (scale + pad,
no crop) when not 9:16 or below 540x960, outside 3-90 s skip Reels with a preview warning, label rules per ADR 019 decision 6, Threads shortened DE text. Accept:
tests for label/no label, skipped Reels, getFile failure; live: one S3 reel.

**T21d** HeyGen avatar video on LinkedIn (Volodymyr asked 2026-11-24; ADR 012 amendment 2026-11-24):
the HeyGen video preview gets a toggle `Also on LinkedIn: no|yes` (default no, Volodymyr's tap only,
changeable only while the render is READY, stored as `video_renders.linkedin`, migration 0017). With yes,
`publish-reels` publishes a second LinkedIn post per language (`linkedin-video` DE, `linkedin-ua-video` UA in
`video_publications`) with the approved render (bytes from `video_renders`, T21b upload path), text = label line
`KI-generierter Avatar / AI-generated avatar` + the approved variant text, marker `postpilot:<uuid>/<lang>/video`.
The text posts stay unchanged. Accept: tests for toggle default no, toggle on/off, toggle refused after
approve/reject and from another user, upload from a render, label always present, no video post when off,
rerun reuses the stored URN (no duplicate post), marker distinct from the text post;
live: one HeyGen video post on LinkedIn.

**T22 Pause UA LinkedIn (ADR 020).** Owner: Codex. Reviewer: Claude Code. Must be merged before the
publication on Fri 2026-11-27 09:00 UTC. New `channels/paused.py` with `PAUSED_CHANNELS`; `build_channels`
skips paused names; `_TEXT_TARGETS` and the publish-reels LinkedIn video channels drop paused names (own
videos are not held); Telegram previews (text, own video, HeyGen toggle) list only active channels and mark
the UA text `UA (not published: LinkedIn UA paused)`. Accept: tests that an approved S1/S2 item publishes
only `linkedin` + Threads and reaches PUBLISHED, an S3 item is not held on `linkedin-ua`, a render with
`Also on LinkedIn: yes` publishes only `linkedin-video`, an item with an old FAILED `linkedin-ua` row is not
retried, preview text; no workflow change; live: part 3 of handmake on 2026-11-27 appears on LinkedIn DE only.
T22 DONE 2026-11-25 (PR #48).

**T23 Telegram webhook inbox + soft S3 humanizer (ADR 021).** Owner: Codex. Reviewer: Claude Code.
Found in the live run of beekeeper reel 002 (2026-11-25): button presses expire before the 5-minute poll,
and the humanizer rejects Volodymyr's own interview wording.
**T23a** Soft humanizer gate for S3 (ADR 021 decision 5), no infra change, first because it unblocks reel 002:
reviewer prompt treats the interview as the author's wording and keeps `unsupported_facts` to unsupported
claims only; for S3, after 3 rounds with only the AI review failing, keep the lowest-score candidate and add
`Humanizer: not passed (score N), check the text` to the preview; hard rule failures still fail. S1 unchanged.
Accept: tests for S3 soft pass (AI review fails 3x, rules pass -> item + warning), S3 hard rule failure
(no item), S1 still strict, best-candidate choice; live: reel 002 reaches the preview.
T23a DONE 2026-11-25 (PR #49).
**T23b** Webhook receiver + inbox (ADR 021 decisions 1-4): migration `telegram_inbox`; receiver function on
the chosen host with secret check, allowed-user filter, insert, immediate `answerCallbackQuery`, dispatch;
poll-telegram drains the inbox instead of `getUpdates` (pre-check removed), 7-day purge; runbook steps for
`setWebhook`/`deleteWebhook` and the receiver's secrets; cron-job.org poll-telegram job to hourly.
Touches a workflow and secrets: Volodymyr's explicit OK before merge. Accept: tests for wrong secret (401),
foreign user (nothing stored), duplicate update_id, DB failure -> 500, callback answered, dispatch failure
still 200, inbox drained in order, failed row retried, processed rows purged after 7 days; live: three
button presses in a row each answered within seconds and handled by the next run.
T23b DONE 2026-11-25 (PR #51).

**T24 Reliability follow-ups after reel 002 and T23b.** Owner: Codex. Reviewer: Claude Code.
**T24a** Retry failed items from Telegram (ADR 022): failure message with per-channel errors and
`Retry`/`Skip` buttons when an item ends in FAILED (poll-telegram, publish-due, publish-reels);
`retry:<id>` FAILED -> PUBLISHING + publish in the same run, `skipfail:<id>` FAILED -> SKIPPED, duplicate
or stale press -> `Already handled`. No migration, no workflow change. Accept: tests for the message
(channels, errors cut to 300 chars, sent once per FAILED), retry publishes only failed channels and reaches
PUBLISHED, own video retry leaves Reels for publish-reels, second press, skip, Telegram send failure does not
fail the job; live: one forced or real failure retried from Telegram.
T24a DONE 2026-11-25 (PR #52).
**T24b** Logs and callback answers: when a channel fails, the job log line names item, channel and the
sanitized error text (not only `ChannelError`); the Reels alert uses the same text; handlers stop calling
`answerCallbackQuery` for inbox updates the receiver already answered (ADR 021 decision 2), user-facing
outcomes that matter are sent as messages. Accept: tests for the log line (no token in it), no second
answer for inbox callbacks; live: one job log shows a readable failure line.
T24b DONE 2026-11-25 (PR #53).

**T25 Guided own-video intake (ADR 023).** Owner: Codex. Reviewer: Claude Code.
**T25a** Guided `/work` with description: bare `/work` -> `Original`/`AI-generated`/`Cancel`; video step
without caption or reply; `Upload description`/`No description`/`Cancel`; text messages and `.txt` joined
without reply until `Done`, which generates right away; active intake takes plain texts before S2; 60-minute
expiry; new `/work` replaces an unfinished intake; caption fast path unchanged. `No description` button is
shown but answers `Coming soon` until T25b. Accept: tests for the full path (kind -> video -> text x2 -> Done
-> preview with AI label), Cancel at each step, expiry (late button, text goes to S2 again), second `/work`,
plain text during intake not created as S2, `Done` without text, fast path still works; live: one video
through the guided flow to the preview.
T25a DONE 2026-11-26 (PR #54).
**T25b** No-description items (ADR 023 decision 5): item without texts, no AI call, Reels-only targets
(IG + FB), empty caption plus AI label line when `ai`, preview with Approve/Skip only, refusal for videos
outside 3-90 s. Accept: tests for item and targets (no LinkedIn/Threads publications), caption with and
without label, preview buttons, duration refusal, Publisher and publish-reels handle empty text; live: one
original video without description posted to IG and FB Reels.
T25b DONE 2026-11-26 (PR #55).
**T26a** Factory intake (ADR 024 decisions 1-5, 9): source `factory`, migration `factory_reels`, workflow
`factory-intake` (workflow_dispatch, JSON input v1), `FACTORY_REPO` config, download of the release
`reel/<episode>` assets from the public repo, validation (contract, slug, release_at window, sizes,
3-90 s, UTF-8, Threads length), hashes stored, item APPROVED with `release_at`, core allows approval
without Telegram only for this source and entry point, intake notice. Repeat rules: same hashes ->
unchanged, changed before publish -> replaced, after publish -> refused. Touches a workflow: Volodymyr's
OK before merge. Accept: tests for each validation error, the three repeat cases, notice text, no Claude
call, other sources still need Approve; live: one test release dispatched, notice in Telegram, row in Neon.
**T26b** Scheduled publish of factory items (ADR 024 decisions 6, 8): `publish-reels` publishes items with
passed `release_at` to LinkedIn DE video, Threads, IG and FB Reels with the texts as written (+ AI label
line), re-downloads and checks hashes, Threads skip rule, no publish before `release_at`, idempotent
reruns, failures via ADR 022 Retry/Skip. Accept: tests for due/not-due, verbatim text per channel,
hash mismatch, deleted asset, rerun after partial publish; live: test reel 002 published at a set time.
**T26c** Covers (ADR 024 decision 7): LinkedIn `uploadThumbnail: true` + PNG upload, IG `cover_url` +
`thumb_offset=0`, FB Reels thumbnail upload if the Graph API supports it for Reels (check first, else
frame 0, write the finding into the ADR). Accept: tests for request payloads and the thumbnail upload
step; live: the cover visible on LinkedIn, IG and FB for one reel.

**T28a** YouTube token (ADR 026 decision 6): token provider `youtube`, command `youtube-import-token`
(reads `YOUTUBE_REFRESH_TOKEN` from the environment, stores it encrypted), access token fetched per run and
never stored, `check-tokens` refresh check with alert on failure, config `YOUTUBE_CLIENT_ID/SECRET`.
Secrets: Volodymyr sets the two client secrets in GitHub Secrets (command given at task start). Accept: tests
for import, refresh success and failure, no token in logs; live: import from `.env`, `check-tokens` green.
**T28b** YouTube channel (ADR 026 decisions 1-4, 8): video channel `youtube` for S3 (with and without a
description) and factory reels, title rules (first line, first sentence, default title, `title_de.txt`),
`<`/`>` replaced, description with AI label line, `categoryId=28`, `containsSyntheticMedia`, resumable upload
with the session URI in `remote_ref`, recovery (308 resume, 201 take id, 404 new session), quota errors held,
preview and intake notice list YouTube. HeyGen renders untouched. Accept: tests for each title case, payload,
each recovery branch, no second upload after a completed session, quota hold; live: one own video and one
factory reel as public Shorts.
**T28c** YouTube cover (ADR 026 decision 4): `thumbnails.set` with the factory cover after
`processingStatus=succeeded`, best effort with a warning. Accept: tests for the wait, success and failure
paths; live: cover visible on one factory Short (needs the channel's intermediate features).
**T29a** TikTok token and sandbox check (ADR 026 decisions 5-6): token provider `tiktok` with refresh-token
rotation written back, `tiktok-import-token`, `TIKTOK_ENV=sandbox|production`, `/renew` for provider `tiktok`
(signed state), `check-tokens` alert 30 days before the refresh expiry. Then one sandbox inbox upload
(`video.upload`) to the public account; record the result in ADR 026: works -> `draft` mode allowed, fails ->
`tiktok` stays in `PAUSED_CHANNELS`. Accept: tests for rotation, renewal, expiry alert; live: the sandbox check.
**T29b** TikTok channel, `draft` mode (ADR 026 decisions 1, 2, 5, 8): video channel `tiktok` for S3 and
factory reels, inbox upload (`FILE_UPLOAD`, chunking), `publish_id` saved before upload, recovery via
`status/fetch`, result line with the caption to copy, rate and spam errors held. Accept: tests for chunking,
each status branch, caption line; live: one own video and one factory reel in the TikTok inbox.
**T29c** TikTok `direct` mode (ADR 026 decisions 5, 7): Telegram TikTok row in the preview and in the factory
intake notice (privacy from `creator_info`, comment/duet/stitch toggles, nothing preselected, Approve waits
for privacy), per-item settings migration, Direct Post with caption, `is_aigc`, `video_cover_timestamp_ms=0`,
factory fallback to the inbox when no choice by `release_at`. Then Claude records the sandbox demo, replaces the
placeholder video, Volodymyr submits for review. Accept: tests for the row, defaults, the gate on Approve, the
payload, the factory fallback; live: sandbox demo post (private), app submitted.
ADRs: 026 (decisions 5, 7); AGENTS.md AI label (`is_aigc = true` for every AI video in direct mode, tested).
**T27a** Project allowlist (ADR 025 decisions 2, 3, 6): `config/projects.toml` with the factory entry
(`beekeeper`, `reel`, LinkedIn DE, Threads, IG, FB, YouTube, TikTok, `auto`, `max_per_day = 2`), source
`project`, migration `project_items` with the `factory_reels` data copied, F3.3 rule in the core keyed on
`approval = "auto"`. Factory behaviour unchanged. Accept: tests for config parsing and validation, migration
up/down with data, the approval rule (auto vs tap vs unknown project); live: an existing factory item still
publishes.
**T27b** Manifest v2 intake (ADR 025 decisions 4-7): `manifest.json` schema, v1 asset names kept for the
factory, polling of enabled projects in the hourly run, workflow `project-intake` (`{"project": ...}`), `tap`
preview with Approve/Reject, `max_per_day`, `enabled = false`, idempotency per (project, id). Touches a
workflow: Volodymyr's OK before merge. Accept: tests for each manifest error, polling, both approval modes,
the day limit, the three repeat cases; live: one test release in a test repo found by polling.
**T27c** `post` kind, text (ADR 025 decision 4): text posts from a release to LinkedIn DE and Threads, text as
written, length checks. Accept: tests for intake and publish per channel; live: one test post.
**T27d** `post` kind, image (ADR 025 consequences): LinkedIn Images API upload, Threads `IMAGE` container.
Accept: tests for upload payloads and recovery; live: one image post on both.
**T27e1** Contract for other projects (ADR 025 decision 8). Owner Codex, reviewer Claude. Docs only, no code
or workflow change: `docs/publishing-contract.md` (tag rules, manifest v2 schema with every field and its
limits, assets per kind, approval and idempotency rules as seen from the project, rejection reasons, a ready
`gh release create` example for a reel and for a post, how to ask for a `config/projects.toml` entry) and the
short AGENTS.md block for other projects (ADR 025 decision 8 wording). Every limit in the doc matches the
code (`sources/project_release.py`, `projects.py`). Accept: doc reviewed by Volodymyr.

**T27e2** Factory switched to v2, then `factory-intake` removed. Owner Codex, reviewer Claude. Starts only
after one factory release in v2 was published live (the factory session moves itself, postpilot only sends
the contract). Removes `factory-intake.yml`, the v1 source/channel code and v1 asset names, drops
`factory_reels` (migration). Touches a workflow: Volodymyr's OK before merge.
ADRs: 024, 025 (decisions 4-8); AGENTS.md AI label and idempotency (factory reels keep the label on every
channel after the v1 code is removed; a v2 re-release of a published v1 episode does not post twice; both tested).

**T27f** ADR 025 gaps found in T27e1 (ADR 025 T27f notes). Owner Codex, reviewer Claude. No workflow change.
(1) Reel text on Threads: with `ai: true` append `\n\nKI-generiertes Video / AI-generated video`, counted in
the 500-char limit, both for `post_threads.txt` and for the `post_de.txt` fallback (fallback only when text plus
label fit, else Threads skipped as today); v1 factory releases and v2 alike; intake notice and preview show it.
(2) Release scan: `per_page=100`, follow pages (newest first) until a release created more than 61 days ago, an
empty page, or 10 pages (then a warning in the log). (3) Reel caption limits: the 2200 check (text plus label)
applies only when `instagram` or `tiktok` is in the item's effective channels; otherwise `post_de.txt` plus label
must fit LinkedIn's 3000. Update `docs/publishing-contract.md` to match. Accept: tests for the Threads label
(file, fallback, fallback that no longer fits, `ai: false`), pagination stops (age, empty, cap), the 2200 rule
with and without IG/TikTok; contract text matches the code.

**T30** AI-label invariant + ADR audit for T26-T29 (lesson from T27f: three task reviews missed the Threads
reel label because it was not in their Accept). Owner Codex, reviewer Claude. No workflow change.
ADRs: 019, 024, 025, 026; AGENTS.md AI label.
(1) One parametrised test over every registered channel adapter (`channels/registry.py`) and every content kind
it accepts (HeyGen avatar, own video, factory reel, `post` image): with AI media the outgoing payload carries the
label (caption/text suffix, burned-in label or the platform flag such as YouTube `containsSyntheticMedia`, TikTok
`is_aigc`); a new adapter not listed in the test's channel/kind table fails the test. (2) Claude writes
`docs/adr-audit-2026-11.md`: each binding rule of ADR 019 and 024-026 with the code or test that enforces it, or
"gap"; gaps become tasks. Accept: test fails when the label is removed from any one adapter (checked by hand
once in review); audit table reviewed by Volodymyr.

## Order
T01 -> T02 -> T03 -> T04 -> T05 -> T06 -> T07 -> T08 -> T10 -> T11 -> T12 -> T09 -> T13 -> T14 -> T15 -> T16 -> T19 -> T20 -> T17 -> T18a -> T18b -> T18c -> T21a -> T21b -> T21c -> T21d -> T18 -> T22 -> T23a -> T23b -> T24a -> T24b -> T25a -> T25b -> T26a -> T26b -> T26c (due before 2026-12-14) -> T28a -> T28b -> T28c -> T29a -> T29b -> T27a -> T27b -> T27c -> T27d -> T27e1 -> T27f -> T29c -> T30 -> T27e2.
First useful vertical slice after T11: Telegram text to approval to LinkedIn.
