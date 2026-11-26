# Spec: postpilot

Status: DRAFT for review by Volodymyr. Source: docs/00-interview.md (rounds 0-4, summary confirmed 2026-11-18).
No architecture or code before this spec is approved.

## 1. What and why

postpilot is a Python pipeline that turns two kinds of input into approved posts on four channels:

| Source | Trigger | Output languages |
|---|---|---|
| S1: GitHub activity of Volodymyr | Weekly, 4 posts/month (mandatory) | DE, UA |
| S2: text written by Volodymyr in Telegram (UA) | On demand | UA original, DE auto-translation |

| Channel | Content | Language |
|---|---|---|
| LinkedIn | Text, length limits as in the current agent | DE first, then UA |
| Threads | Text only, shortened | DE only |
| Instagram Reels | HeyGen avatar video, burned-in subtitles | DE |
| Facebook Reels | Same video, separate credentials | DE |

Why: a portfolio project for automation roles in Germany (and Ukraine from 2029-01-01) that shows real
engineering: agents in production, CI/CD, tests, docs and decision records. It replaces
`../work_in_Linkedin`, which is deleted after postpilot is live.

## 2. Users and roles

- Volodymyr: the only user. Writes S2 texts, approves or rejects every item in Telegram.
- Claude Code: spec, architecture, ADRs, task planning, reviews. Codex: implements tasks. See AGENTS.md.

## 3. Functional requirements

### F1. Weekly GitHub post (S1)
- F1.1 Collect the last 7 days of Volodymyr's GitHub activity, pick one project, write the post in DE
  and UA (two separate posts).
- F1.2 Rhythm kept from the current agent: draft on Wednesday, publish on Friday after approval.
- F1.3 Series support: one project can continue across several weeks; only one series is active at a
  time; previous parts are passed to the next prompt to avoid repetition.
- F1.4 No repeats: a project already posted is excluded unless it is part of the active series.

### F2. Telegram text (S2)
- F2.1 Volodymyr sends a UA text to the bot. The bot translates it to DE and sends a preview with both
  versions.
- F2.2 The text is never rewritten beyond translation and the per-channel length limits (AGENTS.md).
  Humanizer does not run on S2 text.
- F2.3 After approval the item is published right away on all channels, not on the Friday slot.
- F2.4 Volodymyr can edit the DE translation in Telegram before approving.

### F3. Approval in Telegram
- F3.1 A new bot with new secrets. Nothing is reused from handmake.
- F3.2 Every item (text and video) is previewed. Actions: approve, edit, regenerate (S1 only), skip.
- F3.3 No publishing without an explicit approval. This is enforced in the core, not by convention.
  Exception (ADR 024): factory reels (source `factory`, created only by `factory-intake`) are approved by
  the factory's script approval and publish at their release time without a tap.
- F3.4 Only Volodymyr's Telegram user id may operate the bot; all other senders are ignored.

### F4. Quality and safety gates (ported from work_in_Linkedin)
- F4.1 Humanizer gate on generated (S1) text. Fail-closed: a text that does not pass is not queued.
- F4.2 Repo safety gate: a repository URL appears in a post only if the repo is public, has a README
  and a LICENSE. A missing LICENSE auto-fix (commit of an MIT file) stays configurable and off by default
  until reviewed in the architecture phase.
- F4.3 Click-tracked short links per post and language, to attribute traffic.

### F5. Video
- F5.1 The video is made by a HeyGen avatar of Volodymyr from the LinkedIn DE text, via the HeyGen v3 API
  (v2 is legacy).
- F5.2 The avatar speaks German. German subtitles are burned into the video.
- F5.3 Every video is labelled as AI-generated on every channel (EU AI Act, Meta label).
- F5.4 The video is previewed in Telegram and approved before it goes to Instagram or Facebook.
- F5.5 Budget: 4 mandatory videos per month plus extra ones for S2 texts; the wallet is about 24 USD.
  The system tracks spend and refuses to render when the configured monthly cap is reached.

### F6. Publishing
- F6.1 Channels are pluggable behind one interface; the core depends on no specific network.
- F6.2 Publishing is idempotent: a rerun never posts the same item to the same channel twice.
  Per item and channel the state is stored (pending, published with remote id, failed).
- F6.3 A failure on one channel does not block the others, and a failed channel can be retried alone.
- F6.4 Instagram and Facebook Reels use separate credentials, not auto-share.

### F7. Operations
- F7.1 Runs in the cloud, target cost about 0 EUR/month. The laptop is not part of production.
- F7.2 Telegram notification on every failure. Token lifecycle ported from the old agent and extended
  to all providers: expiry tracking, silent refresh where possible, alert 7 days ahead, one-tap assisted
  renewal for LinkedIn (about 60 days, no refresh token for ordinary apps), held posts released after
  renewal (ADR 005).
- F7.3 Daily snapshot of GitHub traffic and short-link clicks, since GitHub keeps only 14 days.
- F7.4 Ported when rational, each as its own task: comment monitoring with AI reply drafts, fork/star
  watch, engagement log and /stats, profile advisor. Order and inclusion are decided in task planning.

## 4. Non-functional requirements

- Secrets only in `.env` (gitignored) and GitHub Secrets; `.env.example` has placeholders. Never logged.
- CI on every PR: lint, type check, unit tests. Integration tests use recorded or fake channel adapters;
  real networks are never hit in CI.
- Every non-obvious technical choice has an ADR in docs/decisions/.
- Docs, code and commits in English; Volodymyr's texts in Telegram stay Ukrainian.
- Task PRs under about 400 non-test lines.

## 5. Acceptance criteria (MVP = first useful release)

1. A UA text sent to the bot returns a preview with the DE translation; after approval it appears on
   LinkedIn (DE) and Threads, and an AI-labelled video appears on Instagram and Facebook Reels.
2. The Wednesday job produces an S1 draft in DE and UA; nothing is published before approval; Friday
   publishes it.
3. Re-running any job after a success publishes nothing new (idempotency test).
4. With one channel failing, the other channels still publish and the failed one can be retried.
5. A post whose repo fails the safety gate is published without the link or held, never with the link.
6. The monthly video cap stops rendering and notifies.
7. CI is green; secrets scan finds nothing; deployment runs from the cloud without the laptop.

## 6. Non-goals

- No auto-publishing without approval, ever.
- No reuse of code, assets, accounts or secrets from handmake; no changes to work_in_Linkedin from this repo.
- No support for networks beyond the four listed (the interface allows adding them later).
- No web UI; Telegram is the only interface.
- No use of coachhelp.de (being shut down).
- No scheduling of S2 texts to the Friday slot.

## 7. Decisions and open questions

Answered by Volodymyr on 2026-11-18:
- Image generation is dropped. Posts are text plus the avatar video; no OpenAI images (removes the
  OpenAI dependency).
- Claude API: the existing key is reused (copied into this project's `.env`; same account and budget).
- LinkedIn app: lean towards reusing the existing app and saying so in the portfolio. Recommendation:
  reuse the app (Client ID and Secret) but run a fresh OAuth authorization so postpilot holds its own
  token; never copy the old access token. The app's redirect URL list must get the cloud callback
  address. Final choice is made in the architecture ADR. Old and new agent share the app's rate limits
  while both run.
- Meta developer app: unknown; checked together in the architecture phase (Threads, Instagram and
  Facebook publishing need it, and possibly app review).

Still open:
1. Cloud host for about 0 EUR/month: decided in the architecture phase.
2. LinkedIn token renewal flow in the cloud (browser step): ADR.
3. HeyGen: new API key (old one retired) and the new avatar of Volodymyr; step-by-step when needed.

## 8. Out-of-repo cleanup (tracked, not part of the code)

- Delete GitHub repo social_bridge and GitHub Projects named "lumara" (Volodymyr confirms before deletion).
- Cancellation guide for coachhelp.de paid services when Volodymyr asks.
- Delete work_in_Linkedin only after postpilot runs in production.
