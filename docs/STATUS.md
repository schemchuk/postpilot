# STATUS

- Active agent: claude-code (orchestrating Codex, docs/HANDOFF.md)
- Current task: T04 Telegram notifier + approval (docs/03-tasks.md). Spec, architecture,
  ADRs 001-006 and the task plan are APPROVED by Volodymyr (2026-11-18).
- Done: T01 Skeleton + CI (PR #1, merge confirmed by Volodymyr retroactively). T02 Store + models + state
  machine (PR #2). T03 Publisher + fake channel + Channel protocol (PR #3): 90 tests green incl. 16 integration
  tests against Neon.
- Branch: main
- Last commit: see `git log -1`
- Next step: secret gate for T04: Volodymyr creates a new Telegram bot, puts TELEGRAM_BOT_TOKEN and
  TELEGRAM_CHAT_ID into `.env` and GitHub Secrets. Then Claude Code starts `feat/T04-telegram-notifier`.
- Blocked on: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID (Volodymyr).
- Database: Neon free Postgres (Frankfurt), `DATABASE_URL` is in `.env` and in GitHub Secrets (unquoted,
  never `source` `.env`). Integration tests run with `TEST_DATABASE_URL` in a throwaway schema; they are
  skipped in CI until a workflow change adds the secret (needs Volodymyr's explicit OK).
- Notes: T03 recovers stale PUBLISHING claims via the channel marker lookup (`find_existing`); real
  adapters must implement it. A sender still running past `stale_after` can race the lookup.
- Secrets: ANTHROPIC_API_KEY, LinkedIn app credentials, DATABASE_URL are set. Later: Telegram bot token
  (T04), TOKEN_ENCRYPTION_KEY, HeyGen key/avatar, Meta app, Threads, IG, FB credentials.
- Decisions: git user.name stays `schemchuk`.
- Pending cleanup: delete GitHub repo social_bridge and "lumara" Projects (confirm first).
