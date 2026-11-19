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
**T16 Fork/star watch + profile advisor.** Ported, each optional.
**T17 Docs + portfolio.** README with architecture diagram, ADR index, demo, note that the LinkedIn app
is reused and why.
**T18 Cutover.** Run both agents side by side for two weeks, then disable and delete work_in_Linkedin
(Volodymyr confirms). Also delete social_bridge and "lumara" Projects if not yet done.

## Order
T01 -> T02 -> T03 -> T04 -> T05 -> T06 -> T07 -> T08 -> T10 -> T11 -> T12 -> T09 -> T13 -> T14 -> T15 -> T16 -> T17 -> T18.
First useful vertical slice after T11: Telegram text to approval to LinkedIn.
