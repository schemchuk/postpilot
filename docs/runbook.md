# Scheduled jobs

| Workflow | UTC schedule | Manual dispatch | Purpose |
| --- | --- | --- | --- |
| Poll Telegram | Webhook dispatch and hourly at :00 | `gh workflow run poll-telegram.yml` | Drain the Telegram inbox, process approvals and renewals, and retry held publications after renewal. |
| Check tokens | Daily at 07:00 | `gh workflow run check-tokens.yml` | Check or refresh tokens and send expiry alerts and renewal prompts. |
| Publish due | Friday at 09:00 | `gh workflow run publish-due.yml` | Publish approved items and resume unfinished publications when channels are configured. |

Scheduled database jobs require a database with the Postpilot migrations applied.
Use `gh run list --workflow <workflow-file>` to find a dispatched run. Job failures
send a redacted alert to the configured Telegram chat and fail the Actions run.
GitHub email delivery depends on the owner's Actions notification settings.

To rotate a credential, update its unquoted value in the gitignored `.env` from
the repository root. Replace `TELEGRAM_BOT_TOKEN` in the example with the
variable being rotated, then sync it and verify the secret name.

```sh
grep '^TELEGRAM_BOT_TOKEN=' .env | cut -d= -f2- | tr -d '\n' | gh secret set TELEGRAM_BOT_TOKEN
gh secret list
```

Coordinate a token encryption key change with re-encryption of stored tokens
or renew those tokens with the new key. A key change also invalidates pending renewal links.

## Retry or skip a failed item

Open the Telegram **Publishing failed** message to see the errors for active
channels. After addressing the error, press **Retry**. The bot resumes publishing
in the same run and leaves any remaining own-video Reels for the next
`publish-reels` run. Channels that already published are preserved. If the item
fails again, use the buttons on the new failure message.

Press **Skip** to close the item and keep any posts already published. Both
buttons handle the state transition without a manual database update.

## Import and renew a Threads access token

Use the one-time import with a newly generated long-lived Threads access token.
From the repository root, pass the token from the unquoted `THREADS_ACCESS_TOKEN`
entry in your local `.env` to the process without echoing it:

```sh
THREADS_ACCESS_TOKEN="$(grep '^THREADS_ACCESS_TOKEN=' .env | cut -d= -f2-)" \
  postpilot threads-import-token
```

The command stores the token encrypted and records an expiry 60 days from import.
Remove the import-only entry from `.env` after success. Configure
`THREADS_APP_ID` and `THREADS_APP_SECRET` for the jobs that use Threads.
`THREADS_REDIRECT_URI` defaults to `LINKEDIN_REDIRECT_URI`. Register the effective
callback URI in the Threads app.

With Threads settings available to the job, `check-tokens` attempts refresh within
14 days of expiry. For assisted renewal, send `/renew threads` in Telegram, open
the authorization link, and send the command produced by the callback page.

## Facebook Page token

Use the Graph Page ID shown in `debug_token` under `granular_scopes[].target_ids`
for the Page permissions. The ID in a `profile.php` URL is a different ID.
For some New Pages Experience Pages, `me/accounts` returns `[]`.

From the repository root, import the unquoted `FACEBOOK_PAGE_ACCESS_TOKEN` entry
from your local `.env` without echoing it:

```sh
FACEBOOK_PAGE_ACCESS_TOKEN="$(grep '^FACEBOOK_PAGE_ACCESS_TOKEN=' .env | cut -d= -f2-)" \
  postpilot facebook-import-token
```

The command encrypts the Page token. Non-expiring Page tokens are stored with a
3650-day expiry placeholder. `check-tokens` validates the token with Meta and
alerts on missing or invalid tokens. Renewal is a manual re-import.

Set `FACEBOOK_PAGE_ID` in GitHub Secrets for both scheduled jobs:

```sh
grep '^FACEBOOK_PAGE_ID=' .env | cut -d= -f2- | tr -d '\n' | gh secret set FACEBOOK_PAGE_ID
gh secret list
```

Scheduled `publish-reels` runs skip FAILED channel publications. To include
them when selecting the oldest eligible render, run `postpilot publish-reels
--retry` or enable `retry` when dispatching the workflow manually. The job
checks any saved video ID before attempting to publish again.

## Instagram token

1. Open [Graph API Explorer](https://developers.facebook.com/tools/explorer/), select
   the `postpilot-2` app and generate a User Token with `instagram_basic`,
   `instagram_content_publish`, `pages_show_list`, `pages_read_engagement` and
   `business_management`.
2. Open the [Access Token Debugger](https://developers.facebook.com/tools/debug/accesstoken/),
   inspect the token and choose **Extend Access Token**. Copy the long-lived User Token
   into the unquoted `INSTAGRAM_ACCESS_TOKEN` entry in the gitignored `.env`.
3. In Graph API Explorer, read the linked professional account ID from the Facebook
   Page's `instagram_business_account` field and set `INSTAGRAM_BUSINESS_ACCOUNT_ID`
   in `.env`.

From the repository root, import the token without echoing it and sync the account ID
for the scheduled jobs:

```sh
INSTAGRAM_ACCESS_TOKEN="$(grep '^INSTAGRAM_ACCESS_TOKEN=' .env | cut -d= -f2-)" \
  postpilot instagram-import-token
grep '^INSTAGRAM_BUSINESS_ACCOUNT_ID=' .env | cut -d= -f2- | tr -d '\n' | gh secret set INSTAGRAM_BUSINESS_ACCOUNT_ID
gh secret list
```

The import validates and encrypts the token. Its stored expiry is the earliest
nonzero value of `expires_at` and `data_access_expires_at`, or 60 days after import
if neither is set. The token checked at setup had `expires_at=0` and a data-access
expiry about 90 days out. Re-import a new long-lived token before data access
expires. `check-tokens` alerts within 14 days of the stored expiry.

Meta marks **API setup with Facebook login** as legacy. It continues to work and
receives no new features. The API quota verified at setup was 100 posts per 24 hours.

## Traffic snapshots

The `snapshot-traffic` workflow runs daily at 07:30 UTC and supports manual
dispatch. It reads repositories from `posted_repos` and refreshes cumulative
clicks for stored short links. Configure `SHORTIO_API_KEY` and `SHORTIO_DOMAIN`
for Short.io. Provide a GitHub token with push access to the tracked repositories
as the `GH_TRAFFIC_TOKEN` Actions secret or `GITHUB_TOKEN` for local runs.
The repository owner defaults to `schemchuk` and can be overridden locally with
`GITHUB_OWNER`.

GitHub views and clones use today's UTC row when available and otherwise use
the latest available row. Snapshots are stored under the UTC run date. Rerunning
the workflow on the same date overwrites that day's values. Individual failures
are logged, and a run with tracked targets and zero successful snapshots sends
a job failure alert.

### Avatar video

`postpilot render-video` checks the monthly cap and HeyGen wallet, then renders
at most one eligible German post. The Friday workflow runs at 10:00 UTC.
Approve or reject the labelled video in Telegram. Videos exceeding 50 MB stay
READY and trigger a text alert.

Use `postpilot render-video --retry` to retry the oldest eligible FAILED render
once after checking HeyGen for any charge or completed render from the previous
attempt. Investigate alerts for RENDERING rows before making any manual changes.

## Engagement monitoring

`poll-engagement` runs every ten minutes. Run it manually with
`gh workflow run poll-engagement.yml` or `postpilot poll-engagement`.
For LinkedIn, set the `IMAP_USER` and `IMAP_PASSWORD` GitHub Secrets to the mailbox address and Gmail app
password. The local command also supports an optional `IMAP_HOST` (default `imap.gmail.com`).
The first run and any mailbox UIDVALIDITY change skip existing notifications.
Threads replies are read for posts published in the last 14 days using the existing stored
Threads token with `threads_read_replies` and require no extra secret.

The GitHub watch polls public repositories for `GITHUB_OWNER` (default `schemchuk`).
On first sight, it records existing stars and forks without Telegram notifications.
Later polls fetch each list only when its count changes, up to ten pages per list.
Counts are stored as JSON in
the `github_watch` key of `bot_state`. To rebaseline on the next poll, run
`DELETE FROM bot_state WHERE key = 'github_watch';`. Existing engagement rows are retained.

Each run clears comment text, actor names and reply drafts older than 90 days before
polling sources, even when no source is configured. Event rows remain for counts.

Send `/stats` in Telegram to see 30-day engagement and publication counts, short-link
click totals and recent clicks, and the latest GitHub traffic snapshot. The
`poll-telegram` job answers this command.

### Reply to comments from Telegram

Tap **Send** on a Threads comment to approve and publish the displayed reply.
After sending, the message shows the reply link (or the original post link) and
keeps the **Open post** button. Use Telegram **Reply** on the engagement message
to replace its draft with your own text. Threads replies fit up to 500 characters.
**Another variant** generates a fresh draft. Both edits are blocked while a reply
is sending or after it has been sent. LinkedIn replies are still copied manually.

**Send uncertain** means the bot could not confirm publication. Press **Send**
again after five minutes so the bot can check the saved container before retrying.
If Telegram reports that the token lacks `threads_manage_replies`, follow
[Import and renew a Threads access token](#import-and-renew-a-threads-access-token)
to re-authorize with `/renew threads`, grant `threads_manage_replies`, and send
the callback command in Telegram before retrying.

## Weekly GitHub draft

`draft-weekly` runs Wednesday at 18:00 Europe/Berlin via cron-job.org, with a Wednesday
17:00 UTC GitHub fallback. The job previews the German and Ukrainian variants in Telegram
for approval before Friday's publication.

- No eligible public project: Telegram reports “No weekly post this week: no new public
  project with commits.” If active projects were already posted, the message lists them
  and points to `/series`.
- Model skip: Telegram reports “No weekly post this week (<repo>): <reason>” and confirms
  the series ended when applicable.

Manual workflow dispatch generates a draft only when the current Berlin week has no
`weekly_drafts` row. If the saved draft is awaiting approval, dispatch resends its Telegram
preview from the stored variants. **Regenerate** uses the saved GitHub facts and sends a
new preview on success. If regeneration fails, Telegram reports the failed step and
resends the previous draft. If a poll is interrupted, the next poll that opens the
database restores the draft to approval and resends its preview.

Repository selection counts up to 100 commits per repository from the last seven days.
Generation sees up to 20 commit messages and the count, which also appears in the preview.

- `/series` shows the active series, its next part and the queued series, or lists previously posted repositories.
- `/series_start <repo> [parts]` starts a series about a public, unarchived, non-fork repository
  owned by `GITHUB_OWNER`. Use 2–10 parts or omit the number for an open-ended series.
- `/series_stop` stops the active series.
- `/series_next <repo> [parts]` queues one series behind the active one (same repo checks). It starts
  in the same transaction that ends the active series (last part published, skipped by the model, or
  `/series_stop`). `/series` shows it; `/series_next_cancel` removes it.

## Telegram webhook (ADR 021)

1. Import this repository as a Vercel project with `main` as the production branch and the repository
   root as the project root. The WSGI entry point is configured in `pyproject.toml`. The webhook URL is
   the root path of the production deployment, for example `https://<production-domain>/`.
   Telegram must be able to POST to that URL without Vercel authentication.
2. Set these Vercel environment variables for **Production** and deploy.

   | Variable | Value |
   |---|---|
   | `TELEGRAM_BOT_TOKEN` | Approval bot token |
   | `TELEGRAM_WEBHOOK_SECRET` | Random Telegram webhook secret, for example from `openssl rand -hex 32` |
   | `DATABASE_URL` | Postgres connection URL for the Actions database |
   | `TELEGRAM_CHAT_ID` | Allowed user's numeric private chat ID |
   | `GITHUB_DISPATCH_TOKEN` | Fine-grained PAT scoped to `postpilot` with Actions read/write |
   | `GITHUB_REPOSITORY` | Optional `owner/repo`, defaults to `schemchuk/postpilot` |

3. Dispatch `gh workflow run poll-telegram.yml` on the updated `main` and wait for success so the inbox
   migration is applied before registering the webhook. Edit the cron-job.org `poll-telegram` job
   (8905864) to run hourly at minute 00 UTC.
4. From the repository root, substitute the production domain below and run the commands. Keep the
   bot token and webhook secret as unquoted values in the local gitignored `.env`.
   The commands pass curl configuration on stdin to keep credentials out of curl's arguments.

   ```sh
   webhook_url='https://<production-domain>/'
   {
     printf 'url = "https://api.telegram.org/bot%s/setWebhook"\n' \
       "$(grep '^TELEGRAM_BOT_TOKEN=' .env | cut -d= -f2-)"
     printf 'data-urlencode = "url=%s"\n' "$webhook_url"
     printf 'data-urlencode = "secret_token=%s"\n' \
       "$(grep '^TELEGRAM_WEBHOOK_SECRET=' .env | cut -d= -f2-)"
   } | curl --silent --show-error --fail --config - \
     --data-urlencode 'allowed_updates=["message","callback_query"]'

   printf 'url = "https://api.telegram.org/bot%s/getWebhookInfo"\n' \
     "$(grep '^TELEGRAM_BOT_TOKEN=' .env | cut -d= -f2-)" |
     curl --silent --show-error --fail --config -
   ```

   Check that `ok` is true, `result.url` matches the production URL, and `allowed_updates` lists
   `message` and `callback_query`. Press three approval buttons and verify each receives
   `Received, working on it` and is handled by the next run.

For rollback, delete the webhook while retaining Telegram's pending updates.

```sh
printf 'url = "https://api.telegram.org/bot%s/deleteWebhook"\n' \
  "$(grep '^TELEGRAM_BOT_TOKEN=' .env | cut -d= -f2-)" |
  curl --silent --show-error --fail --config - --data 'drop_pending_updates=false'
```

Run the inbox consumer on `main` until pending inbox rows are handled. Revert T23b to restore
`getUpdates`, then return the cron-job.org job to every five minutes.

## External schedule trigger (cron-job.org)

GitHub `schedule` is unreliable for this repository (ADR 017), so cron-job.org dispatches the
workflows. Each cron-job.org job: URL
`https://api.github.com/repos/schemchuk/postpilot/actions/workflows/<file>/dispatches`, method
POST, body `{"ref":"main"}`, headers `Authorization: Bearer <PAT>`,
`Accept: application/vnd.github+json`, `X-GitHub-Api-Version: 2022-11-28`,
`Content-Type: application/json`, `User-Agent: postpilot-cron` (without it GitHub answers 403), timezone UTC unless specified below, failure notifications on.

| Workflow file | Schedule (UTC unless specified) |
|---|---|
| poll-telegram.yml | hourly at :00 (`0 * * * *`) |
| poll-engagement.yml | every 30 minutes |
| publish-reels.yml | hourly at :00 (`0 * * * *`) |
| check-tokens.yml | daily 07:00 |
| snapshot-traffic.yml | daily 07:30 |
| draft-weekly.yml | Wednesday 18:00 Europe/Berlin (job timezone Europe/Berlin) |
| publish-due.yml | Friday 09:00 |
| render-video.yml | Friday 10:00 |

The jobs were created on 2026-11-20 through the cron-job.org REST API (https://docs.cron-job.org/rest-api.html):
API key `CRONJOB_API_KEY` in `.env` only (created in cron-job.org Settings -> API keys, no IP limit;
100 API requests a day). `GET /jobs` lists them; `GET /jobs/<id>` returns the headers including the
PAT, so never print that response. To change all jobs (e.g. a new PAT), read one job, edit its
`extendedData.headers` in memory and `PATCH /jobs/<id>` each job. Job IDs: poll-telegram 8905864,
poll-engagement 8905882, publish-reels 8906005, check-tokens 8906007, snapshot-traffic 8906009,
publish-due 8906013, render-video 8906015.

Keep the intervals: the Neon free plan has 100 CU-hours a month and every database run keeps the
compute awake ~6 minutes (ADR 017 decision 5). Each `poll-telegram` run opens the database
to check the inbox and perform scheduled duties. Check Neon usage in the Neon console (Billing / Usage).

PAT `postpilot-cron-dispatch`: fine-grained, resource owner `schemchuk`, only repository `postpilot`,
Repository permissions Actions: Read and write, created 2026-11-20 without an expiry date (cannot be
changed afterwards; regenerate with an expiry if wanted). A 403 from GitHub means a missing
`User-Agent` header or a missing Actions permission. It is stored only in cron-job.org. Renewal: create a new PAT
the same way, replace the header in every cron-job.org job, delete the old PAT. Check with
`gh run list --workflow poll-telegram.yml -L 5` (hourly dispatches and dispatches from the webhook).

## LinkedIn profile advisor

- `/profile <text>` replaces the profile. `/profile_add <text>` appends on a new line.
  Use several messages if needed, up to 20,000 characters total.
- `/profile_clear` deletes the profile text from the database. It is stored as plain text
  in PostgreSQL in `bot_state` under `linkedin_profile`.
- `/advise` sends the stored profile and public GitHub repository data to Claude, then
  returns a dated Markdown file in Telegram. The advisor never edits LinkedIn or publishes
  drafts. Each poll processes approvals and attempts publishing before generating advice.
  If advice fails, the bot attempts to reply `Advice failed` and the job still exits 0
  unless another part of the poll fails.

## Factory reel intake

After script approval, the factory publishes a public release tagged
`reel/<episode>` in `FACTORY_REPO`. Attach `reel_de.mp4`, `cover_de.png`,
`post_de.txt`, and optionally `post_threads.txt`. Dispatch from an authenticated
checkout of postpilot, using a release time within the next 60 days.

```sh
gh workflow run factory-intake -f payload='{"v":1,"episode":"002-medunko-v-splyachtsi","release_at":"2026-12-01T08:00:00+01:00","ai":true}'
```

The Telegram notice reports whether intake stored, replaced, or refused the
episode, or found it unchanged. Once scheduled publishing is available, cancel
a reel by deleting its GitHub release before `release_at`. The next publish
attempt will report the missing release.

## YouTube Shorts setup (connector, in progress)

One-time setup for the YouTube Shorts channel. No code uses it yet; the channel
adapter follows its own ADR and task. A vertical video up to 3 minutes is
classified as a Short by YouTube automatically; the API has no Shorts flag.

1. Channel: Google account that owns the YouTube channel `shemchuk`
   (https://www.youtube.com/account).
2. Intermediate features (custom thumbnails, needed for factory covers):
   YouTube Studio -> Settings -> Channel -> Feature eligibility ->
   "Intermediate features" -> verify by phone. Advanced features are not needed.
3. Google Cloud project `postpilot` under the same account
   (https://console.cloud.google.com/projectcreate, no organization).
4. Enable "YouTube Data API v3" in that project
   (https://console.cloud.google.com/apis/library/youtube.googleapis.com).
5. OAuth consent screen (Google Auth Platform): External, app name `postpilot`,
   scope `youtube.upload` only (it covers `videos.insert` and `thumbnails.set`;
   for a Short set the thumbnail after processing has succeeded).
   Publish the app to "In production": in "Testing" refresh tokens expire after
   7 days. The app stays unverified; the owner clicks through the
   "unverified app" warning once. Google verification is not needed for one user.
6. OAuth client: type "Desktop app"; Volodymyr puts the client ID and secret in
   `.env` as `YOUTUBE_CLIENT_ID` and `YOUTUBE_CLIENT_SECRET`.
7. Refresh token: one local consent run, stored as `YOUTUBE_REFRESH_TOKEN`
   (command added with the channel task).

Quota (checked 2026-11-26): `videos.insert` has its own bucket of 100 calls per
day; other calls share 10,000 units per day. Uploads from unverified API
projects are not restricted to private, so no YouTube API audit is needed; the
audit form (https://support.google.com/youtube/contact/yt_api_form) is only for
more quota.
