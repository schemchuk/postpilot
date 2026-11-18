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
