# Tasks: postpilot

Status: APPROVED by Volodymyr (2026-11-18).
`feat/<id>-<slug>`, one PR per task under about 400 non-test lines, CI green before merge.
Every task ships tests; no real network in CI. "Secret gate" = Volodymyr supplies a secret first;
Claude Code gives a step-by-step guide with a verified link at that moment (AGENTS.md).
Tasks touching workflows or secret handling need Volodymyr's explicit OK before merge.

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

## Order
T01 -> T02 -> T03 -> T04 -> T05 -> T06 -> T07 -> T08 -> T10 -> T11 -> T12 -> T09 -> T13 -> T14 -> T15 -> T16 -> T19 -> T20 -> T17 -> T18a -> T18b -> T18c -> T21a -> T21b -> T21c -> T21d -> T18.
First useful vertical slice after T11: Telegram text to approval to LinkedIn.
