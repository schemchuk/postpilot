# STATUS

- Active agent: claude-code (orchestrating Codex, docs/HANDOFF.md)
- Current task: T05b is merged; waiting for Volodymyr to register the LinkedIn redirect URI. Next: T06
  Failure alerts + job entry points (docs/03-tasks.md). Spec, architecture,
  ADRs 001-006 and the task plan are APPROVED by Volodymyr (2026-11-18).
- Done: T01 Skeleton + CI (PR #1, merge confirmed by Volodymyr retroactively). T02 Store + models + state
  machine (PR #2). T03 Publisher + fake channel + Channel protocol (PR #3). T04 Telegram notifier + approval (PR #4,
  merged with Volodymyr's OK). T05a token core: encrypted store, check-tokens logic, hold on expiry (PR #5).
  T05b LinkedIn assisted renewal + GitHub Pages callback page (PR #6, merged with Volodymyr's OK): 185 tests
  green incl. integration tests against Neon. Pages live: https://schemchuk.github.io/postpilot/callback/
- Branch: main
- Last commit: see `git log -1`
- Next step: Volodymyr adds https://schemchuk.github.io/postpilot/callback/ as a redirect URI in the LinkedIn app
  (guide in chat). T06 (workflows, needs his OK) wires check-tokens alerts and held-item reruns.
- Blocked on: nothing for T06; real LinkedIn renewal needs the redirect URI registered.
- Follow-ups: purge unused oauth nonces in bot_state; local fallback `postpilot auth linkedin --open`.
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
