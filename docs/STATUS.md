# STATUS

- Active agent: claude-code
- Current task: T02 Store + models + state machine (docs/03-tasks.md). Spec, architecture, ADRs 001-006
  and the task plan are APPROVED by Volodymyr (2026-11-18). Window protocol: docs/HANDOFF.md.
- Done: T01 Skeleton + CI, PR #1 squash-merged to main (956fc91).
- Branch: main
- Last commit: see `git log -1`
- Next step: Secret gate for T02. Volodymyr creates a free Neon Postgres project and puts
  `DATABASE_URL` into `.env` (and GitHub Secrets). Then Claude Code creates `feat/T02-store`, writes the
  brief and runs Codex in the background (docs/HANDOFF.md, step 2).
- Blocked on: `DATABASE_URL` (Volodymyr).
- Provider choice for ADR 003 (record in T02): Neon free plan (1 GB per project, compute suspends after
  5 min and wakes on connect). Supabase rejected: free projects pause after 1 week of inactivity and need
  a manual restore.
- Secrets: ANTHROPIC_API_KEY and LinkedIn app credentials are in `.env`. Later: Telegram bot token,
  TOKEN_ENCRYPTION_KEY, HeyGen key/avatar, Meta app, Threads, IG, FB credentials.
- Open questions for Volodymyr: PR #1 changed ci.yml and AGENTS.md requires his explicit OK before such a
  merge; the merge happened without a recorded OK. Confirm it retroactively or ask for a revert.
- Pending cleanup: delete GitHub repo social_bridge and "lumara" Projects (confirm first).
