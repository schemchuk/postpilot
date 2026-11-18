# Handoff: how work moves between chat windows

One window = one session = one task step (AGENTS.md). Never continue a task in an old window.

## Loop for each task Tnn
1. **Secret step (only if the task has a "Secret gate").** New Claude Code window: Claude gives the
   guide and a verified link, Volodymyr puts the secret into `.env` (and GitHub Secrets if CI needs it).
2. **Build.** New Codex window: implements Tnn on branch `feat/Tnn-<slug>`, opens a PR, sets STATUS to
   `Active agent: claude-code`, says "ready for review".
3. **Review.** New Claude Code window: reviews the PR, merges if CI is green with no blocking findings,
   otherwise lists findings (Codex fixes them in a new window, then back to review). Non-blocking
   findings go into the next task. Claude then sets STATUS to the next task and `Active agent: codex`.
4. **Veto/OK.** Tasks marked "Needs OK" (workflows, secret handling): Volodymyr approves before merge.
Then repeat with the next task.

## Prompts to paste (replace Tnn)
Codex, build:
> You are Codex on the postpilot project. Read AGENTS.md and docs/STATUS.md, confirm you are the Active
> agent, then implement task Tnn from docs/03-tasks.md exactly as specified (read only the docs it
> touches). Work on branch feat/Tnn-<slug>, add tests, run ruff, mypy and pytest, open a PR, update
> docs/STATUS.md, and stop. Talk to me in Ukrainian; code, docs and commits in English.

Claude Code, review and merge:
> Read AGENTS.md and docs/STATUS.md. Review the open PR for task Tnn against its acceptance criteria in
> docs/03-tasks.md. If CI is green and nothing blocks, merge it, then update STATUS for the next task.

Claude Code, secret step:
> Read AGENTS.md and docs/STATUS.md. Task Tnn needs a secret. Give me a step-by-step guide with a verified
> working link.
