# STATUS

- Active agent: claude-code
- Current task: architecture drafted (docs/02-architecture.md, ADRs 001-006), waiting for review (gate 3)
- Branch: main
- Last commit: see `git log -1`
- Next step: Volodymyr reviews architecture and ADRs (all PROPOSED); then write docs/03-tasks.md
- Blocked on: Volodymyr's review. Key decision: ADR 001 hosting (GitHub Actions, no server).
- Secrets: ANTHROPIC_API_KEY and LinkedIn app credentials are in `.env`. Still needed later, one at a
  time, each with a guide and a verified link: Telegram bot token, DATABASE_URL, HeyGen key/avatar,
  Meta app, Threads, IG, FB credentials.
- Pending cleanup: delete GitHub repo social_bridge and "lumara" Projects (confirm first).
