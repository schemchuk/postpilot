# 019. Own video intake via Telegram `/work` (source S3)

Status: ACCEPTED (Volodymyr, 2026-11-24)

## Context

Volodymyr has his own videos (sometimes made with AI tools, sometimes filmed himself) and interview-style
texts (questions and answers, Ukrainian). The videos target a professional audience, so LinkedIn is the
main channel. He wants to add one whenever he likes with as little work as possible: one Telegram
message, one approval tap. Today the bot accepts text only (`TelegramInbox.handle_text`), and every video
in the system is a HeyGen render that must carry the AI label (ADR 006, ADR 014).

Facts checked on 2026-11-24:
- Telegram Bot API: a bot downloads files with `getFile` up to 20 MB; the download link lives at least
  one hour, the `file_id` stays valid and can be downloaded again later or re-sent with `sendVideo`
  without uploading bytes. A caption is at most 1024 characters. A video sent "as video" from a phone is
  compressed by the Telegram client (a 60 s reel is usually below 20 MB); one sent "as file" is not.
- LinkedIn Videos API (Marketing 2026-11): permission `w_member_social` (already granted), flow
  `POST /rest/videos?action=initializeUpload` (owner, fileSizeBytes) -> `PUT` each part (4 MB) to the
  returned `uploadUrl`, collect the `ETag` headers -> `POST /rest/videos?action=finalizeUpload`
  (video, uploadToken, uploadedPartIds) -> `GET /rest/videos/{urn}` until `AVAILABLE`
  (`PROCESSING`, `PROCESSING_FAILED`, `WAITING_UPLOAD`). MP4, 3 s to 30 min, 75 KB to 500 MB.
  The post references the video in `content.media.id`.

## Decision

1. **New source S3 "own video".** An item of source S3 has one own video and a post generated from
   Volodymyr's interview text. HeyGen never renders an S3 item (`render-video` skips S3).
2. **Intake: one message.** Volodymyr sends a video (Telegram `video`, or `document` with an `video/mp4`
   MIME type) with the caption `/work [ai] [interview text]`. Only his user ID is accepted (F3.4).
   - `ai` right after `/work` marks the video as AI-generated (see 6). Without it the video is treated
     as filmed by him.
   - Longer texts (captions max 1024 chars): he replies to the bot's "Video received" message with a
     text message or a `.txt` document (UTF-8, max 20,000 chars). Several text replies are joined in
     order until he presses `Generate`.
   - A video without text gets the answer `Video received. Reply with the interview text (message or
     .txt).` and waits; nothing is generated or published.
   - `/work` without a video answers with the usage line.
   - A file over 20 MB gets `Video over 20 MB: send it as video, not as file, or shorten it.` and is not
     stored.
3. **No video bytes in Postgres.** The intake stores the Telegram `file_id`, `file_unique_id`, size,
   duration, width, height and the AI flag in a new table `own_videos` (migration 0016), linked to the
   item. Channels download the bytes via `getFile` at publish time (under 20 MB, in memory). This keeps
   the Neon free tier (0.5 GB) safe. The same `file_unique_id` is never accepted twice (idempotent
   intake; the bot answers `This video was already added.`).
4. **Post generation.** One Claude call turns the interview into a LinkedIn post in Ukrainian and German
   (same JSON schema style as S1), then the humanizer gate (ADR 007) runs as for S1; failure keeps no
   item and answers `Generation failed (<step>), resend /work or press Generate again.` The interview is
   Volodymyr's material, so the prompt keeps his facts and wording, and invents nothing. F2.2 (no
   rewriting) still applies to S2; S3 is rewriting by his explicit choice (2026-11-24).
5. **One preview, one approval.** The preview is the video (re-sent by `file_id`), followed by the UA and
   DE texts with Approve / Edit / Regenerate / Skip, the AI label state (`AI label: yes|no`) and the target
   channels. Approving the item approves the text and the video together (the S3 video needs no second
   approval, because Volodymyr made it and saw it in the preview). After approval it is published right
   away, like S2 (F2.3).
6. **AI label.** With `ai`: caption line `KI-generiertes Video / AI-generated video`, Instagram
   `is_ai_generated=true`, LinkedIn text line (no API flag exists). Without `ai`: no label. ADR 006 is
   amended: its rule binds HeyGen and every video flagged `ai`; the channels reject an `ai` video
   without the label, as before.
7. **Channels.** LinkedIn DE and UA posts with the video (main audience), Threads shortened DE text
   (existing channel, text only), Instagram and Facebook Reels with the DE post as caption. All four
   channels are the default for every S3 item. A video that is not 9:16 or below 540x960 is fitted
   automatically with ffmpeg (scale and pad to 1080x1920, nothing cropped or cut) before the Reels
   upload. Only a video outside 3-90 s skips Reels (never trimmed); the preview warns about it before
   approval.
   The LinkedIn video is uploaded once per item and its URN is stored before the post is created, so a
   rerun reuses it (ADR 013 rule: store the remote reference before any call that can publish).
8. **Effort.** Per video: one message (plus a text reply if the text is long) and one tap. No local
   script, no file host, no new secret.

## Consequences

- Migration 0016 (`own_videos`, source S3), new LinkedIn video upload path in the LinkedIn channel,
  Reels channels read bytes from Telegram for S3 instead of `video_renders`.
- Videos above 20 MB need compression on the phone (send "as video"). A local Bot API server would lift
  the limit but needs an always-on host, rejected (ADR 001).
- A deleted Telegram message does not invalidate the `file_id`; if Telegram ever refuses the file, the
  publish fails with `ChannelError` and Telegram reports it.
- Poll-telegram runs every 5 minutes, so the preview comes up to 5 minutes after sending.
- Split into T21a (intake, storage, generation, preview), T21b (LinkedIn video), T21c (Reels and Threads
  for S3), each under ~400 non-test lines.
