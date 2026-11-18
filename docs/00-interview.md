# Interview

## Round 0 — context (2026-11-18)
- Idea: texts (AI-generated from GitHub activity, or written by Volodymyr in Telegram) go to
  LinkedIn and Threads (text only); a HeyGen avatar video made from the text goes to Instagram
  Reels and possibly Facebook Reels.
- Volodymyr wants to be positioned in a portfolio as an engineer who builds automations with agents.
- Process: the same gated workflow as in ../handmake.

## Round 1 (2026-11-18)
1. **Platform:** a separate project, not a tenant of handmake. handmake secrets belong to his wife's
   accounts and are never reused. A new Telegram bot is created from scratch with new secrets.
2. **Language and sources:** Volodymyr writes in Telegram in Ukrainian. His texts must also go to
   LinkedIn. The weekly posts generated from GitHub activity stay.
3. **Avatar:** a HeyGen avatar already exists (quality is poor). Budget covers 4 mandatory videos per
   month plus extra ones for texts written in Telegram. The clip is made from the LinkedIn version.
   LinkedIn length limits stay as they are; Threads is shortened to an acceptable length.
4. **Accounts:** Instagram and Facebook business accounts exist; Volodymyr extracts the secrets.
   Reels are published with separate Instagram and Facebook credentials, not via auto-share.
5. **Approval:** every post is previewed in Telegram before publishing. No auto-publishing.
6. **Portfolio:** target is an automation job in Germany; from 2029-01-01 also Ukraine. English docs
   and a new repo are fine. Codex implements for now. About 10 days for the project, full-time.
- Existing work_in_Linkedin: keep it running; its pending changes were committed and pushed
  (0945c8c, 2026-11-18). The portfolio project lives in a new folder and a new repo.

## Round 2 (2026-11-18)
1. **Name:** postpilot is accepted.
2. **Scope:** no time box ("if it needs more than 10 days, fine"). Port everything from
   work_in_Linkedin that is rational and serves the project goals.
3. **Languages:** Volodymyr asks for a recommendation that makes the project look coherent
   (see round 3).
4. **Hosting:** move to the cloud; the laptop will be replaced soon.
5. **Stack:** Python. Nothing is taken from handmake except Volodymyr's own GitHub access.
   Volodymyr owns the domain coachhelp.de.
6. **HeyGen:** the plan has API access; Volodymyr will top up credits. He does not know which
   avatar is meant and wants to be shown it. Note: an automated lookup with the HeyGen key from
   ../social_bridge was blocked by the permission system (key belongs to another project).

## Round 3 (2026-11-18)
1. **Languages:** recommendation accepted. LinkedIn DE first, then UA. Threads DE only. Reels: avatar
   speaks German, German subtitles burned in. Telegram texts are written in UA and auto-translated to
   DE; both versions are shown in the preview.
2. **Avatar:** Volodymyr knows which avatar is meant and allows copying the needed values into this
   project's `.env` (HEYGEN_API_KEY copied from ../social_bridge). The account behind that key holds
   only three private photo avatars (Numi, Arcas, Luna from lumara), no personal avatar found.
   Volodymyr wants the lumara characters deleted from HeyGen (destructive, confirm before doing it).
   HeyGen v2 endpoints are legacy (sunset 2026-10-31 announced); the adapter must use the v3 API.
3. **Domain:** coachhelp.de is to be shut down completely (Vercel, database, LinkedIn, domains).
   Not used for postpilot. A cancellation guide for paid services is wanted when that topic comes up.
4. **Cloud budget:** aim for 0 EUR/month; the project is low-traffic.
5. **Old repo:** delete work_in_Linkedin once postpilot runs in production.
6. **Routes:** weekly GitHub posts keep the Wed draft / Fri publish rhythm. Telegram texts follow
   the same route as generated posts (preview, then all channels).

## Summary for confirmation (before the spec)
1. postpilot: Python personal-brand content pipeline, portfolio for automation jobs in DE (UA from 2029).
2. Sources: weekly posts from GitHub activity (4/month) and Volodymyr's UA texts from Telegram.
3. Channels: LinkedIn (DE + UA), Threads (DE, short), Instagram Reels and Facebook Reels (DE avatar video).
4. Video: HeyGen avatar of Volodymyr, made from the LinkedIn DE text, labelled as AI-generated.
5. Every item is previewed and approved in a new Telegram bot before publishing.
6. Everything rational from work_in_Linkedin is ported (humanizer, link safety, series, analytics, comments).
7. Runs in the cloud at about 0 EUR/month; the laptop is not part of production.
8. Separate credentials for IG and FB; nothing from handmake except the process.
9. Gated workflow: spec, architecture with ADRs, tasks; Codex implements, Claude Code reviews.
10. No time box; work_in_Linkedin is deleted after postpilot goes live.

## Round 4 (2026-11-18)
- **Telegram texts** are published right after approval, not on the Friday slot.
- **HeyGen:** the account behind `HEYGEN_API_KEY` has a wallet balance of about 24 USD, so Volodymyr's
  avatar is created anew there. Numi, Arcas and Luna were deleted from it on 2026-11-18 (verified:
  no private avatars left). The second account (Umbra) has about 0.4 USD and is not used.
- **Key:** Volodymyr wants the key that lived in lumara/social_bridge retired. Plan: he generates a new
  API key in the HeyGen dashboard (which invalidates the old one) and puts it into this `.env` only.
- **Cleanup:** social_bridge (private) is to be deleted from GitHub; the lumara repo no longer exists
  there. GitHub Projects with "lumara" in the name are to be removed too.
- 2026-11-18: Umbra deleted from the second HeyGen account; local folders ../lumara and
  ../social_bridge deleted from the laptop on Volodymyr's request.
