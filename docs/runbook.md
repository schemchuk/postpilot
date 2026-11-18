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
