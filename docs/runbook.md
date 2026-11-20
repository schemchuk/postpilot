# Scheduled jobs

| Workflow | UTC schedule | Manual dispatch | Purpose |
| --- | --- | --- | --- |
| Poll Telegram | Every five minutes | `gh workflow run poll-telegram.yml` | Process approvals and renewals, retry held publications after renewal, and save the Telegram offset. |
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
