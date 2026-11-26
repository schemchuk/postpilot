# AGENTS.md — postpilot (working name)

## Language rules
- Talk to Volodymyr in Ukrainian.
- Write all repository docs, code, comments and commit messages in English.
- Volodymyr writes his own texts in Ukrainian in Telegram. Never rewrite his texts beyond what the
  spec allows (translation, length limits) without asking.

## Project
A personal-brand content pipeline for Volodymyr, built as a portfolio project for automation roles
in Germany (and Ukraine from 2029-01-01). It must show real engineering: agents in production,
CI/CD, tests, documentation and decision records, not a demo.

Content sources:
1. Weekly AI-written posts about Volodymyr's GitHub activity (4 per month, mandatory).
2. Texts Volodymyr writes in Telegram (Ukrainian), extra posts on demand.

Outputs: LinkedIn (text, length as in the current agent), Threads (text only, shortened),
a HeyGen avatar video based on the LinkedIn version, published to Instagram Reels and Facebook Reels
with separate credentials. Every post is previewed and approved in Telegram before publishing.

Prior art (learn from it, do not copy blindly):
- `work_in_Linkedin` — the former LinkedIn agent (Python, systemd). Removed 2026-11-21 (ADR 018
  amendment): GitHub repo private + archived, local backup `~/backups/work_in_Linkedin-2026-11-21.tar.gz`.
- `../handmake` — process only (spec-driven gates, ADRs, STATUS.md handoff). Take NO code, assets,
  accounts or secrets from it: they belong to someone else's accounts. Only Volodymyr's own GitHub
  access is shared.

## Hard constraints
- Secrets only in `.env` (gitignored) and GitHub Secrets. Never print, log or commit tokens.
  Keep `.env.example` with placeholders. Volodymyr creates and pastes all secrets himself.
- Whenever a secret or credential is needed, give Volodymyr a step-by-step guide and a verified,
  working link to the exact page where it is obtained (open the link first to check it is live).
  Volodymyr assigns secret tasks one at a time as work progresses; do not ask for or fetch them early.
  Secrets he explicitly allows to be reused may be copied between `.env` files without printing them.
- When a secret must also go into GitHub Secrets, give Volodymyr a ready terminal command that reads the
  value from `.env` as plain text and pipes it to `gh secret set`, so he never pastes it into a browser or
  chat, e.g. `grep '^NAME=' .env | cut -d= -f2- | tr -d '\n' | gh secret set NAME`. Never `source` `.env`
  (values with `&` break the shell) and keep values unquoted. Run from the repo root; verify
  afterwards with `gh secret list` (names only).
- AI-generated media (avatar videos) is labelled as AI-generated on every channel (EU AI Act, Meta).
- Publishing is idempotent: a rerun never posts the same item twice.
- Channels are pluggable: nothing in the core depends on a specific network.
- No auto-publishing without an explicit approval in Telegram. Only exception: factory reels
  (ADR 024), approved with the script in the factory and published at their release time.
- Stack: Python (decided in interview round 2). Runs in the cloud, not on Volodymyr's laptop.
- No time box: scope is not cut to fit a deadline (round 2). Still keep tasks small.

## Roles and ownership
| Area | Owner | Reviewer |
|---|---|---|
| Interview, spec, architecture, ADRs, task planning, reviews | Claude Code | Volodymyr |
| Implementing tasks from docs/03-tasks.md | Codex | Claude Code |
| Merge of a task PR | the reviewer agent, never the author | Volodymyr (veto) |
| Secrets, accounts, legal decisions | Volodymyr only | n/a |

## Handoff protocol
`docs/STATUS.md` is the single source of truth: Active agent, Current task, Branch, Last commit,
Next step, Blocked on, Open questions for Volodymyr.
- START of every session: `git pull --rebase` (once a remote exists), read AGENTS.md and
  docs/STATUS.md, confirm you are the Active agent. If not, stop and tell Volodymyr.
- END of every session: working tree clean, STATUS.md updated, pushed, Active agent set.
- ONE TASK = ONE WINDOW. After a task is merged and STATUS.md is pushed, stop and tell Volodymyr (in Ukrainian)
  to open a new window and say "continue per STATUS". Never start the next task in the same window unless
  Volodymyr explicitly says so. Details: docs/HANDOFF.md.
- Read only the docs your task touches. One session per task. Task PRs under ~400 non-test lines.

## Git rules
- Conventional Commits, imperative, under 72 chars, trailer `Agent: claude-code` or `Agent: codex`.
- Docs commits may go to `main`. Code goes on `feat/<task-id>-<slug>`, one PR per task.
- Reviewer merges after green CI and no blocking findings (`gh pr merge --squash --delete-branch`).
  PRs touching workflows or secret handling need Volodymyr's explicit OK first.
- Non-blocking review findings go into the next task, the reviewer does not fix them itself.
- Never force-push, never rewrite history, never commit `.env`. Check `git diff --staged` for secrets.

## Workflow (spec-driven, with gates)
No code before steps 1-4 are approved by Volodymyr.
1. INTERVIEW -> docs/00-interview.md
2. SPEC -> docs/01-spec.md (what, why, acceptance criteria, non-goals). Stop for review.
3. ARCHITECTURE -> docs/02-architecture.md plus docs/decisions/NNN-title.md. Stop for review.
4. TASKS -> docs/03-tasks.md (each with Owner, Reviewer, acceptance criteria, tests). Stop for review.
5. EXECUTE one task at a time; writer and reviewer are different agents. Docs are the source of truth.

## Interview rules
- Max 5-6 questions per round, then wait. Push back on vague or contradictory answers.
- Append answers to docs/00-interview.md after each round.
- When closed, summarize in 10 lines and get confirmation before writing the spec.
