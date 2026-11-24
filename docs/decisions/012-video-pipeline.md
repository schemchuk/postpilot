# 012. HeyGen rendering and Telegram approval

Status: ACCEPTED (Volodymyr, 2026-11-19)

Generate a spoken German script from the approved LinkedIn German variant. Before
submitting to HeyGen, check the UTC monthly render cap and wallet balance. Process
one item per run and serialize render jobs so simultaneous runs share the budget.

Persist the captioned MP4 in Postgres as `bytea` after burning in the AI-generated
label. Send that video to Telegram for a second approval from the configured user.
If the file exceeds the upload limit, keep it READY and send an alert without
approval buttons.

A failed render can be retried once by an explicit operator command. Report
interrupted RENDERING rows for investigation because HeyGen may already have
billed the request. Instagram and Facebook publishing is handled in T14.

## Amendment 2026-11-24: HeyGen video on LinkedIn (T21d)

Status: ACCEPTED (Volodymyr, 2026-11-24)

Volodymyr asked (2026-11-24) to post approved HeyGen videos on LinkedIn too, opt-in per item, and
chose a separate second post over holding the text post (2026-11-24).

1. **Opt-in toggle, default no.** The video preview gets a third button `Also on LinkedIn: no`
   next to Approve / Reject. A tap by the configured user flips it (`no` <-> `yes`) and edits the
   button label. The value is stored on the render (migration 0017:
   `video_renders.linkedin boolean NOT NULL DEFAULT false`). It can be changed only while the
   render is READY; after Approve or Reject the button is removed and the value is frozen.
2. **Second post, the text post is unchanged.** The LinkedIn text posts (DE, UA) are published as
   today, on schedule, independent of the video. When an approved render has `linkedin = true`,
   `publish-reels` additionally publishes two LinkedIn video posts, `linkedin-video` (DE) and
   `linkedin-ua-video` (UA), tracked in `video_publications` like the Reels channels. With
   `linkedin = false`, no approved render, or a rejected/failed render, nothing more goes to
   LinkedIn.
3. **Text of the video post.** First line `KI-generierter Avatar / AI-generated avatar` (always,
   ADR 006: there is no LinkedIn API flag), then the approved variant text of that language. The
   added line also keeps LinkedIn from rejecting the post as a duplicate of the text post.
4. **Idempotency.** The video post gets its own marker `postpilot:<uuid>/<lang>/video`, so marker
   lookup never confuses it with the text post (`postpilot:<uuid>/<lang>`). Upload and recovery
   reuse the T21b LinkedIn video path (ADR 019 decision 7): bytes come from `video_renders.video`,
   the video URN is stored before the post is created, a rerun reuses it, one post per language.
5. **No new secret, no workflow change.** The LinkedIn token and the publish-reels job already
   exist.

Consequences: on LinkedIn the same topic appears twice (text post, later the video post). The
video post does not wait for the text post and is not linked to it.
