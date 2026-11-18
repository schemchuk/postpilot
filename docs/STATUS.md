# STATUS

- Active agent: claude-code (orchestrating Codex, docs/HANDOFF.md)
- Current task: T03 Publisher + fake channel + Channel protocol (docs/03-tasks.md). Spec, architecture,
  ADRs 001-006 and the task plan are APPROVED by Volodymyr (2026-11-18).
- Done: T01 Skeleton + CI (PR #1, merge confirmed by Volodymyr retroactively). T02 Store + models + state
  machine (PR #2): 68 unit tests, 9 integration tests green against Neon.
- Branch: feat/T03-publisher (once started)
- Last commit: see `git log -1`
- Next step: Codex builds T03; Claude Code reviews, runs checks, merges after green CI.
- Blocked on: nothing.
- Database: Neon free Postgres (Frankfurt), `DATABASE_URL` is in `.env` and in GitHub Secrets (unquoted,
  never `source` `.env`). Integration tests run with `TEST_DATABASE_URL` in a throwaway schema; they are
  skipped in CI until a workflow change adds the secret (needs Volodymyr's explicit OK).
- Notes for T03: a crash after `claim_publication` leaves a PUBLISHING row that is not re-claimable;
  resolve it via the channel marker check (architecture, Idempotency).
- Secrets: ANTHROPIC_API_KEY, LinkedIn app credentials, DATABASE_URL are set. Later: Telegram bot token
  (T04), TOKEN_ENCRYPTION_KEY, HeyGen key/avatar, Meta app, Threads, IG, FB credentials.
- Decisions: git user.name stays `schemchuk`.
- Pending cleanup: delete GitHub repo social_bridge and "lumara" Projects (confirm first).
