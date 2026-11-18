# STATUS

- Active agent: claude-code
- Current task: architecture accepted except revised ADR 005; docs/03-tasks.md drafted for review (gate 4)
- Branch: main
- Last commit: see `git log -1`
- Next step: Volodymyr OKs ADR 005 (token lifecycle) and reviews docs/03-tasks.md; then Codex starts T01
- Blocked on: Volodymyr's review of ADR 005 and the task list
- Secrets: ANTHROPIC_API_KEY and LinkedIn app credentials are in `.env`. Needed later, one at a time,
  each with a guide and a verified link: Telegram bot token, DATABASE_URL, TOKEN_ENCRYPTION_KEY, HeyGen
  key/avatar, Meta app, Threads, IG, FB credentials.
- Pending cleanup: delete GitHub repo social_bridge and "lumara" Projects (confirm first).
