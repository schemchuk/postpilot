# STATUS

- Active agent: claude-code (orchestrating Codex, docs/HANDOFF.md)
- Current task: T05a Token lifecycle core (docs/03-tasks.md T05, split in two: T05a core + encrypted
  store + check-tokens logic; T05b LinkedIn assisted renewal + callback page + redirect URI). Spec, architecture,
  ADRs 001-006 and the task plan are APPROVED by Volodymyr (2026-11-18).
- Done: T01 Skeleton + CI (PR #1, merge confirmed by Volodymyr retroactively). T02 Store + models + state
  machine (PR #2). T03 Publisher + fake channel + Channel protocol (PR #3). T04 Telegram notifier + approval (PR #4,
  merged with Volodymyr's OK): 133 tests green incl. integration tests against Neon.
- Branch: main
- Last commit: see `git log -1`
- Next step: secret gate for T05a: Volodymyr generates TOKEN_ENCRYPTION_KEY into `.env` and GitHub Secrets
  (command given in chat). Then Claude Code starts `feat/T05a-token-core`. T05b needs the LinkedIn redirect
  URI registered (guide at that time) and Volodymyr's OK for the workflow/secret-handling parts.
- Blocked on: TOKEN_ENCRYPTION_KEY (Volodymyr).
- Database: Neon free Postgres (Frankfurt), `DATABASE_URL` is in `.env` and in GitHub Secrets (unquoted,
  never `source` `.env`). Integration tests run with `TEST_DATABASE_URL` in a throwaway schema; they are
  skipped in CI until a workflow change adds the secret (needs Volodymyr's explicit OK).
- Notes: T03 recovers stale PUBLISHING claims via the channel marker lookup (`find_existing`); real
  adapters must implement it. A sender still running past `stale_after` can race the lookup.
- Secrets: ANTHROPIC_API_KEY, LinkedIn app credentials, DATABASE_URL are set. Telegram bot token and chat id are set
  (bot @volodymyr_postpilot_bot). Later: TOKEN_ENCRYPTION_KEY (T05a), HeyGen key/avatar, Meta app, Threads, IG, FB credentials.
- Decisions: git user.name stays `schemchuk`.
- Pending cleanup: delete GitHub repo social_bridge and "lumara" Projects (confirm first).
