# STATUS

- Active agent: claude-code (orchestrating Codex, docs/HANDOFF.md)
- Current task: T06 is merged (PR #7). Next: T07 AI client + prompts + humanizer (docs/03-tasks.md).
- Done: T01 skeleton + CI (#1), T02 store + models + state machine (#2), T03 publisher + fake channel (#3),
  T04 Telegram notifier + approval (#4), T05a token core (#5), T05b LinkedIn assisted renewal + Pages
  callback (#6), T06 CLI jobs + failure alerts + scheduled workflows (#7). 230 tests green incl. integration
  tests against Neon. Pages live: https://schemchuk.github.io/postpilot/callback/. Redirect URI is registered
  in the LinkedIn app (Volodymyr confirmed).
- Branch: main
- Last commit: see `git log -1`
- Next step: FIRST verify the GitHub secret DATABASE_URL (see Blocked on), then run
  `gh workflow run check-tokens.yml --ref main` and confirm it is green. Then start T07.
- Blocked on: GitHub secret `DATABASE_URL` is EMPTY (the early write used `source .env`, which breaks on `&`).
  Volodymyr must run: `grep '^DATABASE_URL=' .env | cut -d= -f2- | tr -d '\n' | gh secret set DATABASE_URL`.
  The scheduled workflows (poll-telegram every 5 min, check-tokens daily, publish-due Fridays) fail until it is
  fixed. If the fix is delayed, `gh workflow disable poll-telegram.yml` stops the failure e-mails (re-enable after).
- Follow-ups: purge unused oauth nonces in bot_state; local fallback `postpilot auth linkedin --open`;
  run integration tests in CI (needs a workflow change + OK); LinkedIn token has not been issued yet, the first
  `/renew` flow is untested against real LinkedIn (check-tokens will prompt: status "missing").
- Database: Neon free Postgres (Frankfurt), `DATABASE_URL` is in `.env` and in GitHub Secrets (unquoted,
  never `source` `.env`). Integration tests run with `TEST_DATABASE_URL` in a throwaway schema; they are
  skipped in CI until a workflow change adds the secret (needs Volodymyr's explicit OK).
- Notes: T03 recovers stale PUBLISHING claims via the channel marker lookup (`find_existing`); real
  adapters must implement it. A sender still running past `stale_after` can race the lookup.
- Secrets: ANTHROPIC_API_KEY, LinkedIn app credentials, DATABASE_URL are set. Telegram bot token and chat id are set
  (bot @volodymyr_postpilot_bot). TOKEN_ENCRYPTION_KEY is set (copy in a password
  manager is recommended). Later: HeyGen key/avatar, Meta app, Threads, IG, FB credentials.
- Decisions: git user.name stays `schemchuk`.
- Pending cleanup: delete GitHub repo social_bridge and "lumara" Projects (confirm first).
