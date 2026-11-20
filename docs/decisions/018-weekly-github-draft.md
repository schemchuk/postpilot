# 018. Weekly GitHub draft job (S1 wiring)

Status: ACCEPTED (Volodymyr, 2026-11-20). Implements spec F1 and F4 with the building blocks from T07, T08 and T09.

## Context

- Spec F1 (weekly GitHub post, 4 per month, mandatory) is the main content source, but
  `postpilot draft-weekly` still prints `not implemented yet` and has no workflow. Found while
  preparing T17 on 2026-11-20.
- The parts exist and are tested: `GitHubClient.list_active_repos`, `pick_project`, `SeriesStore`,
  `check_repo` + `finalize_links` (ADR 008), `humanize_post` (ADR 007), `tracked_link` (ADR 011),
  `POST_SYSTEM_PROMPT` and `SERIES_SYSTEM_PROMPT`, `Notifier.send_preview` with a Regenerate button,
  `publish-due` (Friday) that publishes every APPROVED item.
- Nothing records a published S1 post: `SeriesStore.record_part` and `mark_posted` have no caller, so
  the no-repeat rule (F1.4) and the series history (F1.3) never get data. `snapshot-traffic` reads
  `posted_repos` and therefore has nothing to snapshot.
- The `regen` button moves an item back to DRAFT, but `poll-telegram` passes no `on_regenerate`, so
  an S1 item would stay in DRAFT forever.
- On 2026-11-20 the owner `schemchuk` has two public repositories (postpilot, handmake); all others are
  private and archived. After the first post about a repository, F1.4 excludes it, so most later weeks
  need either new activity in another public repository or an active series.
- The workflow token `github.token` cannot call `/user/repos` (it is an installation token).

## Decision

1. **Schedule.** New workflow `draft-weekly.yml` (`workflow_dispatch` plus a GitHub `schedule`
   fallback), dispatched by a new cron-job.org job on Wednesday 18:00 Europe/Berlin (the old agent's
   rhythm). `publish-due` stays Friday 09:00 UTC.
2. **Public repositories only.** Activity comes from `GET /users/{owner}/repos` (public, owner type)
   plus `/repos/{owner}/{repo}/commits?since=` for the last 7 days, authenticated with `github.token`
   for the rate limit. Private repositories are out of scope (all are archived; a link to them would be
   blocked anyway). `GitHubClient.list_active_repos` gets a `public_only` path that uses the users
   endpoint even with a token.
3. **One draft per week, idempotent.** New table `weekly_drafts` (migration 0013): `week` (Monday of the
   ISO week in Europe/Berlin, primary key), `item_id` (nullable), `repo`, `series_id`, `part`,
   `facts` (jsonb: the generation payload), `outcome` (`drafted`, `skipped`, `no_activity`),
   `recorded_at`. The row is written in the same transaction as the item and its variants. A rerun in
   the same week does nothing and prints why. A humanizer or AI failure writes no row, so a manual
   rerun retries.
4. **Project choice.** An active series wins (series prompt with `previous_parts` from
   `SeriesStore.history`). Otherwise `pick_project` chooses deterministically (most commits, not in
   `posted_repos`) and only that one project is sent to `POST_SYSTEM_PROMPT`. The model may still answer
   `skip`. No eligible project: no AI call, `outcome = no_activity`, one Telegram message that names
   the active posted repositories and says `/series` can continue one of them (T20b).
5. **Gates, in this order.** `check_repo` decides whether the `url` field is in the payload; the model
   writes DE and UA; `finalize_links` removes repository links when the check failed; `humanize_post`
   runs on both texts (fail-closed, ADR 007); both texts must fit the LinkedIn limit. Then the item is
   created (source `github`, state AWAITING_APPROVAL, variants `de` and `ua`), and, when the link is
   allowed and short.io is configured, the repository URL in each variant is replaced by a tracked
   link per language (`tracked_link` with the item ID; on short.io failure the plain URL stays).
6. **Preview with source data.** The S1 preview adds a short source block under the texts: repository,
   series part (if any), number of commits, link allowed or the blocking reasons (for example
   `missing_license`). This covers the STATUS follow-up "the preview shows no GitHub data" for S1.
7. **Old drafts expire.** When a new weekly item is created, any older S1 item still in
   AWAITING_APPROVAL moves to SKIPPED (spec section 3, "wait or expire to SKIPPED").
8. **Recording after publication.** `publish-due`, after publishing, records every S1 item that is
   PUBLISHED and whose `weekly_drafts.recorded_at` is empty: series part via
   `record_part(..., series_id=...)` or `mark_posted(repo)`, then sets `recorded_at`, in one
   transaction. A model `skip` inside a series completes the series (new `SeriesStore.complete`).
9. **Regenerate (T20b).** `poll-telegram` wires `on_regenerate` for S1: it reruns generation and the
   gates from `weekly_drafts.facts` (no new GitHub call), replaces both variants and the short links,
   moves the item back to AWAITING_APPROVAL and sends a new preview. On a gate or AI failure the old
   variants stay, the item goes back to AWAITING_APPROVAL, and the bot sends `Regeneration failed,
   previous draft kept` with the old preview, so the item is never stuck in DRAFT.
   `poll-telegram` gets `timeout-minutes: 10` for the longer AI chain.
10. **Series control (T20b).** `/series` shows the active series (repository, part, total) or the
    posted repositories with a start hint; `/series_start <repo> [parts]` starts one (repo must be
    public and owned by `GITHUB_OWNER`; parts 2-10 or empty for open-ended); `/series_stop` stops it.

## Consequences

- S1 runs end to end: Wednesday draft, approval in Telegram, Friday publication on LinkedIn DE + UA
  and Threads DE, Friday video from the DE text, history and traffic snapshots fed by real posts.
- Weekly cost: one generation call plus up to 12 humanizer calls (3 rounds x rewrite + review x 2
  languages).
- Without a LICENSE in a public repository, posts about it carry no link (ADR 008). postpilot had no
  LICENSE; Volodymyr chose MIT on 2026-11-20 (added to the repository root).
- Two tasks: **T20a** decisions 1-8 (migration, job, workflow, cron-job.org job, recording);
  **T20b** decisions 9-10 (Regenerate, series commands, poll-telegram timeout).

## Amendment 2026-11-20 (Volodymyr): rollout order

- The old agent (`work_in_Linkedin`) runs a 5-part series about handmake (part 1 on 2026-11-15, part 2 on
  2026-11-20, parts 3-5 on the Fridays up to 2026-12-11). It keeps publishing the weekly posts until that
  series ends. Posts about handmake are allowed.
- Until then `draft-weekly` is paused: the workflow is disabled (`gh workflow disable draft-weekly.yml`,
  which also stops the GitHub `schedule`) and cron-job.org job 8906614 is disabled. Live checks use a
  temporary manual dispatch.
- After 2026-12-11 (T18): copy the old agent's posted repositories into `posted_repos`, start a 5-part
  series about postpilot (`/series_start postpilot 5`), enable the workflow and the cron-job.org job. First
  postpilot draft Wednesday 2026-12-16, first publication Friday 2026-12-18. After that series the normal
  weekly rhythm applies.
- First live dispatch (run 44760632324, 2026-11-20) failed in the quality gate: the 20-commit cap made
  postpilot and handmake tie (name order picked handmake), and the humanizer rejected both texts after 3
  rounds (generated bullet lists count as markdown; strict fact review). Fixes are part of T20b.
