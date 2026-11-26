# Architecture decision records

Dates are taken from the status lines in the records; ADRs 007-009 have no date there, so their acceptance commit date is used.

| Number | Title | Status | Date | Decision |
| --- | --- | --- | --- | --- |
| 001 | [Hosting: GitHub Actions cron, no server](001-hosting-github-actions.md) | Accepted | 2026-11-18 | Run short jobs on GitHub Actions without an always-on server. ADR 017 adds external dispatch. |
| 002 | [Ports and adapters, Channel protocol](002-ports-and-adapters.md) | Accepted | 2026-11-18 | Define publishing protocols and put network-specific behavior in adapters. |
| 003 | [State store: free managed Postgres](003-state-store-postgres.md) | Accepted | 2026-11-18 | Keep durable workflow state in Neon Postgres and manage schema changes with SQL migrations. |
| 004 | [Approval as an explicit state machine](004-approval-state-machine.md) | Accepted | 2026-11-18 | Enforce Telegram approval with explicit item state transitions and a separate video approval. |
| 005 | [Token lifecycle: tracking, silent refresh, assisted renewal](005-token-lifecycle.md) | Accepted | 2026-11-18 | Use provider token managers, encrypted storage, silent refresh, and assisted renewal. |
| 006 | [AI-generated label](006-ai-label.md) | Accepted | 2026-11-18 | Label generated videos in the video and caption, and use platform labels where supported. |
| 007 | [AI client and humanizer](007-ai-client-and-humanizer.md) | Accepted | 2026-11-19 | Isolate Claude behind a text model interface and fail closed when a weekly draft fails the humanizer. |
| 008 | [Series identity and repository link checks](008-series-and-repo-safety.md) | Accepted | 2026-11-19 | Track series identity and history, and include repo links only after repository safety checks. |
| 009 | [LinkedIn Posts API channel](009-linkedin-channel.md) | Accepted | 2026-11-19 | Publish German and Ukrainian variants independently and use a visible LinkedIn recovery marker. |
| 010 | [Threads text channel](010-threads-channel.md) | Accepted | 2026-11-19 | Publish shortened German text through a Threads container. ADR 013 supersedes the visible footer. |
| 011 | [Short links and traffic snapshots](011-short-links-and-traffic.md) | Accepted | 2026-11-19 | Use Short.io links per post and language and store daily click and GitHub traffic snapshots. |
| 012 | [HeyGen rendering and Telegram approval](012-video-pipeline.md) | Accepted | 2026-11-19, amended 2026-11-24 | Check the HeyGen budget, render a captioned German video, and preview it for separate Telegram approval. Amendment: opt-in second LinkedIn video post. |
| 013 | [Threads idempotency without a visible footer](013-threads-hidden-idempotency.md) | Accepted | 2026-11-19 | Persist the Threads container ID and use status and exact-text recovery without a visible footer. |
| 014 | [Instagram and Facebook Reels publishing](014-reels-publishing.md) | Accepted | 2026-11-19 | Publish approved videos directly from Postgres through separate Instagram and Facebook API flows. |
| 015 | [Comment monitoring, reply drafts and engagement log](015-comments-and-engagement.md) | Accepted | 2026-11-20 | Monitor LinkedIn via Gmail and Threads via API, then draft replies in Telegram. ADR 017 supersedes the manual-only reply rule for Threads. |
| 016 | [Fork/star watch and profile advisor](016-fork-star-watch-and-profile-advisor.md) | Accepted | 2026-11-20 | Watch GitHub stars and forks and offer LinkedIn profile advice through Telegram. |
| 017 | [External schedule trigger and one-click Threads replies](017-external-trigger-and-one-click-replies.md) | Accepted | 2026-11-20 | Dispatch workflows from cron-job.org and send Threads replies after Telegram approval. LinkedIn replies remain manual. |
| 018 | [Weekly GitHub draft job (S1 wiring)](018-weekly-github-draft.md) | Accepted | 2026-11-20 | Generate and track weekly GitHub drafts by Berlin ISO week. Amendments: handmake takeover, queued next series, run tracing (2026-11-21). |
| 019 | [Own video intake via Telegram `/work` (S3)](019-own-video-intake.md) | Accepted | 2026-11-24 | Add own videos with interview text by one Telegram message; LinkedIn video posts, Reels, AI label only when flagged. |
| 020 | [Pause Ukrainian LinkedIn posts](020-pause-linkedin-ua.md) | Accepted | 2026-11-25 | Stop `linkedin-ua` and `linkedin-ua-video` by one code switch; UA text is still generated and shown in the preview. |
| 021 | [Telegram webhook inbox and a soft humanizer gate for own videos](021-telegram-webhook-inbox.md) | Accepted | 2026-11-25 | Receive Telegram updates by webhook into a Postgres inbox so button presses never expire; S3 posts may pass to the preview with a humanizer warning. |
| 022 | [Retry failed items from Telegram](022-retry-failed-items.md) | Accepted | 2026-11-25 | A failed publication sends its reasons to Telegram with Retry and Skip buttons; no automatic retries. |
| 023 | [Guided own-video intake from /work](023-own-video-guided-intake.md) | Accepted | 2026-11-25 | Bare /work leads through buttons (kind, video, description or none); no replies; no-description videos go to Reels only. |
| 024 | [Factory reels: machine handoff, verbatim text, cover, release time](024-factory-reels.md) | Accepted | 2026-11-26 | The cartoon factory hands reels over by a public GitHub release + workflow_dispatch; text as written, cover as thumbnail, published at release time without a tap. |
| 025 | [Publishing from any project](025-project-publishing.md) | Proposed | 2026-11-26 | Allowlisted repos hand over reels and posts as public GitHub releases with a manifest; postpilot polls them, per-project approval (tap by default, auto by explicit OK). |
