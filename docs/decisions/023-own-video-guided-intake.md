# 023. Guided own-video intake from /work

Status: ACCEPTED (Volodymyr, 2026-11-25; amends ADR 019 decisions 2 and 7)

## Context

ADR 019 intake needs one message with a typed caption (`/work [ai] [text]`) and, for longer texts, a
Telegram *reply* to the bot's "Video received" message. Volodymyr finds both hard to remember: he has to
know the `ai` keyword and must reply to a specific message. He wants to start with a bare `/work` and be
led by buttons, never by replies (2026-11-25). He also wants to post a video without any text.

Since T23b (ADR 021) button presses reach the bot within seconds, so a step-by-step flow is practical.

## Decision

1. **Bare `/work` starts a guided intake** (one per chat). The bot answers `What kind of video?` with
   `Original` / `AI-generated` / `Cancel`. A new `/work` replaces an unfinished intake (the old one is
   cancelled, its buttons answer `Already handled`).
2. **Video step.** After the choice the bot answers
   `Send the video (MP4 up to 20 MB; Reels need 3-90 s).` The next video message in the chat (no caption,
   no reply needed) is taken. Size, duplicate and duration rules from ADR 019 stay.
3. **Description step.** After the video the bot answers `Video received.` with `Upload description` /
   `No description` / `Cancel`.
   - `Upload description`: the bot answers
     `Send the text as messages or a .txt file, then press Done.` with `Done` / `Cancel`. Plain text
     messages and `.txt` documents in the chat are joined in order (20,000 chars, UTF-8), each answered
     `Text added (N chars).` `Done` starts generation right away (ADR 019 decision 4, soft humanizer of
     ADR 021); `Done` with no text answers `Add the text first.`
   - `No description`: no AI call. The item is created with empty texts and goes straight to the
     preview (decision 5).
4. **The active intake owns the chat's plain messages.** While an intake waits for the video or the
   text, plain texts are not taken as S2 posts. It expires 60 minutes after its last step; after that
   texts are S2 again and a late button answers `Expired, send /work again.` `Cancel` ends it at once
   (`Cancelled`). Only Volodymyr's user ID is accepted, as before.
5. **No-description items publish to Instagram and Facebook Reels only**, with an empty caption plus the
   AI label line when the video is AI-generated (ADR 019 decision 6). No LinkedIn, no Threads (text-only).
   The preview shows the video and `Own video · no description · Reels only · AI label: yes|no` with
   `Approve` / `Skip` (no Edit / Regenerate, nothing to edit). A video outside 3-90 s cannot go anywhere,
   so the bot refuses `No description` for it: `Video is N s: Reels need 3-90 s. Upload a description to
   post on LinkedIn.`
6. **Old path stays.** A video with the caption `/work [ai] [text]` and replies to "Video received" work
   as in ADR 019 (fast path). Bot texts stay in English.

## Consequences

- Intake state lives on the `own_videos` row plus one pending-intake record per chat (kind chosen, no
  video yet); the implementer may use `bot_state` or a small migration, whichever keeps the tests simple.
- The S2 text handler must ask the intake first: the dispatch order changes.
- Items without text are new for the Publisher and channels: Reels caption may be empty, LinkedIn and
  Threads are not targets for these items.
- Still no auto-publishing: every path ends in a preview with Approve.
