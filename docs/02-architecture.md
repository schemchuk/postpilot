# Architecture: postpilot

Status: APPROVED by Volodymyr (2026-11-18).
All ADRs 001-006 are ACCEPTED (2026-11-18).

## 1. Shape

A ports-and-adapters Python package. The core knows items, states and rules; every network sits behind
an adapter. Execution is serverless-style: short jobs on GitHub Actions, durable state in a free
managed Postgres (ADR 001, 003). No always-on server.

```
 sources            core (pure logic)                      channels (adapters)
 GitHubSource  -->  Item -> Draft -> Translate/Gen  -->    LinkedInChannel (DE, UA)
 TelegramInbox -->  Humanizer, RepoSafety, ShortLinks      ThreadsChannel (DE)
                    Approval state machine                 InstagramReelsChannel
                    Publisher (idempotent fan-out)         FacebookReelsChannel
                    Video pipeline (HeyGen v3, budget)     Notifier (Telegram)
                             |
                        Store (Postgres)
```

## 2. Modules (package `postpilot/`)

| Module | Responsibility |
|---|---|
| `core/models.py` | `Item`, `Variant` (lang + text), `Publication` (item, channel, status, remote_id) |
| `core/states.py` | Item states: DRAFT, AWAITING_APPROVAL, APPROVED, PUBLISHING, PUBLISHED, SKIPPED, FAILED |
| `core/publisher.py` | Fan-out to channels; per item+channel idempotency key; retry of one channel |
| `core/gates.py` | Humanizer (S1 only), RepoSafety, length limits per channel |
| `sources/github.py` | Weekly activity, project choice, series state |
| `sources/telegram.py` | Reads updates, creates S2 items, UA to DE translation |
| `ai/` | Claude client, prompts, humanizer; the only place that imports the Anthropic SDK |
| `video/heygen.py` | v3 API: create, poll, download; subtitles; AI label; monthly cap |
| `channels/` | One file per network behind `Channel` protocol: `validate()`, `publish(item) -> remote_id` |
| `tokens/` | `TokenManager` per provider (LinkedIn, Threads, IG, FB): expiry tracking, silent refresh, assisted renewal (ADR 005) |
| `notify/telegram.py` | Preview messages, buttons, failure and expiry alerts |
| `store/` | Postgres access, migrations; no business logic |
| `cli.py` | Entry points: `draft-weekly`, `poll-telegram`, `publish-due`, `snapshot-traffic`, `check-tokens` |

## 3. Flows

**S1 weekly.** Wed cron: `draft-weekly` collects activity, checks repository link
safety, generates DE+UA and runs the humanizer before storing the item in AWAITING_APPROVAL
and sending a preview. Fri cron: `publish-due` publishes APPROVED
items. Unapproved items are never published; they wait or expire to SKIPPED.

**S2 Telegram.** `poll-telegram` (every ~5 min, Actions cron) reads updates with a stored offset
(Telegram keeps updates 24 h). A UA text from the allowed user id becomes an item; DE translation and
preview follow. On approval the same run publishes immediately.

**Video.** After approval of the LinkedIn DE text, `video` renders (budget check first), the video is
previewed in Telegram, and only after a second approval goes to Instagram and Facebook with the
AI label and burned-in DE subtitles.

**Idempotency.** Unique key (item_id, channel) in `publications`. `publish` claims the row
(PUBLISHING) in a transaction before calling the network; on success stores remote_id. A rerun skips
PUBLISHED rows and retries only FAILED ones. A crash between network success and DB write is handled
by checking the channel for an existing post with the item's marker before retrying.

## 4. Infrastructure

- GitHub Actions: scheduled workflows plus manual `workflow_dispatch`; CI (ruff, mypy, pytest) on every PR.
  The repo is public, so Actions minutes are free.
- Postgres: free managed instance (ADR 003). Secrets in GitHub Secrets; `.env` only for local runs.
- Concurrency: one workflow concurrency group per job type, so two runs never overlap.
- Workflows and secret handling changes need Volodymyr's explicit OK (AGENTS.md).

## 5. Security

- Telegram: only the allowed user id is served; everything else is dropped silently.
- Tokens never logged; error reports are redacted. Secret scanning in CI.
- LinkedIn and Meta tokens expire (about 60 days). `check-tokens` runs daily: silent refresh where the
  provider allows it, otherwise a Telegram alert 7 days ahead with a one-tap renewal flow (ADR 005).
  Tokens are stored encrypted in Postgres, never in logs. A post held by an expired token is released
  right after renewal.

## 6. Testing

Unit tests for core and gates; contract tests that every channel adapter passes against a fake
HTTP server; idempotency and partial-failure tests on the publisher; recorded fixtures for HeyGen and
Graph API. No real network in CI.

## 7. Task order (preview for gate 4)

1 skeleton + CI, 2 store + models + state machine, 3 publisher + fake channel, 4 Telegram notifier and
approval, 4b token lifecycle (TokenManager, check-tokens, LinkedIn assisted renewal), 5 AI client + humanizer, 6 GitHub source, 7 LinkedIn channel, 8 Telegram inbox S2,
9 Threads, 10 HeyGen video, 11 Instagram/Facebook Reels, 12 ported extras (short links, traffic,
series, comments, fork/star), 13 production cutover and removal of the old agent.
