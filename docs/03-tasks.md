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
**T18 Cutover.** Run both agents side by side for two weeks, then disable and delete work_in_Linkedin
(Volodymyr confirms). Also delete social_bridge and "lumara" Projects if not yet done.
T18 is not before 2026-12-11 (end of the old agent's handmake series, ADR 018 amendment): copy the old
agent's posted repos into `posted_repos`, `/series_start postpilot 5`, enable `draft-weekly.yml` and
cron-job.org job 8906614 (first draft Wed 2026-12-16).
T18 owner: Claude Code alone (Volodymyr, 2026-11-20: Codex is not available by then). Claude Code
implements and checks; Volodymyr reviews and gives the OK before any merge (author never merges own work).

## Order
T01 -> T02 -> T03 -> T04 -> T05 -> T06 -> T07 -> T08 -> T10 -> T11 -> T12 -> T09 -> T13 -> T14 -> T15 -> T16 -> T19 -> T20 -> T17 -> T18.
First useful vertical slice after T11: Telegram text to approval to LinkedIn.
