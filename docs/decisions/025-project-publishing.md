# 025. Publishing from any project: allowlisted repos, release manifest, per-project approval

Status: ACCEPTED (Volodymyr, 2026-11-26; amends spec F3.3, the AGENTS.md approval constraint and ADR 024
decisions 3-4)

## Context

Volodymyr wants publishing to social networks to be a standing service for all his future projects, the way
the cartoon factory already uses postpilot (ADR 024). Each project is built with Claude Code; the agent there
should be able to say "publish this" without holding any social network credential.

What exists after T26a-c:
- Source `factory`, table `factory_reels`, workflow `factory-intake` (`workflow_dispatch`, JSON payload v1 with
  `episode`, `release_at`, `ai`), assets read from a public GitHub release `reel/<episode>` of one repo fixed
  in config (`FACTORY_REPO`). Text verbatim, cover as thumbnail, published by the hourly `publish-reels` at
  `release_at`, idempotent per episode. Not verified live yet.
- The only exception to "no publishing without a tap in Telegram" is that source.
- Only reels (video). No way for a project to hand over a text post or a picture post.

Limits of extending this as is:
- One repo, one kind, one approval policy, all hard-coded.
- Every project would need its own PAT with Actions write on postpilot. All PATs belong to Volodymyr, so
  `github.actor` is the same for every call: postpilot cannot tell which project dispatched, and a payload
  field `repo` would be self-asserted.
- `release_at` and `ai` come from the dispatch payload, not from the release, so whoever holds a PAT decides
  them.

Alternative considered: give Claude in each project the social tokens and let it post directly. Rejected:
tokens spread over many `.env` files, every project reimplements uploads, covers, idempotency, retries and
the AI label, and nothing central enforces approval.

## Decision

1. **postpilot is the only publisher.** Projects never hold social network credentials. A project hands over
   content as a public GitHub release; postpilot reads, checks and publishes it.

2. **Allowlist in a committed file `config/projects.toml`.** One entry per project:
   ```toml
   [projects.beekeeper]
   repo = "schemchuk/beekeeper-ai-integrator"
   kinds = ["reel"]
   channels = ["linkedin-de", "threads", "instagram", "facebook"]
   approval = "auto"        # "auto" | "tap"
   max_per_day = 2          # published items per Berlin day, all kinds
   enabled = true
   ```
   The repo is never taken from the input. Changes to this file go through a PR; a PR that adds a project with
   `approval = "auto"` or widens an existing one's `channels`, `kinds` or `max_per_day` needs Volodymyr's
   explicit OK, like workflow and secret changes. Only public repos in this version.

3. **Source `project` replaces `factory`.** Items get the source `project` and a project key. The existing
   `factory_reels` rows move into a new table `project_items` (project, kind, id unique per project, tag,
   asset names and SHA-256, texts, `ai`, `release_at`, approval mode, item id). The factory becomes the
   `beekeeper` entry above with the same behaviour it has today.

4. **Release contract v2: everything lives in the release.** Tag `<kind>/<id>`, where `kind` is `reel` or
   `post` and `id` is a slug. Assets:
   - `manifest.json` (required):
     ```json
     {"v": 2, "kind": "reel", "id": "002-medunko-v-splyachtsi",
      "release_at": "2026-12-01T08:00:00+01:00", "ai": true,
      "channels": ["linkedin-de", "threads", "instagram", "facebook"]}
     ```
     `ai` is required (no default). `channels` must be a subset of the project's allowlisted channels and
     defaults to all of them that fit the kind. `release_at` needs an offset; the window is as in ADR 024
     (not more than 1 hour in the past, at most 60 days ahead).
   - `post_de.txt` (required, UTF-8, as written), `post_threads.txt` (optional, at most 500 chars),
     same rules as ADR 024 decision 6.
   - `reel`: `video.mp4` (3-90 s, max 100 MB) and `cover.png`, required.
   - `post`: `image.png` or `image.jpg`, optional. Channels: `linkedin-de`, `threads`.
   The factory's v1 asset names (`reel_de.mp4`, `cover_de.png`) stay accepted for its releases until it
   switches to v2.

   **T27c notes.** Text-only posts carry no AI label. Polling skips releases with an image asset
   until T27d without recording intake or rejection.

   **T27d notes.** A post may include one `image.png` or `image.jpg` up to 8 MB. Intake checks
   the file signature and stores the hash, which is verified again before publication. With
   `ai: true`, both captions end with `KI-generiertes Bild / AI-generated image`. LinkedIn uses
   the Images API and resumes an available image by URN, recovering uncertain posts by commentary
   and image URN. Threads saves the `IMAGE` container ID, waits for it to finish and uses it to
   recover publication.

   **T27f notes (2026-11-27, Claude, Volodymyr asked to close the gaps).** Reel text on Threads gets the
   video AI label when `ai` is true (the factory kept "AI label" for all four channels, ADR 024 context), counted
   in 500 chars. Release scan pages through releases up to 61 days old (60-day `release_at` window plus margin),
   at most 10 pages of 100. The 2200 caption check applies only to items targeting Instagram or TikTok. The
   workflow keeps the name `publish-reels`: cron-job.org dispatches it by file name, a rename goes with T27e2.

5. **Discovery by polling, dispatch optional.** The hourly `publish-reels` run (renamed `publish-projects`)
   first lists recent releases of every enabled project and takes in new `reel/*` and `post/*` tags with a
   `manifest.json`. A project needs no secret for this. `project-intake` (`workflow_dispatch`, input
   `{"project": "beekeeper"}`) runs the same scan for one project at once; it carries no content, so a leaked
   PAT can only make postpilot look earlier. The factory's v1 dispatch keeps working until it moves to v2,
   then `factory-intake` is removed.

6. **Approval per project.**
   - `tap` (default): intake sends the usual Telegram preview (text per channel, cover or image, AI label,
     release time) with Approve and Reject. Approve schedules it for `release_at`, or publishes in the next
     run when `release_at` has passed. No tap before `release_at` means it waits; it never publishes unapproved.
   - `auto`: as ADR 024 today. The project's own approval counts, the item is created APPROVED and a notice
     without buttons is sent on intake. F3.3's exception becomes "items of source `project` whose project has
     `approval = "auto"` in `config/projects.toml`", enforced in the core.

7. **Safety limits, enforced in the core.**
   - Text is never rewritten: no Claude, no humanizer, no truncation. Over-length text is refused at intake with
     the reason in Telegram.
   - `max_per_day`: an item over the limit is not published; it waits for the next day and Telegram says so.
   - `enabled = false` stops intake and publishing of not yet published items of that project.
   - Idempotency as ADR 024 decision 5, keyed by (project, id): same hashes change nothing, changed assets or
     `release_at` replace a not yet published item (a `tap` item then needs a new tap), after any channel
     published a change is refused.
   - Publishing downloads the assets again and fails on a hash mismatch (ADR 024). A deleted release before
     `release_at` makes the item fail and ask Retry/Skip (ADR 022).
   - LinkedIn UA stays paused (ADR 020); a manifest cannot select a paused channel.

8. **Contract for the agents in other projects.** `docs/publishing-contract.md` in postpilot describes the
   release format, the manifest schema, the tag rules and a ready `gh release create` example. A short block
   for each project's `AGENTS.md` points to it: "To publish, build a release per the postpilot contract; never
   handle social tokens; ask Volodymyr before the first release of a project." A project gets its entry in
   `config/projects.toml` only by a postpilot PR.
   See the [publishing contract](../publishing-contract.md).

## Consequences

- One migration: `project_items`, data copied from `factory_reels`, then `factory_reels` dropped in a later
  migration once the factory is on v2.
- New channel capability: image posts on LinkedIn (Images API upload) and Threads (`IMAGE` container).
- The hourly run makes one GitHub API call per enabled project (`GITHUB_TOKEN`, 1000 requests per hour); fine
  for tens of projects.
- New projects no longer need a PAT on postpilot; the factory's existing PAT becomes optional and can be
  revoked after it moves to v2.
- Polling means intake can lag up to an hour; a project that needs sooner calls `project-intake`.
- Not covered: private repos (would need a read-only PAT in postpilot), kinds other than `reel` and `post`,
  scheduling more than 60 days ahead, per-channel text other than DE and Threads.
- Proposed tasks, each one PR, after acceptance:
  - T27a `config/projects.toml`, source `project`, `project_items` with factory migration, F3.3 rule in core.
  - T27b manifest v2 intake, polling in the hourly run, `project-intake` workflow, `tap` preview and approval.
  - T27c `post` kind: text posts on LinkedIn DE and Threads.
  - T27d images for `post` (LinkedIn Images API, Threads image container).
  - T27e `docs/publishing-contract.md`, the AGENTS.md block, factory switched to v2, `factory-intake` removed.

## Implementation notes (T27b)

The scan runs at the start of every `publish-reels` run, scheduled every 15 minutes.
It fingerprints asset metadata from the release list, so unchanged releases require no
asset downloads. Rejection notices are deduplicated by project, tag and fingerprint.
The workflow rename is deferred to T27e. Support for `post` tags is deferred to T27c.
