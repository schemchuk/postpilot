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
