# STATUS

- Active agent: codex
- Current task: T01 Skeleton + CI (docs/03-tasks.md). Spec, architecture, ADRs 001-006 and the task
  plan are APPROVED by Volodymyr (2026-11-18). Window protocol: docs/HANDOFF.md.
- Branch: main (work on feat/T01-skeleton)
- Last commit: see `git log -1`
- Next step: Codex implements T01, opens a PR, sets Active agent to claude-code for review.
  T01 changes a workflow: Volodymyr's explicit OK is required before merge.
- Blocked on: nothing
- Secrets: ANTHROPIC_API_KEY and LinkedIn app credentials are in `.env`. T02 will need DATABASE_URL
  (secret step in a Claude Code window first), later: Telegram bot token, TOKEN_ENCRYPTION_KEY, HeyGen
  key/avatar, Meta app, Threads, IG, FB credentials.
- Pending cleanup: delete GitHub repo social_bridge and "lumara" Projects (confirm first).
