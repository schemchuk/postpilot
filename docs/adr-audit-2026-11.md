# ADR audit 2026-11 (T30 part 2)

Author: Claude (reviewer agent). Reviewer: Volodymyr.
Scope: binding rules of ADR 019 (own video), 024 (factory reels), 025 (project publishing), 026 (YouTube and
TikTok), checked against the code and tests on `main` after T29c2 (2026-11-27).

Status values:
- **test**: a test fails if the rule is broken (test named).
- **code**: enforced in code, no test that would fail if it broke.
- **process**: a rule for people or review, not code.
- **deferred**: not built yet, the task is planned.
- **superseded**: a later ADR changed the rule.
- **gap**: rule not enforced; becomes a task.

The AI-label rules across all channels are covered by one invariant test from T30 part 1
(`tests/contract/test_ai_label_invariant.py`, 69 cases over 12 adapters, plus a guard that fails when a new
adapter class has no row); rows below point to it as "T30 invariant". Review hand-check: removing the label
from FactoryThreadsChannel (Codex), YouTube `containsSyntheticMedia` and the OwnVideoReels caption (Claude) each
made the test fail.

## ADR 019 Own video intake (S3)

| # | Rule | Status | Evidence |
|---|---|---|---|
| 019-1 | HeyGen never renders an S3 item | test | `tests/jobs/test_reels.py::test_job_publishes_heygen_with_linkedin_only`, render eligibility in `tests/integration/test_video_eligibility.py` |
| 019-2a | Only Volodymyr's user ID can use `/work` | test | `tests/sources/test_own_video.py::test_foreign_sender_cannot_use_video_intake` |
| 019-2b | Video without text waits, nothing generated | test | `tests/integration/test_own_video_intake.py::test_reply_join_dedupe_and_unknown_fallback`, guided flow tests (ADR 023) |
| 019-2c | File over 20 MB refused with the fixed message, not stored | test | `test_own_video_intake.py::test_rejected_size_duplicate_and_skip` |
| 019-3a | No video bytes in Postgres, only `file_id` and metadata | code | `store/own_videos.py` schema (migration 0016); no test checks the absence of bytes |
| 019-3b | Same `file_unique_id` never accepted twice | test | `test_rejected_size_duplicate_and_skip` ("already added") |
| 019-4 | Post generation runs the humanizer gate; failure keeps no item | test | `test_own_video.py::test_own_video_humanizer_preview`, `test_own_video_intake.py::test_generation_failure` |
| 019-5 | One preview, one Approve; preview shows `AI label: yes/no` and channels | test | `test_own_video_intake.py::test_generation_regeneration_and_approval` |
| 019-6a | `ai`: caption line `KI-generiertes Video / AI-generated video`, LinkedIn text line | test | T30 invariant; `tests/contract/test_linkedin_video.py::test_label_counts_toward_length_limit` |
| 019-6b | `ai`: Instagram `is_ai_generated=true` | test | T30 invariant; `tests/contract/test_own_video_reels.py::test_own_video_instagram_caption_and_fit` |
| 019-6c | Without `ai`: no label | test | T30 invariant (non-AI rows) |
| 019-6d | Channels reject an `ai` video without the label | test | `video/reel.py::ReelVideo.validate`, `test_ai_label_invariant.py::test_reel_adapters_reject_unlabelled_ai_video` (closed G1) |
| 019-7a | LinkedIn DE and UA get the video | superseded | ADR 020 pauses LinkedIn UA; DE in `test_linkedin_video.py::test_multipart_upload_and_commentary` |
| 019-7b | Threads gets the shortened DE text only | test | `test_own_video_reels.py::test_own_video_threads_uses_shortened_german_text_only` |
| 019-7c | Non-9:16 video fitted with ffmpeg, never cropped or cut | test | `test_own_video_reels.py::test_own_video_instagram_caption_and_fit` |
| 019-7d | Outside 3-90 s skips Reels, preview warns before approval | test | `test_own_video_intake.py` ("Reels skipped: video is 91 s") |
| 019-7e | LinkedIn video URN stored before the post, rerun reuses it | test | `test_linkedin_video.py::test_finalized_ref_recovers_existing_marker`, `tests/integration/test_own_video_publish.py::test_own_video_jobs_share_targets_and_resume_saved_ref` |

## ADR 024 Factory reels (S4)

| # | Rule | Status | Evidence |
|---|---|---|---|
| 024-1 | Factory items never run Claude or the humanizer | code | intake in `sources/factory.py` has no AI client; no test asserts it |
| 024-2 | F3.3 exception only for factory source and entry point, enforced in core | test | `tests/test_factory_intake.py::test_core_publisher_rejects_factory_before_side_effects`, `tests/jobs/test_factory_contract.py::test_factory_opt_in_does_not_bypass_approval_for_other_sources` |
| 024-3a | Repo fixed in config, never from input | test | `test_factory_intake.py::test_cli_intake_uses_project_repo` (now via `config/projects.toml`, ADR 025) |
| 024-3b | Required assets, size limits (video 100 MB) | test | `test_factory_intake.py::test_missing_asset`, `test_oversized_asset` |
| 024-4a | Payload checks: version, slug, `release_at` offset and window | test | `test_factory_intake.py::test_bad_payload`, `test_release_and_duration_errors` |
| 024-4b | Asset bytes not in Neon; hashes re-checked at publishing | test | `test_factory_contract.py::test_factory_video_checks_and_caches_release_asset`, `tests/integration/test_factory_publish.py::test_factory_asset_failure_sends_retry_skip` |
| 024-5 | Repeat: same hashes no change, changed replaces, after publish refused | test | `tests/integration/test_store.py::test_factory_intake_repeats_and_publisher_isolation` |
| 024-6a | Verbatim text on LinkedIn DE, IG, FB, plus AI label line when `ai` | test | `test_factory_intake.py::test_notice_and_verbatim_text`, `test_factory_contract.py::test_linkedin_verbatim_and_uncertain_recovery_by_video_id`, T30 invariant |
| 024-6b | Threads: `post_threads.txt`, else DE if it fits 500, else skipped with a note; no shortening | test | `test_factory_intake.py::test_threads_skip_or_use_explicit_text`, `test_factory_contract.py::test_factory_threads_rejects_invalid_text_without_shortening` |
| 024-7a | LinkedIn cover via `uploadThumbnail` | test | `test_linkedin_video.py::test_factory_thumbnail_upload` |
| 024-7b | IG `cover_url` plus `thumb_offset=0` | test | `tests/contract/test_instagram_reels.py` (factory cover case) |
| 024-7c | FB cover best effort, failure only logged | test | `tests/contract/test_facebook_reels.py` (thumbnails case) |
| 024-8 | Published after `release_at` by the hourly run, unreleased untouched | test | `test_factory_contract.py::test_factory_publisher_leaves_unreleased_items_untouched`, `test_factory_publish.py::test_due_factory_reels_preserve_text_and_skip_threads` |
| 024-9 | Intake notice text, no buttons except Retry/Skip on failure | test | `test_factory_intake.py::test_notice_and_verbatim_text`, `test_factory_publish.py::test_factory_asset_failure_sends_retry_skip` |

## ADR 025 Project publishing

| # | Rule | Status | Evidence |
|---|---|---|---|
| 025-1 | postpilot is the only publisher; projects hold no social credentials | process | `docs/publishing-contract.md`; nothing to test in postpilot |
| 025-2a | Allowlist `config/projects.toml` validated, repo never from input | test | `tests/test_projects.py::test_invalid_field`, `test_invalid_key`, `test_duplicate_repo` |
| 025-2b | PR adding `auto` or widening channels/kinds/limit needs Volodymyr's OK | test | `tests/test_projects_widening.py`, `tests/test_workflows.py`, `.github/workflows/projects-guard.yml` |
| 025-3 | Source `project` replaces `factory`, data migrated | test | `tests/integration/test_project_items_migration.py::test_project_items_migration_round_trip` |
| 025-4a | Manifest v2: `ai` required, channels subset of allowlist, `release_at` window | test | `tests/sources/test_project_release.py::test_manifest_requires_each_field`, `test_manifest_rejects_invalid_fields`, `test_manifest_cannot_select_unallowed_kind` |
| 025-4b | Reel and post asset rules, Threads 500 incl. label | test | `test_project_release.py::test_reel_rejects_invalid_content`, `test_reel_threads_label_counts_toward_limit`, `test_post_rejects_invalid_text` |
| 025-4c | Text-only posts carry no AI label | test | T30 invariant (text-only row); `test_factory_contract.py::test_publish_project_post_verbatim_after_checking_text_assets` |
| 025-4d | Image post with `ai`: both captions end with the image label | test | T30 invariant; `test_project_release.py::test_image_label_counts_toward_text_limit` |
| 025-4e | Image: one file up to 8 MB, signature checked, hash re-checked | test | `test_project_release.py::test_image_post_rejects_invalid_assets`, `test_factory_contract.py::test_image_release_fails_before_channel_calls` |
| 025-4f | Reel text on Threads gets the video label when `ai` (T27f) | test | T30 invariant; `test_factory_contract.py::test_threads_only_checks_release_metadata_and_labels_stored_text` |
| 025-4g | Factory v1 asset names accepted until v2 | test | `test_project_release.py::test_publishing_reads_and_verifies_v2_asset_names`, v1 in `test_factory_intake.py` |
| 025-5a | Hourly run scans enabled projects, pages up to 61 days | test | `test_project_release.py::test_release_scan_pagination`, `test_scan_filters_and_isolates_projects` |
| 025-5b | `project-intake` dispatch carries no content | test | `tests/test_workflows.py::test_project_intake_dispatch` |
| 025-5c | Workflow renamed `publish-projects`, `factory-intake` removed after v2 | test | `tests/test_workflows.py`, `tests/sources/test_project_release.py::test_scan_filters_and_isolates_projects` (T27e2) |
| 025-6a | `tap`: preview with Approve/Reject, never published unapproved, replaced item needs a new tap | test | `tests/integration/test_store.py::test_project_tap_replacement_approval_and_refusal`, `tests/notify/test_project_approval.py::test_project_callback_checks_owner_chat_and_preview_revision` |
| 025-6b | `auto`: created APPROVED, notice without buttons; exception enforced in core | test | `test_project_release.py::test_scan_sends_notice_or_preview`, `test_auto_image_post_sends_notice_with_photo_and_no_approval_buttons`, `test_projects.py::test_publish_checks_project_policy_and_channels` |
| 025-7a | Text never rewritten; over-length refused at intake | test | `test_project_release.py::test_project_text_is_verbatim`, `test_post_rejects_invalid_text` |
| 025-7b | `max_per_day` per Berlin day, Telegram says so | test | `test_projects.py::test_project_daily_limit_and_channel_subset`, `tests/jobs/test_reels.py::test_project_daily_notices_are_deduplicated_on_berlin_day` |
| 025-7c | `enabled = false` stops intake and publishing | test | `test_projects.py::test_intake_refuses_unapproved_project_before_writes`, `test_publish_checks_project_policy_and_channels` |
| 025-7d | Idempotency by (project, id) | test | `test_store.py::test_project_post_save_replace_and_refuse_reel_clash`, `test_project_tap_replacement_approval_and_refusal` |
| 025-7e | A manifest cannot select a paused channel | test | `test_projects.py` (paused channel rejected in the allowlist, manifest must be a subset) |
| 025-8 | `docs/publishing-contract.md` and the AGENTS.md block | code | file exists; the factory still on v1 (T27e2) |
| 025-c1 | `factory_reels` dropped once factory is on v2 | deferred | T27e2 |

## ADR 026 YouTube Shorts and TikTok

| # | Rule | Status | Evidence |
|---|---|---|---|
| 026-2a | Routing: S3 and factory to YouTube/TikTok, HeyGen never | test | `tests/jobs/test_reels.py::test_youtube_routes_only_own_and_factory_and_shares_token_and_budget`, `test_tiktok_routes_only_own_and_factory_and_caches_token` |
| 026-2b | Preview and intake notice list every target channel | test | `test_own_video.py::test_own_video_tiktok_preview`, `test_factory_intake.py::test_notice_and_verbatim_text` |
| 026-3a | Title rules, default title, text never truncated, `<>` replaced | test | `tests/contract/test_youtube_upload.py::test_upload_payload_and_checkpoint` |
| 026-3b | Public, not for kids, category 28, `containsSyntheticMedia` from AI flag | test | `test_youtube_upload.py::test_upload_payload_and_checkpoint`, T30 invariant |
| 026-3c | Description = text plus AI label line when AI | test | T30 invariant |
| 026-3d | Factory `title_de.txt` used as title | test | `test_factory_intake.py::test_youtube_title_asset`, `test_oversized_youtube_title_asset_keeps_factory_intake` |
| 026-4a | Session URI stored before bytes; 308/200/404 recovery | test | `test_youtube_upload.py::test_upload_payload_and_checkpoint`, `test_recover_saved_session` |
| 026-4b | Cover via `thumbnails.set` after processing, best effort | test | `test_youtube_upload.py::test_thumbnail_wait_and_upload`, `test_thumbnail_failure_keeps_published_id` |
| 026-5a | `TIKTOK_MODE` draft or direct | test | `tests/test_config.py::test_tiktok_mode` |
| 026-5b | Draft: inbox upload, Telegram result carries the caption | test | `tests/contract/test_tiktok_upload.py::test_upload_saves_ref_before_chunks_and_polls`, `test_reels.py::test_tiktok_workflow_reports_caption_from_own_and_factory_adapters` |
| 026-5c | Direct: caption plus AI label, over 2200 skips with a note, `is_aigc`, cover frame 0 | test | `tests/contract/test_tiktok_direct.py::test_direct_payload`, `test_direct_rejection_before_upload`, T30 invariant |
| 026-5d | TikTok row from `creator_info`, nothing preselected, Approve waits for privacy | test | `tests/notify/test_tiktok_preview.py::test_tiktok_privacy_and_toggle_rows`, `test_approve_requires_privacy_when_offered` |
| 026-5e | Factory reel without a choice by `release_at` goes to inbox, others not held | test | `test_tiktok_direct.py::test_project_without_choice_goes_to_inbox_and_caption_is_available`, `test_factory_reel_uses_only_the_choice_available_at_release` |
| 026-5f | `publish_id` stored before upload, `status/fetch` recovery | test | `test_tiktok_upload.py::test_recovery`, `test_tiktok_direct.py::test_direct_recovery` |
| 026-6a | YouTube refresh token imported encrypted, access token never stored, daily check | test | `tests/tokens/test_youtube.py`, `tests/test_cli.py` (import and check-tokens) |
| 026-6b | TikTok rotation written back, alert 30 days before expiry, `/renew` | test | `tests/tokens/test_tiktok.py`, `tests/notify/test_renewal.py` |
| 026-7 | Sandbox demo recorded, app submitted | deferred | live part of T29c2, open |
| 026-8a | One item per run, independent failures, never block IG/FB | test | `test_reels.py::test_channel_failure_is_reported_and_does_not_block_others`, `test_configured_reels_channels_use_independent_claims_and_tokens` |
| 026-8b | Quota/rate/spam held, not failed | test | `test_youtube_upload.py::test_quota_pending_without_discarding_checkpoint`, `test_tiktok_direct.py::test_direct_rate_and_spam_errors_remain_pending`, `test_tiktok_upload.py::test_tiktok_error_classification` |
| 026-8c | YouTube description over 5000 bytes fails YouTube only | test | `test_youtube_upload.py::test_description_limit_counts_escaped_utf8_bytes` |

## Gaps

- **G1 (019-6d, ADR 006). Closed in T30.** `ReelVideo.validate` had no test; the invariant file now feeds an
  unlabelled AI caption to Instagram, Facebook, YouTube and TikTok (draft and direct) and expects a refusal.
- **G2 (025-2b).** Closed in T31. Agents use Volodymyr's `gh` login, so the label is an audit trail
  and a stop for honest mistakes, not a security boundary.

Rows marked **code** (019-3a, 024-1, 025-8) are low risk: they are structural (no bytes column, no AI client in
the intake module) and a change would be visible in review.
