# 024. Factory reels: machine handoff, verbatim text, cover, release time

Status: ACCEPTED (Volodymyr, 2026-11-26; amends spec F3.3 and the AGENTS.md approval constraint)

## Context

The cartoon factory `beekeeper-ai-integrator` (https://github.com/schemchuk/beekeeper-ai-integrator,
public since 2026-11-26) has its own Telegram bot for scripts and approval. It uses postpilot only as a
publisher and filed four rules on 2026-11-26 (its `docs/interview/2026-11-26-round-4.md`,
`docs/postpilot-requests.md`), deadline 2026-12-14, otherwise it publishes by itself:

- R1. The post text is posted as written, no AI rewrite (approved with the script).
- R2. `cover.png` is the video thumbnail wherever the network allows (LinkedIn, IG, FB).
- R3. Publishing at the given release time (Tue/Thu 08:00 Berlin), without a tap at that hour.
- R4. Machine handoff, no manual `/work` steps; the same reel is never posted twice.
- Kept: LinkedIn DE, Threads, Instagram and Facebook Reels, AI label.

Facts on the postpilot side:
- S3 own videos (ADR 019, 023) run Claude + humanizer on the description (against R1); the no-description
  path is Reels only. S3 publishes right after Approve; there is no per-item release time.
- Telegram `file_id`s are per bot, so a video sent by the factory bot cannot be read by postpilot's bot.
- Every channel already uploads bytes (LinkedIn Videos API parts, IG resumable `rupload`, FB
  `upload_phase`), so the video can come from any URL.
- LinkedIn `initializeUpload` with `uploadThumbnail: true` returns a thumbnail upload URL (today False).
  IG Reels takes `cover_url` (a public image URL, wins over `thumb_offset`) or `thumb_offset` in ms.
  Frame 0 of every factory reel is the cover (factory T21), so `thumb_offset=0` is a correct fallback.
- AGENTS.md and spec F3.3: no publishing without an explicit approval in Telegram.

The factory also mentioned a request about postpilot's Friday GitHub post. Volodymyr declined it
(2026-11-26): the weekly post is postpilot's own rubric.

## Decision

1. **New source S4 `factory`**, separate from S3. Its items never run Claude or the humanizer.
2. **Approval is the factory's script approval.** Volodymyr decided (2026-11-26) that factory items need no
   tap in postpilot: the item is created APPROVED with `release_at`. Spec F3.3 and the AGENTS.md
   constraint get one exception: items of source `factory` created by `factory-intake`. The exception is
   enforced in the core (only that source, only that entry point), the same way F3.3 is.
3. **Handoff through a public GitHub release of the factory repo** (Volodymyr, 2026-11-26). Per episode
   the factory publishes a release with the tag `reel/<episode>` and the assets:
   - `reel_de.mp4` (required; Reels need 3-90 s; max 100 MB),
   - `cover_de.png` (required),
   - `post_de.txt` (required, UTF-8, the LinkedIn/IG/FB text as written),
   - `post_threads.txt` (optional, at most 500 chars).
   The repo is fixed in postpilot config (`FACTORY_REPO=schemchuk/beekeeper-ai-integrator`), never taken
   from the input. Assets are public, so postpilot needs no new secret to read them.
4. **Trigger: `workflow_dispatch` on a new workflow `factory-intake`** with one JSON input
   `{"v": 1, "episode": "002-medunko-v-splyachtsi", "release_at": "2026-12-01T08:00:00+01:00", "ai": true}`.
   The factory calls it with its own fine-grained PAT (postpilot repo only, Actions write), kept in the
   factory's `.env`. The job checks the contract version, the episode slug, that `release_at` has an offset
   and is not more than 1 hour in the past or 60 days ahead, downloads the assets, checks sizes, duration,
   text length and UTF-8, and stores the item. Asset bytes are not stored in Neon: the row keeps the tag,
   the asset names and their SHA-256; publishing downloads them again and fails if a hash changed.
5. **Idempotency (R4).** `episode` is unique. A repeated dispatch with the same episode and the same asset
   hashes changes nothing. A repeat with different hashes or `release_at` while the item is not yet
   published replaces the stored data (the factory fixed something before release); after any channel
   has published it is refused. The bot says which of the three happened.
6. **Verbatim text (R1).** LinkedIn DE, Instagram and Facebook get `post_de.txt` as written, plus the AI
   label line when `ai` is true (ADR 019 decision 6). Threads gets `post_threads.txt`, else `post_de.txt`
   when it is at most 500 chars, else Threads is skipped and the intake notice says so. No truncation,
   no AI shortening. LinkedIn UA stays paused (ADR 020); the factory sends DE only.
7. **Cover (R2).** LinkedIn: `uploadThumbnail: true` and `cover_de.png` uploaded to the returned URL.
   Instagram: `cover_url` = the public asset URL, `thumb_offset=0` as well (used if Meta ignores the URL).
   Facebook Reels: upload the cover as the video thumbnail if the Graph API accepts it for Reels (to be
   verified in T26c), else frame 0.
8. **Release time (R3).** `publish-reels` (hourly at :00 UTC, has the Meta settings and ffmpeg) publishes
   factory items whose `release_at` has passed, on all four channels. 08:00 Berlin is a full hour in UTC
   in winter and summer, so a reel goes out within minutes of its release time. A cron-job.org miss is
   caught by the next hourly run.
9. **Telegram notices, no buttons except on failure.** On intake: `Factory reel <episode> · scheduled
   <release_at Berlin> · LinkedIn DE, Threads, IG, FB · AI label: yes|no` (plus the Threads skip note and
   the release link). After publishing: the usual result lines. A failure uses ADR 022 Retry/Skip.
   There is no Stop button (factory ADR 005 decision 7); the fallback is to delete the release before
   `release_at` (publishing then fails on download and asks Retry/Skip).

## Consequences

- One migration: `factory_reels` (episode unique, tag, asset names and hashes, texts, `ai`, `release_at`,
  item id). Items get the source `factory`.
- The factory can publish to Volodymyr's accounts without a tap in postpilot. That power sits in the
  factory's PAT (Actions write on postpilot): it lives only in the factory's `.env`, and rotating it stops
  the handoff.
- postpilot depends on GitHub release downloads at publish time; a deleted asset fails the item visibly.
- The factory's own `/work` fallback stays: S3 still works for any video.
- Tasks: T26a intake, T26b scheduled publishing with verbatim text, T26c covers. Due before 2026-12-14.
