# 016. Fork/star watch and profile advisor

Status: ACCEPTED (Volodymyr, 2026-11-20)

## Context

Spec F7.4 lists two more features of `work_in_Linkedin` to port: a watch that reports new forks
and stargazers of Volodymyr's public repositories, and an AI profile advisor that reads his
LinkedIn profile text and his GitHub repositories and suggests a headline, About text, skills,
featured projects and post drafts. On 2026-11-20 Volodymyr chose to port both, with the profile
text supplied through Telegram (it is private and never goes into git).

- The old watch polls `gh repo list` counts hourly and fetches the fork/stargazer lists only for a
  repo whose count changed. A repo seen for the first time is baselined without events. State is a
  local JSON file.
- The old advisor is a one-off script that reads `profile.md`, fetches READMEs of a hard-coded
  priority repo list and writes `profile_suggestions.md`.
- `poll-engagement` (ADR 015) already logs events in `engagement_events`, notifies Telegram after
  the insert and is counted by `/stats`.

## Decision

### Fork/star watch (T16a)

1. **Another source in `poll-engagement`**, isolated like the LinkedIn and Threads sources. It
   reads `GET /users/{GITHUB_OWNER}/repos?type=owner` (public repos only, paginated) with the
   workflow's built-in `github.token` as `GITHUB_TOKEN` (public data, no new secret). Archived
   repos are included; private repos are never returned by this endpoint.
2. **Cheap polling.** Per repo, the last seen `stargazers_count` and `forks_count` are kept in
   `bot_state` (key `github_watch`, keyed by the numeric repo ID). The stargazer or fork list
   (`/repos/{owner}/{repo}/stargazers`, `/forks`, paginated, capped at 10 pages) is fetched only
   when the count differs from the stored one.
3. **Log in `engagement_events`** with `channel = 'github'` and the new kinds `star` and `fork`
   (migration 0011 widens the `kind` CHECK). `source_ref` is `star:<repo_id>:<user_id>` or
   `fork:<repo_id>:<fork_repo_id>`, using numeric IDs so renames do not create duplicates.
   `actor` is the login, `post_url` the repo URL. Insert stays `ON CONFLICT DO NOTHING`.
4. **Baseline.** A repo not yet in `github_watch` has its current stargazers and forks inserted
   as already notified (no Telegram), so the first run never reports old history.
5. **Telegram.** One short HTML message per new event: `⭐ <login> starred <repo>` or
   `🍴 <login> forked <repo>`, with the login linking to `https://github.com/<login>`. Logins and
   repo names are escaped. `/stats` counts them through the existing per-channel/kind summary.
6. Unstar-then-star by another user leaves the count unchanged and is missed until the next count
   change. Accepted (same as the old agent).

### Profile advisor (T16b)

7. **Profile text through Telegram, stored in `bot_state`** (key `linkedin_profile`), handled by
   `poll-telegram`: `/profile <text>` replaces the stored text, `/profile_add <text>` appends (a
   LinkedIn profile can exceed one 4096-character Telegram message), `/profile_clear` deletes it.
   The bot confirms with the stored length only, never echoes the text. Max 20,000 characters.
8. **`/advise` runs the advisor** in `poll-telegram`: it lists Volodymyr's public, non-fork repos
   (name, description, language, last push, top 10 by last push with a README excerpt of 2,500
   characters), sends one Claude call with the profile and repo data in delimited data blocks, and
   replies with the result as a Markdown document (`sendDocument`, `profile-advice-<date>.md`).
   No hard-coded repo list. The prompt is ported from the old agent in English, positions
   Volodymyr for automation roles in Germany, and follows the humanizer rules of ADR 007.
9. The advisor only produces text for Volodymyr to read; it never edits the LinkedIn profile and
   never publishes the post drafts. A failure answers `Advice failed` and does not block the rest
   of the poll.

## Consequences

- `poll-engagement` gets `GITHUB_TOKEN: ${{ github.token }}` and `GITHUB_OWNER` in its workflow.
- GitHub logins are public data, but the 90-day purge of ADR 015 also clears `actor` for GitHub
  rows. `source_ref` keeps numeric IDs for idempotency.
- A long Claude call for `/advise` makes that `poll-telegram` run slower (within the 5-minute job
  timeout); approvals in the same run are handled before the advisor call.
- `poll-telegram` needs `GITHUB_TOKEN` too for the repo list in T16b.
